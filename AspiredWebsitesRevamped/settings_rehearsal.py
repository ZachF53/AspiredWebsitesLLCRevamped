"""Settings for the Account/Website migration rehearsal.

The rehearsal loads production-shaped data and then runs *writing* backfills
against it.  Two things must therefore be impossible:

1. Touching the production database.  ``DATABASE_URL`` from ``.env`` is
   ignored outright — this module always points at a dedicated on-disk
   SQLite file and asserts that fact at import time.
2. Reaching a live third-party service.  Every outbound credential is blanked
   and email/Celery are pinned to in-memory transports, so a signal that fires
   during seeding or backfilling cannot bill a customer, provision a droplet,
   or send mail.

Usage::

    python manage.py migrate --settings=AspiredWebsitesRevamped.settings_rehearsal

The database file defaults to ``db_rehearsal.sqlite3`` beside ``manage.py``
(covered by the ``*.sqlite3`` gitignore rule) and can be relocated with
``ASPIRED_REHEARSAL_DB``.
"""

import os
from pathlib import Path

from .settings import *  # noqa: F401,F403


DEBUG = True

ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'testserver']

# ── Database — never DATABASE_URL, never production ────────────────────────
_REHEARSAL_DB = Path(
    os.getenv('ASPIRED_REHEARSAL_DB')
    or (BASE_DIR / 'db_rehearsal.sqlite3')  # noqa: F405
).resolve()

# A rehearsal must be recognisable as one.  If someone points
# ASPIRED_REHEARSAL_DB at the dev database (or anything else that is not
# clearly a rehearsal file), refuse to start rather than let a destructive
# backfill run against it.
if 'rehearsal' not in _REHEARSAL_DB.name.lower():
    raise RuntimeError(
        'Refusing to start: the rehearsal database filename must contain '
        f'"rehearsal" (got {_REHEARSAL_DB}).')

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': str(_REHEARSAL_DB),
    }
}
REHEARSAL_DATABASE_PATH = str(_REHEARSAL_DB)

# ── Transport security off — this is a local, HTTP-only rehearsal ──────────
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_PROXY_SSL_HEADER = None

# ── No external services ───────────────────────────────────────────────────
# Seeding and the backfills both fire model signals.  Anything that reaches
# out to Stripe, DigitalOcean, SendGrid, Twilio, Namecheap, Google, or
# Anthropic must fail closed on a missing key instead of hitting a real
# account with rehearsal data.
STRIPE_SECRET_KEY = ''
STRIPE_PUBLISHABLE_KEY = ''
STRIPE_WEBHOOK_SECRET = ''
DO_API_TOKEN = ''
SENDGRID_API_KEY = ''
ANTHROPIC_API_KEY = ''
TWILIO_ACCOUNT_SID = ''
TWILIO_AUTH_TOKEN = ''
GOOGLE_PLACES_API_KEY = ''
NAMECHEAP_API_KEY = ''

EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

# Rehearsal credentials are encrypted under a throwaway key.  The real
# VAULT_SERVER_SECRET never touches rehearsal data, and a rehearsal runs on a
# workstation whose .env may not define one at all.
VAULT_SERVER_SECRET = 'rehearsal-only-vault-server-secret-not-production'
MOONIEFUL_SYNC_SECRET = 'rehearsal-only-moonieful-sync-secret'

# Redis must not be required to run a rehearsal.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'aspired-rehearsal-cache',
    },
}
CHANNEL_LAYERS = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
}

# Not eager: a queued task is dropped on the floor rather than executed, so
# `.delay()` calls in signals neither raise nor do real work.
CELERY_TASK_ALWAYS_EAGER = False
CELERY_BROKER_URL = 'memory://'
CELERY_RESULT_BACKEND = 'cache+memory://'
