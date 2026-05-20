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
    'billing',
    'contracts',
    'outreach',
    'social',
    'reporting',
    'admin_dashboard',
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
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'core' / 'static']
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ── Email — SendGrid SMTP ───────────────────────────────────────────────────
# Per CLAUDE.md: all transactional + outreach mail goes through SendGrid.
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.sendgrid.net'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = 'apikey'
EMAIL_HOST_PASSWORD = env('SENDGRID_API_KEY', '')
DEFAULT_FROM_EMAIL = env(
    'DEFAULT_FROM_EMAIL',
    'Zachery Long <zachery@aspiredwebsites.com>',
)
SENDGRID_API_KEY = env('SENDGRID_API_KEY', '')


# ── Stripe ──────────────────────────────────────────────────────────────────
STRIPE_SECRET_KEY = env('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = env('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET = env('STRIPE_WEBHOOK_SECRET', '')


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


# ── Meta / Facebook ─────────────────────────────────────────────────────────
META_APP_ID = env('META_APP_ID', '')
META_APP_SECRET = env('META_APP_SECRET', '')


# ── LinkedIn ────────────────────────────────────────────────────────────────
LINKEDIN_CLIENT_ID = env('LINKEDIN_CLIENT_ID', '')
LINKEDIN_CLIENT_SECRET = env('LINKEDIN_CLIENT_SECRET', '')


# ── Celery / Redis ──────────────────────────────────────────────────────────
REDIS_URL = env('REDIS_URL', 'redis://localhost:6379/0')
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE


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
