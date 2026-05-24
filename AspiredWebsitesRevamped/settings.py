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
    # 'daphne' must precede staticfiles so runserver serves ASGI in dev.
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Phase 7 — intcomma / naturalday filters in the BI dashboard.
    'django.contrib.humanize',
]

THIRD_PARTY_APPS = [
    'django_htmx',
    'channels',
    # TOTP is handled directly with pyotp — django_otp is intentionally not
    # installed. The otp_totp tables from an earlier migration are left
    # dormant on existing databases (not reversed).
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
                # Exposes STATIC_VERSION (git short SHA) for cache-busting
                # static asset URLs in base templates.
                'core.context_processors.static_version',
            ],
        },
    },
]

WSGI_APPLICATION = 'AspiredWebsitesRevamped.wsgi.application'
ASGI_APPLICATION = 'AspiredWebsitesRevamped.asgi.application'


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

# Cache-buster for static assets — appended to <link>/<script> URLs as
# `?v={{ STATIC_VERSION }}` by the base templates. Derived from the
# current git short SHA so every deploy yields a new value, forcing
# browsers to re-fetch CSS/JS even if the underlying URL is unhashed
# (the manifest storage above is currently not producing hashed
# filenames in prod, and WhiteNoise's `Cache-Control: immutable`
# header makes plain URLs stick in the browser cache for 30 days).
# Fallback to a build-time timestamp if git isn't on PATH or this
# isn't a git checkout (Docker images, tarball deploys).
def _static_version():
    import subprocess
    from datetime import datetime
    try:
        sha = subprocess.check_output(
            ['git', '-C', str(BASE_DIR), 'rev-parse', '--short', 'HEAD'],
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip()
        if sha:
            return sha
    except Exception:
        pass
    return datetime.utcnow().strftime('%Y%m%d%H%M%S')

STATIC_VERSION = _static_version()


# ── Logging ─────────────────────────────────────────────────────────────────
# Django's default LOGGING with DEBUG=False routes django.request errors to
# `mail_admins` only — so unless ADMINS is set + email works, every 500 just
# disappears. We explicitly route the django.request logger (the one that
# fires for unhandled view exceptions) to stderr AND a dedicated file so
# tracebacks always land somewhere greppable.
import os as _os
_DJANGO_LOG = _os.path.join(
    str(BASE_DIR.parent), 'logs', 'django-error.log',
) if (BASE_DIR.parent / 'logs').exists() else None

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': (
                '[{asctime}] {levelname} {name} {process:d}/{thread:d}\n'
                '  {message}\n'
            ),
            'style': '{',
        },
    },
    'handlers': {
        # Stderr handler — picked up by gunicorn's --error-logfile.
        'console': {
            'class': 'logging.StreamHandler',
            'level': 'INFO',
            'formatter': 'verbose',
        },
        # Optional file handler — only attached when the prod logs dir
        # exists (it does on the DO Droplet; doesn't on local dev).
        **({
            'django_error_file': {
                'class': 'logging.handlers.RotatingFileHandler',
                'level': 'WARNING',
                'formatter': 'verbose',
                'filename': _DJANGO_LOG,
                'maxBytes': 10 * 1024 * 1024,    # 10 MB
                'backupCount': 5,
            },
        } if _DJANGO_LOG else {}),
    },
    'loggers': {
        # The logger Django emits unhandled-view-exception tracebacks
        # to. Default propagation chain caps at level INFO via the root
        # logger, so we override here.
        'django.request': {
            'handlers': (
                ['console', 'django_error_file']
                if _DJANGO_LOG else ['console']
            ),
            'level': 'WARNING',
            'propagate': False,
        },
        # Our own app loggers — anything that calls
        # `logger = logging.getLogger(__name__)` inherits this.
        'clients': {
            'handlers': ['console'] + (
                ['django_error_file'] if _DJANGO_LOG else []),
            'level': 'INFO',
            'propagate': False,
        },
        'billing': {
            'handlers': ['console'] + (
                ['django_error_file'] if _DJANGO_LOG else []),
            'level': 'INFO',
            'propagate': False,
        },
        'reporting': {
            'handlers': ['console'] + (
                ['django_error_file'] if _DJANGO_LOG else []),
            'level': 'INFO',
            'propagate': False,
        },
        'admin_dashboard': {
            'handlers': ['console'] + (
                ['django_error_file'] if _DJANGO_LOG else []),
            'level': 'INFO',
            'propagate': False,
        },
    },
}

MEDIA_URL = 'media/'
MEDIA_ROOT = env('MEDIA_ROOT') or (BASE_DIR / 'media')

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ── Email — SendGrid SMTP ───────────────────────────────────────────────────
# Per CLAUDE.md: all transactional + outreach mail goes through SendGrid.
SENDGRID_API_KEY = env('SENDGRID_API_KEY', '')
# Custom SMTP backend that auto-appends the legal address footer
# (8735 Dunwoody Place, Ste R, Atlanta GA 30350) to every outgoing
# email. Subclasses Django's SMTP backend; passes everything else
# through unchanged. Direct SendGrid SDK callers (PDF-attachment
# paths) bypass this and use core.email_signature.append_signature
# explicitly for the same result.
EMAIL_BACKEND = 'core.email_backend.AspiredEmailBackend'
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


