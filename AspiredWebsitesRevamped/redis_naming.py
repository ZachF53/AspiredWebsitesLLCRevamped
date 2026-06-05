"""
Tag every Redis connection this process opens with a CLIENT SETNAME
so ``CLIENT LIST`` shows which process is which instead of an
indistinguishable wall of ``name=redis-py``.

The redis-py library exposes ``Connection.on_connect`` — a method
called once per socket when a pooled connection finishes authenticating.
We monkey-patch it to send an extra ``CLIENT SETNAME <process-name>``
right after the auth handshake. Every Redis connection in the process
inherits the patch — Django cache, Celery broker/result backends,
channels_redis pubsub — without per-library wiring.

Process names are computed once per Python process from ``sys.argv``
and ``os.getpid()``:

    gunicorn-12345          web workers (3 of these on prod)
    celery-worker-12346     Celery worker
    celery-beat-12347       Celery beat scheduler
    daphne-12348            Daphne ASGI server (WebSockets)
    runserver-12349         django manage.py runserver (dev)
    py-12350                anything else (manage.py shell, scripts)

The patch must be installed BEFORE any pool opens its first socket,
which is why it lives at the very top of settings.py rather than in
an AppConfig.ready() hook (those fire after some cache reads).
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)


def _process_label():
    """One short string identifying this Python process to Redis."""
    argv = ' '.join(sys.argv).lower()
    pid = os.getpid()

    if 'celery' in argv and 'beat' in argv:
        return f'celery-beat-{pid}'
    if 'celery' in argv and 'worker' in argv:
        return f'celery-worker-{pid}'
    if 'celery' in argv:
        return f'celery-{pid}'
    if 'daphne' in argv:
        return f'daphne-{pid}'
    if 'gunicorn' in argv:
        return f'gunicorn-{pid}'
    if 'runserver' in argv:
        return f'runserver-{pid}'
    if 'manage.py' in argv:
        # e.g. `manage.py shell`, `manage.py migrate`, scripts
        sub = next(
            (a for a in sys.argv[1:] if not a.startswith('-')),
            'cmd',
        )
        return f'mgmt-{sub}-{pid}'
    return f'py-{pid}'


# Computed once per process — call sites read this constant. Tests can
# monkeypatch it if they need to inspect the value.
PROCESS_LABEL = _process_label()


def install():
    """
    Monkey-patch ``AbstractConnection.on_connect_check_health`` so every
    new socket issues ``CLIENT SETNAME <PROCESS_LABEL>`` right after the
    handshake completes.

    Why this method and not ``on_connect``: in redis-py >= 5.x the
    public ``on_connect`` wrapper just delegates to
    ``on_connect_check_health``, which is what ``connect()`` actually
    calls during the open-socket flow. Patching the outer wrapper does
    nothing in production. Patching ``AbstractConnection`` covers every
    subclass — Connection, SSLConnection, UnixDomainSocketConnection —
    in one place.

    Safe to call multiple times — re-installs are no-ops because we
    flag the patched method.
    """
    try:
        from redis.connection import AbstractConnection
    except ImportError:
        logger.warning(
            'redis_naming: redis-py not importable, skipping patch')
        return

    target = AbstractConnection
    if getattr(target.on_connect_check_health, '_aspired_named', False):
        return  # already installed

    original = target.on_connect_check_health
    label = PROCESS_LABEL

    def named_on_connect_check_health(self, check_health=True):
        original(self, check_health=check_health)
        try:
            self.send_command('CLIENT', 'SETNAME', label)
            self.read_response()
        except Exception:  # noqa: BLE001
            # Naming is best-effort — a stricter Redis (ACL'd, paranoid
            # CLIENT command disabled) shouldn't break the connection.
            pass

    named_on_connect_check_health._aspired_named = True
    target.on_connect_check_health = named_on_connect_check_health
