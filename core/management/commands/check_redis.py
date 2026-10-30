"""
Confirm Redis is actually live, not merely configured.

``ResilientRedisCache`` is designed to survive Redis being down, which
means "the site is up" is not evidence that Redis is working. This
command asks directly and exits non-zero when it is not, so it can gate
a deploy step rather than just inform one.

    python manage.py check_redis

Also reports connected_clients against maxclients, because this project
runs on Redis Cloud with a hard cap of 30 and prod already sits around
26 -- close enough that a new consumer is a real risk rather than a
theoretical one.
"""

from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.cache_health import DEGRADED, NOT_SHARED, OK, SKIPPED, probe_cache


def _safe_location():
    """REDIS_URL with any password replaced by '***'."""
    raw = ''
    try:
        raw = settings.CACHES['default'].get('LOCATION') or ''
    except Exception:
        return '(unreadable)'
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ''
    try:
        parts = urlparse(raw)
    except ValueError:
        return '(unparseable)'
    host = parts.hostname or '?'
    port = f':{parts.port}' if parts.port else ''
    user = f'{parts.username}:***@' if parts.username or parts.password else ''
    return f'{parts.scheme}://{user}{host}{port}{parts.path}'


class Command(BaseCommand):
    help = 'Verify Redis is reachable and serving as a shared cache.'

    def handle(self, *args, **options):
        backend = settings.CACHES['default'].get('BACKEND', '?')
        self.stdout.write(f'backend  : {backend}')
        self.stdout.write(f'location : {_safe_location()}')

        result = probe_cache(force=True)

        if result == SKIPPED:
            self.stdout.write(self.style.WARNING(
                'SKIPPED — the configured backend is not '
                'ResilientRedisCache, so there is no Redis to check. '
                'This is expected under settings_test/settings_rehearsal '
                'and a problem anywhere else.'))
            return

        if result in (DEGRADED, NOT_SHARED):
            raise CommandError(
                'Redis is NOT serving this process. The cache has fallen '
                'back to a per-process store, so audit-API cache hits '
                'will be unreliable across workers and rate limits are '
                'per worker rather than global.\n'
                'Try:  redis-cli -u "$REDIS_URL" ping')

        self.stdout.write(self.style.SUCCESS(
            'OK — Redis answered and the round-trip read back intact.'))
        self._report_clients()

    def _report_clients(self):
        """connected_clients / maxclients, when the server will tell us."""
        try:
            from django.core.cache import cache

            client = cache._cache.get_client()
            info = client.info('clients')
            connected = info.get('connected_clients')
            maxclients = (client.config_get('maxclients') or {}).get(
                'maxclients')
        except Exception as exc:
            self.stdout.write(
                f'clients  : unavailable ({type(exc).__name__}) — some '
                'managed Redis plans disable INFO/CONFIG')
            return

        line = f'clients  : {connected} connected'
        if maxclients:
            line += f' / {maxclients} max'
        self.stdout.write(line)

        try:
            if maxclients and int(connected) > int(maxclients) * 0.8:
                self.stdout.write(self.style.WARNING(
                    'Over 80% of the client cap is in use. CLAUDE.md '
                    'notes this plan is capped at 30 with prod near 26 — '
                    'do not add another consumer without checking.'))
        except (TypeError, ValueError):
            pass
