"""
Phase 5a-pivot — GBP OAuth (manager-invite model).

Operator-level token: one row per agency operator. Clients invite the
operator's Google account as Manager on their GMB; this single token
then lists/reads/updates every client's location.

Three views:
    /admin-dashboard/gbp/connect/                 GET → status + button
    /admin-dashboard/gbp/connect/start/           GET → kick off OAuth
    /admin-dashboard/gbp/oauth/callback/          GET ← Google
    /admin-dashboard/gbp/disconnect/              POST → drop token
"""

import datetime as _dt
import logging
import secrets

import requests
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils import timezone

from admin_dashboard.decorators import admin_required
from reporting.google_gbp import encrypt_token
from reporting.models import GbpOperatorToken

logger = logging.getLogger(__name__)


SCOPES = ' '.join([
    'https://www.googleapis.com/auth/business.manage',
    'https://www.googleapis.com/auth/webmasters.readonly',
    'https://www.googleapis.com/auth/userinfo.email',
])
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
USERINFO_URL = 'https://www.googleapis.com/oauth2/v2/userinfo'

SESSION_STATE_KEY = 'gbp_operator_oauth_state'


def _redirect_uri():
    base = getattr(
        settings, 'SITE_BASE_URL', 'https://aspiredwebsites.com')
    return f'{base}/admin-dashboard/gbp/oauth/callback/'


@admin_required
def connect_page(request):
    """Connection status for the logged-in operator's GBP token."""
    token = GbpOperatorToken.objects.filter(user=request.user).first()
    return render(request, 'reporting/gbp/connect.html', {
        'active_nav': 'gbp',
        'token':      token,
        'connected':  token is not None,
    })


@admin_required
def connect_start(request):
    """Kick off OAuth for THIS operator. State is just a random token —
    no channel binding (the token is operator-level)."""
    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '')
    if not client_id:
        messages.error(
            request,
            'GOOGLE_CLIENT_ID is not configured. Add it to .env and '
            'restart gunicorn.')
        return redirect('gbp:connect_page')

    state_token = secrets.token_urlsafe(24)
    request.session[SESSION_STATE_KEY] = state_token

    params = {
        'client_id':     client_id,
        'redirect_uri':  _redirect_uri(),
        'response_type': 'code',
        'scope':         SCOPES,
        'access_type':   'offline',
        'prompt':        'consent',
        'state':         state_token,
        'include_granted_scopes': 'true',
    }
    qs = '&'.join(f'{k}={requests.utils.quote(str(v), safe="")}'
                  for k, v in params.items())
    return redirect(f'{AUTH_URL}?{qs}')


@admin_required
def oauth_callback(request):
    """Google redirects here after consent."""
    incoming_state = request.GET.get('state') or ''
    expected = request.session.pop(SESSION_STATE_KEY, '')
    if not incoming_state or incoming_state != expected:
        messages.error(
            request,
            'OAuth state mismatch — possible CSRF. Try connecting again.')
        return redirect('gbp:connect_page')

    code = request.GET.get('code') or ''
    if not code:
        err = request.GET.get('error') or 'no code returned'
        messages.error(request, f'Google OAuth failed: {err}')
        return redirect('gbp:connect_page')

    try:
        r = requests.post(TOKEN_URL, data={
            'code':          code,
            'client_id':     getattr(settings, 'GOOGLE_CLIENT_ID', ''),
            'client_secret': getattr(settings, 'GOOGLE_CLIENT_SECRET', ''),
            'redirect_uri':  _redirect_uri(),
            'grant_type':    'authorization_code',
        }, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.exception('GBP OAuth token exchange failed')
        messages.error(request, f'Token exchange failed: {exc}')
        return redirect('gbp:connect_page')

    access_token = payload.get('access_token') or ''
    refresh_token = payload.get('refresh_token') or ''
    expires_in = int(payload.get('expires_in') or 3600)
    granted_scopes = payload.get('scope') or SCOPES

    if not access_token:
        messages.error(request, 'Google did not return an access token.')
        return redirect('gbp:connect_page')

    # Best-effort: fetch the operator's email for display.
    provider_email = ''
    try:
        ur = requests.get(
            USERINFO_URL,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10)
        if ur.status_code == 200:
            provider_email = (ur.json().get('email') or '')[:255]
    except Exception:
        logger.exception('GBP userinfo fetch failed (non-fatal)')

    # Encrypt + persist. Refresh-token preservation on re-consent:
    # Google omits refresh_token on subsequent grants, so keep the
    # existing encrypted one when the new payload doesn't include it.
    existing = GbpOperatorToken.objects.filter(user=request.user).first()
    try:
        new_access = encrypt_token(access_token)
        new_refresh = encrypt_token(refresh_token) if refresh_token else (
            existing.refresh_token_encrypted if existing else '')
    except RuntimeError as exc:
        messages.error(request, str(exc))
        return redirect('gbp:connect_page')

    GbpOperatorToken.objects.update_or_create(
        user=request.user,
        defaults={
            'access_token_encrypted':  new_access,
            'refresh_token_encrypted': new_refresh,
            'expires_at':              timezone.now() + _dt.timedelta(
                seconds=max(60, expires_in - 60)),
            'scopes':                  granted_scopes[:500],
            'provider_account_email':  provider_email,
            'last_refresh_at':         timezone.now(),
            'last_refresh_error':      '',
        },
    )

    messages.success(
        request,
        f'Google Business Profile connected as {provider_email or "your account"}. '
        'Every GBP a client has invited you to manage is now visible.')
    return redirect('gbp:connect_page')


@admin_required
def disconnect(request):
    """Drop the operator's token. POST only."""
    if request.method != 'POST':
        return redirect('gbp:connect_page')
    GbpOperatorToken.objects.filter(user=request.user).delete()
    messages.info(
        request,
        'Disconnected. NAP sync, review monitoring, and performance '
        'pulls are paused until you re-connect.')
    return redirect('gbp:connect_page')
