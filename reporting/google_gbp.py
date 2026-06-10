"""
Phase 5a-pivot — Google Business Profile API client.

Manager-invite architecture: a single agency-operator token covers
every client whose GMB has invited the operator as Manager. The
operator's GbpOperatorToken is loaded once per task; all per-client
work goes through it.

Token storage:
    Server-key encrypted via vault.crypto.derive_server_key(). Celery
    background tasks can decrypt without an admin PIN session.

What still works programmatically via this scope:
    ✓ Account + location enumeration
    ✓ Location read/update (NAP, hours, categories)
    ✓ Reviews — read + reply
    ✓ Performance metrics (impressions, clicks, calls)

What does NOT work (deprecated by Google in late 2023):
    ✗ Local posts (create / read / delete)
    ✗ Insights v4
"""

import datetime as _dt
import logging

import requests
from django.conf import settings
from django.utils import timezone

from vault.crypto import decrypt_value, derive_server_key, encrypt_value

logger = logging.getLogger(__name__)


TOKEN_URL = 'https://oauth2.googleapis.com/token'

# Three split APIs replaced the old v4. Each has its own base host.
ACCOUNTS_API = 'https://mybusinessaccountmanagement.googleapis.com/v1'
INFO_API = 'https://mybusinessbusinessinformation.googleapis.com/v1'
# Performance API still uses the same root as Business Profile.
PERFORMANCE_API = 'https://businessprofileperformance.googleapis.com/v1'
# Reviews still live under the original mybusiness.googleapis.com/v4
# host (Google kept this endpoint working even after retiring posts).
LEGACY_API = 'https://mybusiness.googleapis.com/v4'


# ─────────────────────────────────────────────────────────────────────────────
# Token encrypt / decrypt helpers
# ─────────────────────────────────────────────────────────────────────────────

def encrypt_token(plaintext: str) -> str:
    """Server-key-encrypted hex. Raises RuntimeError when
    VAULT_SERVER_SECRET is unset so the OAuth callback can surface a
    friendly error before persisting a garbage row."""
    if not plaintext:
        return ''
    try:
        key = derive_server_key()
    except ValueError as exc:
        raise RuntimeError(
            'VAULT_SERVER_SECRET is not configured on this server. '
            'Add it to .env and restart gunicorn before connecting '
            'Google Business Profile.') from exc
    return encrypt_value(plaintext, key)


def decrypt_token(ciphertext_hex: str) -> str:
    """Decrypt → '' on any failure (no oracle)."""
    if not ciphertext_hex:
        return ''
    try:
        key = derive_server_key()
    except ValueError:
        return ''
    plain = decrypt_value(ciphertext_hex, key)
    if plain == '[decryption failed]':
        return ''
    return plain


# ─────────────────────────────────────────────────────────────────────────────
# Token refresh
# ─────────────────────────────────────────────────────────────────────────────

def refresh_if_needed(token):
    """If the access token is expired (or about to be), refresh it.
    Mutates `token` IN PLACE and saves. Returns the same `token`."""
    if token.expires_at and token.expires_at > (
            timezone.now() + _dt.timedelta(seconds=30)):
        return token

    refresh_token = decrypt_token(token.refresh_token_encrypted)
    if not refresh_token:
        token.last_refresh_error = 'no refresh_token on file'
        token.save(update_fields=['last_refresh_error', 'updated_at'])
        raise RuntimeError(
            'No refresh token on file for operator — must re-connect '
            'Google Business Profile.')

    try:
        r = requests.post(TOKEN_URL, data={
            'client_id':     getattr(settings, 'GOOGLE_CLIENT_ID', ''),
            'client_secret': getattr(settings, 'GOOGLE_CLIENT_SECRET', ''),
            'refresh_token': refresh_token,
            'grant_type':    'refresh_token',
        }, timeout=15)
    except requests.RequestException as exc:
        token.last_refresh_error = f'network: {exc}'[:500]
        token.save(update_fields=['last_refresh_error', 'updated_at'])
        raise RuntimeError(f'GBP token refresh network error: {exc}')

    if r.status_code != 200:
        snippet = r.text[:300]
        token.last_refresh_error = (
            f'refresh {r.status_code}: {snippet}')[:500]
        token.save(update_fields=['last_refresh_error', 'updated_at'])
        raise RuntimeError(
            f'GBP token refresh failed: {r.status_code} {snippet}')

    payload = r.json()
    new_access = payload.get('access_token') or ''
    if not new_access:
        token.last_refresh_error = 'refresh: empty access_token'
        token.save(update_fields=['last_refresh_error', 'updated_at'])
        raise RuntimeError('GBP refresh returned no access_token.')

    token.access_token_encrypted = encrypt_token(new_access)
    expires_in = int(payload.get('expires_in') or 3600)
    token.expires_at = timezone.now() + _dt.timedelta(
        seconds=max(60, expires_in - 60))
    token.last_refresh_at = timezone.now()
    token.last_refresh_error = ''
    token.save(update_fields=[
        'access_token_encrypted', 'expires_at',
        'last_refresh_at', 'last_refresh_error', 'updated_at',
    ])
    return token


def _auth_header(token):
    plain = decrypt_token(token.access_token_encrypted)
    if not plain:
        raise RuntimeError(
            'GBP access token could not be decrypted '
            '(VAULT_SERVER_SECRET issue?). Operator must re-connect.')
    return {'Authorization': f'Bearer {plain}'}


# ─────────────────────────────────────────────────────────────────────────────
# Read — locations
# ─────────────────────────────────────────────────────────────────────────────

