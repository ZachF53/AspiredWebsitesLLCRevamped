"""
Local development settings — never touches the shared Redis.

    python manage.py runserver --settings=AspiredWebsitesRevamped.settings_local
    python manage.py shell    --settings=AspiredWebsitesRevamped.settings_local

WHY THIS EXISTS
---------------
`.env`'s REDIS_URL points at a Redis Cloud instance with a HARD
``maxclients`` of 30, and **production shares it**. Measured 2026-10-10:

    celery-worker   17        <- prod
    daphne           3        <- prod
    celery-beat      3        <- prod
    gunicorn x3      3        <- prod
    runserver x2     2        <- a developer's laptop
    ad-hoc shell     1        <- a developer's laptop
    --------------------
    total           29 / 30

Production's own stack sits at 26 of 30. A laptop running `runserver`
plus one `manage.py shell` is enough to exhaust the pool, and when it
does the failure lands on PROD: every `.delay()` raises

    kombu.exceptions.OperationalError: max number of clients reached

That is what silently stopped lead enrichment from queuing on
2026-10-10 — 148 leads imported and not one enrichment task ran.

A local dev box must not be able to do that to production, so this
module removes the shared broker from the local path entirely:

  * ``CELERY_TASK_ALWAYS_EAGER`` — ``.delay()`` runs the task inline, in
    process, with no broker at all. Zero Redis connections, and it also
    makes enrichment actually run locally instead of vanishing into a
    queue no local worker is consuming.
  * ``LocMemCache`` — per-process cache. Rate limits become per-process
    rather than global, which is wrong for production and completely
    fine for one developer.
  * ``REDIS_URL`` pointed at localhost so anything that reads it
    directly fails fast and loudly instead of reaching for prod.

This mirrors what ``settings_test`` already does; dev simply never got
the same treatment.
"""

from .settings import *  # noqa: F401,F403

# Run tasks inline. No broker, no connection, no queue to miss.
CELERY_TASK_ALWAYS_EAGER = True
# Surface task exceptions in the caller rather than swallowing them into
# a result nobody reads.
CELERY_TASK_EAGER_PROPAGATES = True

# Per-process cache. Never the shared one.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'aspired-local',
    }
}

# Anything still reaching for Redis should hit a closed local port and
# fail immediately, rather than quietly consuming one of prod's 30 slots.
REDIS_URL = 'redis://127.0.0.1:6379/0'
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL

# Channels (the SSH terminal) uses an in-memory layer locally for the
# same reason. Terminal sessions do not survive a reload here; that is
# an acceptable trade for not holding shared connections open.
CHANNEL_LAYERS = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
}

DEBUG = True
