"""Deterministic settings for Django's test runner.

Tests must never inherit HTTPS, Redis, SMTP, PostgreSQL, or Channels behavior
from the operator's current ``.env``.  Integration tests that need one of
those services opt in explicitly with ``override_settings`` or a dedicated
test command.
"""

import tempfile
from pathlib import Path

from .settings import *  # noqa: F401,F403


DEBUG = False

ALLOWED_HOSTS = ['testserver', 'localhost', '127.0.0.1']
PRODUCTION_HOST = 'aspiredwebsites.com'
SITE_BASE_URL = 'https://aspiredwebsites.com'

# Do not inherit or write to production filesystem paths from .env.  Keeping
# the TemporaryDirectory object alive at module scope preserves the paths for
# the test process and removes them when that process exits.
_TEST_ARTIFACTS = tempfile.TemporaryDirectory(
    prefix='aspiredwebsites-tests-')
_TEST_ARTIFACT_ROOT = Path(_TEST_ARTIFACTS.name)
STATIC_ROOT = _TEST_ARTIFACT_ROOT / 'static'
MEDIA_ROOT = _TEST_ARTIFACT_ROOT / 'media'
STATIC_ROOT.mkdir()
MEDIA_ROOT.mkdir()

SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_PROXY_SSL_HEADER = None

# Never connect to a database named by DATABASE_URL while running tests.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    },
}

# Unit and app tests must not require Redis to be running.  Redis outage and
# cross-worker rate-limit behavior belong in explicit integration tests.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'aspired-test-cache',
    },
}
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    },
}

EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = 'memory://'
CELERY_RESULT_BACKEND = 'cache+memory://'