def list_locations(token):
    """All GBP locations the operator has access to (own + Manager-invites).

    Returns [{'name': 'accounts/.../locations/...', 'title': str,
              'address_lines': [...], 'phone_numbers': [...]}].
    """
    refresh_if_needed(token)
    headers = _auth_header(token)

    accounts_r = requests.get(
        f'{ACCOUNTS_API}/accounts', headers=headers, timeout=15)
    accounts_r.raise_for_status()
    out = []
    for account in (accounts_r.json().get('accounts') or []):
        acc_name = account.get('name') or ''  # accounts/<id>
        if not acc_name:
            continue
        try:
            loc_r = requests.get(
                f'{INFO_API}/{acc_name}/locations',
                params={'readMask': ('name,title,phoneNumbers,'
                                     'storefrontAddress,websiteUri')},
                headers=headers, timeout=15)
            loc_r.raise_for_status()
        except Exception:
            logger.exception(
                'GBP list_locations: per-account fetch failed for %s',
                acc_name)
            continue
        for loc in (loc_r.json().get('locations') or []):
            sa = loc.get('storefrontAddress') or {}
            phones = loc.get('phoneNumbers') or {}
            primary_phone = phones.get('primaryPhone') or ''
            out.append({
                'name':          loc.get('name') or '',
                'title':         loc.get('title') or 'untitled',
                'address_lines': sa.get('addressLines') or [],
                'locality':      sa.get('locality') or '',
                'region':        sa.get('administrativeArea') or '',
                'postal_code':   sa.get('postalCode') or '',
                'phone':         primary_phone,
                'website':       loc.get('websiteUri') or '',
            })
    return out


def fetch_location(token, location_name):
    """Read one location's NAP fields. Returns a dict matching the
    list_locations() shape or None on failure."""
    refresh_if_needed(token)
    try:
        r = requests.get(
            f'{INFO_API}/{location_name}',
            params={'readMask': ('name,title,phoneNumbers,'
                                 'storefrontAddress,websiteUri,'
                                 'regularHours')},
            headers=_auth_header(token), timeout=15)
        r.raise_for_status()
    except Exception:
        logger.exception(
            'fetch_location failed for %s', location_name)
        return None
    return r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Read — reviews
# ─────────────────────────────────────────────────────────────────────────────

def list_reviews(token, location_name, page_size=50):
    """All reviews for a location, newest first. Returns list of dicts
    matching Google's review shape — see GbpReview model for the
    fields we persist."""
    refresh_if_needed(token)
    headers = _auth_header(token)
    out = []
    page_token = None
    safety = 0
    while True:
        params = {'pageSize': page_size}
        if page_token:
            params['pageToken'] = page_token
        try:
            r = requests.get(
                f'{LEGACY_API}/{location_name}/reviews',
                params=params, headers=headers, timeout=20)
            r.raise_for_status()
        except Exception:
            logger.exception(
                'list_reviews failed at page %s', safety)
            break
        payload = r.json()
        for rev in (payload.get('reviews') or []):
            out.append(rev)
        page_token = payload.get('nextPageToken')
        if not page_token:
            break
        safety += 1
        if safety > 20:  # 1000 reviews cap
            logger.warning(
                'list_reviews: safety break after %s pages', safety)
            break
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Read — performance
# ─────────────────────────────────────────────────────────────────────────────

# DAILY_METRIC enum values we pull. See Google's
# `dailyMetric` enum docs for the full list.
DAILY_METRICS = [
    'BUSINESS_IMPRESSIONS_DESKTOP_SEARCH',
    'BUSINESS_IMPRESSIONS_MOBILE_SEARCH',
    'BUSINESS_IMPRESSIONS_DESKTOP_MAPS',
    'BUSINESS_IMPRESSIONS_MOBILE_MAPS',
    'CALL_CLICKS',
    'WEBSITE_CLICKS',
    'BUSINESS_DIRECTION_REQUESTS',
]


def fetch_monthly_performance(token, location_name, year, month):
    """Sum daily metrics for the given calendar month. Returns a dict
    of {metric: total} ready to write into GbpPerformanceSnapshot."""
    import calendar
    refresh_if_needed(token)
    headers = _auth_header(token)

    last_day = calendar.monthrange(year, month)[1]
    params = [
        ('dailyRange.start_date.year', year),
        ('dailyRange.start_date.month', month),
        ('dailyRange.start_date.day', 1),
        ('dailyRange.end_date.year', year),
        ('dailyRange.end_date.month', month),
        ('dailyRange.end_date.day', last_day),
    ]
    for m in DAILY_METRICS:
        params.append(('dailyMetrics', m))

    try:
        r = requests.get(
            f'{PERFORMANCE_API}/{location_name}:fetchMultiDailyMetricsTimeSeries',
            params=params, headers=headers, timeout=20)
        r.raise_for_status()
    except Exception:
        logger.exception(
            'fetch_monthly_performance failed for %s', location_name)
        return {}

    payload = r.json()
    totals = {m: 0 for m in DAILY_METRICS}
    for series in (payload.get('multiDailyMetricTimeSeries') or []):
        for ts in (series.get('dailyMetricTimeSeries') or []):
            metric = ts.get('dailyMetric') or ''
            if metric not in totals:
                continue
            for day in ((ts.get('timeSeries') or {}).get(
                    'datedValues') or []):
                value = int(day.get('value') or 0)
                totals[metric] = totals[metric] + value
    totals['_raw_payload'] = payload
    return totals