# ── Secret keys with split ownership ────────────────────────────────────────
# MOONIEFUL_SYNC_SECRET — HMAC for the Moonieful ↔ Aspired sync bridge.
# Must match the value on Miki's server.
MOONIEFUL_SYNC_SECRET = env('MOONIEFUL_SYNC_SECRET', '')

# VAULT_SERVER_SECRET — seeds the AES-256 key used to encrypt SSH credentials
# during automated Droplet provisioning, before any admin has unlocked the
# vault. Never shared. Rotating it makes credentials with
# encrypted_with_server_key=True unrecoverable — see CLAUDE.md.
VAULT_SERVER_SECRET = env('VAULT_SERVER_SECRET', '')

# Legacy alias — kept so a stale .env still boots during the split rollout.
# Falls back to MOONIEFUL_SYNC_SECRET so old code paths still resolve.
# Remove after both new vars are confirmed everywhere.
SYNC_SECRET = env('SYNC_SECRET', default=MOONIEFUL_SYNC_SECRET)

MOONIEFUL_SYNC_URL = env('MOONIEFUL_SYNC_URL', '')
SITE_BASE_URL = env('SITE_BASE_URL', 'https://aspiredwebsites.com')


# ── Celery / Redis ──────────────────────────────────────────────────────────
REDIS_URL = env('REDIS_URL', 'redis://localhost:6379/0')

# Redis-backed cache — shared across every Gunicorn worker, so django-ratelimit
# enforces ONE global limit on /api/track/. A per-process LocMemCache would
# multiply the intended limit by the worker count.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL,
    }
}

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
    'generate-monthly-reports': {
        'task': 'reporting.tasks.send_monthly_reports',
        'schedule': crontab(hour=7, minute=0, day_of_month=1),  # 1st, 7am
    },
    'generate-freshness-reports': {
        'task': 'reporting.tasks.generate_freshness_reports',
        'schedule': crontab(hour=6, minute=0, day_of_month=1,
                            month_of_year='1,4,7,10'),          # quarterly
    },
    'send-nps-surveys': {
        'task': 'reporting.tasks.send_nps_surveys',
        'schedule': crontab(hour=10, minute=0, day_of_week=1),  # Mon 10am
    },
    'send-testimonial-requests': {
        'task': 'reporting.tasks.send_testimonial_requests',
        'schedule': crontab(hour=10, minute=0, day_of_month=15),  # 15th, 10am
    },
    'check-scan-schedule': {
        'task': 'reporting.tasks.check_scan_schedule',
        'schedule': crontab(hour=3, minute=0),                    # daily 3am
    },
    'calculate-health-scores': {
        'task': 'clients.tasks.calculate_all_health_scores',
        'schedule': crontab(hour=6, minute=0),                    # daily 6am
    },
    'monthly-revenue-snapshot': {
        'task': 'clients.tasks.take_monthly_revenue_snapshot',
        'schedule': crontab(hour=1, minute=0, day_of_month=1),    # 1st 1am
    },
    'check-case-study-prompts': {
        'task': 'clients.tasks.check_case_study_prompts',
        'schedule': crontab(hour=8, minute=30),                   # daily 8:30am
    },
    'expire-old-proposals': {
        'task': 'clients.tasks.expire_old_proposals',
        'schedule': crontab(hour=2, minute=0),                    # daily 2am
    },
    'run-monthly-intelligence': {
        'task': 'clients.tasks.run_monthly_intelligence',
        'schedule': crontab(hour=8, minute=0, day_of_month=15),   # 15th 8am
    },
    'check-annual-report-schedule': {
        'task': 'clients.tasks.check_annual_report_schedule',
        'schedule': crontab(hour=9, minute=0, day_of_month=1),    # 1st 9am
    },
    'run-monthly-competitor-gaps': {
        'task': 'clients.tasks.run_monthly_competitor_gaps',
        'schedule': crontab(hour=10, minute=0, day_of_month=20),  # 20th 10am
    },
    'delete-expired-recordings': {
        'task': 'reporting.tasks.delete_expired_recordings',
        'schedule': crontab(hour=2, minute=0),                    # daily 2am
    },
    'recording-storage-report': {
        'task': 'reporting.tasks.recording_storage_report',
        'schedule': crontab(hour=8, minute=0, day_of_week=1),     # Mon 8am
    },
    # Onboarding reminders — every 12h. The task itself debounces per
    # client (24h between setup nudges, 48h between intake nudges) so a
    # cadence faster than the per-client throttle is fine here.
    'send-onboarding-reminders': {
        'task': 'clients.tasks.send_onboarding_reminders',
        'schedule': crontab(hour='*/12', minute=0),
    },
}

# ── Channels (WebSocket / ASGI) ─────────────────────────────────────────────
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [REDIS_URL],
        },
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
# local_settings.py is gitignored — it holds MOONIEFUL_SYNC_SECRET,
# VAULT_SERVER_SECRET, and any machine- or
# environment-specific overrides. Imported last so it wins.
try:
    from local_settings import *  # noqa: F401,F403
except ImportError:
    pass
