"""
Phase 5a — Google Business Profile publisher.

Three public functions:
    _refresh_if_needed(token)
        Refresh access token if expires_at is past (or about to be).
        Updates the SocialToken row IN PLACE with new encrypted
        access token + expires_at. Preserves the refresh token.
        Raises RuntimeError when no refresh token exists (caller
        must surface "operator needs to re-connect").

    list_locations(token)
        GET /v4/accounts/<acc>/locations — returns list of
        {name, locationName} dicts. The `name` field is the GBP
        resource name like 'accounts/123/locations/456' which the
        create_local_post call needs as its location_name arg.

    create_local_post(token, location_name, body) -> (id, permalink)
        POST a local post (the "What's new" type). Returns the
        provider post id + a permalink for the result row.

Errors during refresh / publish raise; the Celery task layer catches
them, records SystemAlert, and writes a PostResult(success=False).

Token-shape contract (matches social.models.SocialToken):
    - token.access_token_encrypted, token.refresh_token_encrypted
    - token.expires_at, token.provider_account_id
We always go through social.crypto for decrypt so a rotated
VAULT_SERVER_SECRET produces an empty string + RuntimeError, not a
silent partial publish.
"""

import datetime as _dt
import logging

import requests
from django.conf import settings
from django.utils import timezone

from social.crypto import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)


TOKEN_URL = 'https://oauth2.googleapis.com/token'
# v4 Business Information API — the public posting + read endpoints
# Aspired Websites uses. There's a newer Business Profile API but
# the v4 endpoints are still served and required for posting.
GBP_API_BASE = 'https://mybusiness.googleapis.com/v4'


# ─────────────────────────────────────────────────────────────────────────────
# Token refresh
# ─────────────────────────────────────────────────────────────────────────────

def _refresh_if_needed(token):
    """If the access token is expired (or about to be), refresh it.
    Mutates `token` IN PLACE and saves. Returns the same `token`."""
    # 30s buffer — give us a window to actually use the token after
    # the refresh decision.
    if token.expires_at and token.expires_at > (
            timezone.now() + _dt.timedelta(seconds=30)):
        return token

    refresh_token = decrypt_token(token.refresh_token_encrypted)
    if not refresh_token:
        token.last_refresh_error = 'no refresh_token on file'
        token.save(update_fields=['last_refresh_error', 'updated_at'])
        raise RuntimeError(
            'No refresh token on file for this channel — operator '
            'must re-connect Google Business Profile.')

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
    """List locations across all Google accounts the OAuth subject
    has access to. The connect-page UI uses this so the operator can
    pick which location a SocialChannel maps to.

    Returns a list of {'name': 'accounts/.../locations/...',
                       'title': str}.
    """
    _refresh_if_needed(token)
    headers = _auth_header(token)

    accounts_r = requests.get(
        f'{GBP_API_BASE}/accounts', headers=headers, timeout=15)
    accounts_r.raise_for_status()
    out = []
    for account in (accounts_r.json().get('accounts') or []):
        acc_name = account.get('name') or ''  # accounts/<id>
        if not acc_name:
            continue
        try:
            loc_r = requests.get(
                f'{GBP_API_BASE}/{acc_name}/locations',
                headers=headers, timeout=15)
            loc_r.raise_for_status()
        except Exception:
            logger.exception('GBP list_locations: per-account fetch failed')
            continue
        for loc in (loc_r.json().get('locations') or []):
            out.append({
                'name':  loc.get('name') or '',  # accounts/.../locations/...
                'title': (loc.get('title') or
                          loc.get('locationName') or 'untitled'),
            })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Write — local posts
# ─────────────────────────────────────────────────────────────────────────────

# GBP local-post body cap. Validated client-side too via the composer
# form, but we re-check here so a stale form can't slip through.
GBP_POST_MAX_CHARS = 1500


def create_local_post(token, location_name, body):
    """Create a "What's new" type local post on the given GBP location.

    location_name shape: 'accounts/<acc>/locations/<loc>' — exactly the
    value returned by list_locations()[<i>]['name'].

    Returns (provider_post_id, permalink). The provider_post_id is the
    short id at the end of the localPosts resource name; permalink is
    the Google-served URL we can show the operator.

    Raises:
        ValueError      body too long.
        RuntimeError    refresh / publish failed.
    """
    if not body:
        raise ValueError('Empty body — refuse to publish blank GBP post.')
    if len(body) > GBP_POST_MAX_CHARS:
        raise ValueError(
            f'Body too long: {len(body)} chars > {GBP_POST_MAX_CHARS} max.')

    _refresh_if_needed(token)
    headers = _auth_header(token)
    headers['Content-Type'] = 'application/json'

    payload = {
        'languageCode': 'en-US',
        'summary':      body,
        'topicType':    'STANDARD',
    }
    url = f'{GBP_API_BASE}/{location_name}/localPosts'
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20)
    except requests.RequestException as exc:
        raise RuntimeError(f'GBP publish network error: {exc}')
    if r.status_code not in (200, 201):
        snippet = r.text[:500]
        raise RuntimeError(
            f'GBP publish failed: {r.status_code} {snippet}')

    data = r.json()
    # Full resource name comes back as
    # 'accounts/.../locations/.../localPosts/<id>'
    full_name = data.get('name') or ''
    provider_id = full_name.rsplit('/', 1)[-1] if full_name else ''
    # `searchUrl` is the published permalink Google serves; if missing,
    # fall back to a constructed deep-link to the post resource.
    permalink = data.get('searchUrl') or ''
    return provider_id, permalink
