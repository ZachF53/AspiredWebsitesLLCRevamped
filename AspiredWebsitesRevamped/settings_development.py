"""Local development settings.

This module deliberately overrides production-only transport security so a
developer's ``.env`` cannot make ``runserver`` redirect every request to
HTTPS.  Production entry points use ``settings_production`` instead.
"""

from .settings import *  # noqa: F401,F403


DEBUG = True

SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_PROXY_SSL_HEADER = None

# Keep the standard local hosts available even when ALLOWED_HOSTS is supplied
# by a production-oriented .env file on the same workstation.
ALLOWED_HOSTS = list(dict.fromkeys([
    *ALLOWED_HOSTS,  # noqa: F405
    'localhost',
    '127.0.0.1',
    'testserver',
]))
