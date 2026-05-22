"""
Django settings for AspiredWebsitesRevamped project.

Aspired Websites LLC — custom web design agency operating system.
All secrets are read from .env via python-dotenv. Never hardcode credentials.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ── Paths & .env ────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')


def env(key, default=None):
    """Read an env var, treating empty strings as missing."""
    value = os.getenv(key, default)
    if value == '':
        return default
    return value


def env_bool(key, default=False):
    value = os.getenv(key)
    if value is None or value == '':
        return default
    return value.lower() in ('1', 'true', 'yes', 'on')


def env_list(key, default=None):
    value = os.getenv(key, '')
    if not value:
        return default or []
    return [item.strip() for item in value.split(',') if item.strip()]


# ── Core Django ─────────────────────────────────────────────────────────────
SECRET_KEY = env(
    'SECRET_KEY',
    'django-insecure-dev-only-replace-me-in-production-via-env',
)

DEBUG = env_bool('DEBUG', True)

ALLOWED_HOSTS = env_list(
    'ALLOWED_HOSTS',
    ['localhost', '127.0.0.1'],
)

# Auth — single custom login at /login/ handles both clients (Phase 3 portal)
# and admins (redirected to /admin-dashboard/ post-login if is_staff).
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/portal/'
LOGOUT_REDIRECT_URL = '/'


# ── Applications ────────────────────────────────────────────────────────────
DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_PARTY_APPS = [
    'django_htmx',
]

LOCAL_APPS = [
    'core',
    'public',
    'clients',
    'sync',
    'billing',
    'contracts',
    'outreach',
    'social',
    'reporting',
    'admin_dashboard',
    'vault',
    'counselsouth',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS


# ── Middleware ──────────────────────────────────────────────────────────────
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django_htmx.middleware.HtmxMiddleware',
    'core.middleware.SecurityHeadersMiddleware',
]

ROOT_URLCONF = 'AspiredWebsitesRevamped.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'core' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'AspiredWebsitesRevamped.wsgi.application'


# ── Database ────────────────────────────────────────────────────────────────
# SQLite in development, PostgreSQL in production via DATABASE_URL.
DATABASE_URL = env('DATABASE_URL')

if DATABASE_URL:
    # Minimal postgres://user:pass@host:port/dbname parser to avoid an extra dep.
    from urllib.parse import urlparse
    parsed = urlparse(DATABASE_URL)
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': parsed.path.lstrip('/'),
            'USER': parsed.username or '',
            'PASSWORD': parsed.password or '',
            'HOST': parsed.hostname or '',
            'PORT': str(parsed.port) if parsed.port else '',
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


# ── Password validation ─────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# ── Internationalization ────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'America/Chicago'
USE_I18N = True
USE_TZ = True


# ── Static & Media ──────────────────────────────────────────────────────────
STATIC_URL = 'static/'
# STATIC_ROOT / MEDIA_ROOT default to the project dir for local dev, but can
# be overridden in .env so production can point them where Nginx serves from
# (e.g. /var/www/aspired/static and /var/www/aspired/media).
STATIC_ROOT = env('STATIC_ROOT') or (BASE_DIR / 'staticfiles')
# No STATICFILES_DIRS: `core` is an installed app, so AppDirectoriesFinder
# already collects core/static/. Listing it here as well made the finders
# scan that folder twice — collectstatic flagged every file as a duplicate.
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_URL = 'media/'
MEDIA_ROOT = env('MEDIA_ROOT') or (BASE_DIR / 'media')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ── Email — SendGrid SMTP ───────────────────────────────────────────────────
# Per CLAUDE.md: all transactional + outreach mail goes through SendGrid.
SENDGRID_API_KEY = env('SENDGRID_API_KEY', '')
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.sendgrid.net'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = 'apikey'
EMAIL_HOST_PASSWORD = SENDGRID_API_KEY

# Default sender, plus role-based addresses used throughout the project.
# All four are sub-addresses of the verified aspiredwebsites.com domain.
DEFAULT_FROM_EMAIL = 'Zachery Long <zacherylong@aspiredwebsites.com>'
EMAIL_FROM_MAIN = 'Zachery Long <zacherylong@aspiredwebsites.com>'
EMAIL_FROM_CONTACT = 'Aspired Websites <contact@aspiredwebsites.com>'
EMAIL_FROM_NO_REPLY = 'Aspired Websites <no-reply@aspiredwebsites.com>'
EMAIL_FROM_PASSWORD_RESET = 'Aspired Websites <password-reset@aspiredwebsites.com>'

# Where new-lead notifications are delivered.
LEAD_NOTIFICATION_EMAIL = env(
    'LEAD_NOTIFICATION_EMAIL',
    'zacherylong@aspiredwebsites.com',
)


# ── Stripe ──────────────────────────────────────────────────────────────────
STRIPE_SECRET_KEY = env('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = env('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET = env('STRIPE_WEBHOOK_SECRET', '')

# Stripe Price IDs now live in the database (billing.ServiceTier.stripe_price_id),
# managed at /admin-dashboard/pricing/. The STRIPE_PRICE_* vars in .env are
# legacy seed values only — see `seed_pricing` and `sync_stripe_products`.


# ── DigitalOcean ────────────────────────────────────────────────────────────
DO_API_TOKEN = env('DO_API_TOKEN', '')
DO_BASE_SNAPSHOT_ID = env('DO_BASE_SNAPSHOT_ID', '')


# ── Anthropic Claude ────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = env('ANTHROPIC_API_KEY', '')


# ── Twilio ──────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = env('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = env('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE_NUMBER = env('TWILIO_PHONE_NUMBER', '')


# ── Google APIs ─────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID = env('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = env('GOOGLE_CLIENT_SECRET', '')
# PageSpeed Insights — Google now requires an API key even for low-volume
# use. Free tier is 25k queries/day. Create at:
#   https://console.cloud.google.com/apis/credentials
GOOGLE_PAGESPEED_API_KEY = env('GOOGLE_PAGESPEED_API_KEY', '')
# Places API — powers the Google Maps lead scraper. See CLAUDE.md →
# External APIs & Costs.
GOOGLE_PLACES_API_KEY = env('GOOGLE_PLACES_API_KEY', '')


# ── Meta / Facebook ─────────────────────────────────────────────────────────
META_APP_ID = env('META_APP_ID', '')
META_APP_SECRET = env('META_APP_SECRET', '')


# ── LinkedIn ────────────────────────────────────────────────────────────────
LINKEDIN_CLIENT_ID = env('LINKEDIN_CLIENT_ID', '')
LINKEDIN_CLIENT_SECRET = env('LINKEDIN_CLIENT_SECRET', '')


# ── Moonieful sync bridge ───────────────────────────────────────────────────
# SYNC_SECRET is the shared HMAC key — set it in local_settings.py (gitignored).
SYNC_SECRET = env('SYNC_SECRET', '')
MOONIEFUL_SYNC_URL = env('MOONIEFUL_SYNC_URL', '')
SITE_BASE_URL = env('SITE_BASE_URL', 'https://aspiredwebsites.com')


# ── Celery / Redis ──────────────────────────────────────────────────────────
REDIS_URL = env('REDIS_URL', 'redis://localhost:6379/0')
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE

# ── Celery beat schedule ────────────────────────────────────────────────────
from celery.schedules import crontab  # noqa: E402

CELERY_BEAT_SCHEDULE = {
    'check-client-uptime': {
        'task': 'reporting.tasks.check_client_uptime',
        'schedule': crontab(minute='*/5'),          # every 5 minutes
    },
    'check-gbp-sync': {
        'task': 'reporting.tasks.check_gbp_sync',
        'schedule': crontab(hour=9, minute=0, day_of_week=1),   # Mon 9am
    },
    'check-keyword-ranks': {
        'task': 'reporting.tasks.check_keyword_ranks',
        'schedule': crontab(hour=7, minute=0, day_of_week=1),   # Mon 7am
    },
    'check-conversion-drops': {
        'task': 'reporting.tasks.check_conversion_drops',
        'schedule': crontab(hour=8, minute=0, day_of_month=2),  # 2nd, 8am
    },
}


# ── Security Headers (CLAUDE.md non-negotiables) ────────────────────────────
# Applied in production only when DEBUG is False.
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = 'DENY'
SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')


# ── Local overrides ─────────────────────────────────────────────────────────
# local_settings.py is gitignored — it holds SYNC_SECRET and any machine- or
# environment-specific overrides. Imported last so it wins.
try:
    from local_settings import *  # noqa: F401,F403
except ImportError:
    pass
