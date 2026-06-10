"""
Phase 5a — Google Business Profile OAuth.

Three views:
    /admin-dashboard/social/<channel_id>/connect/   GET → start
    /admin-dashboard/social/oauth/google/callback/  GET → Google → us
    /admin-dashboard/social/<channel_id>/disconnect/ POST → drop token

Same shape as scheduler/google_oauth_views.py (which connects the
admin's Google Calendar), but per-CHANNEL (one client → many connected
provider accounts) rather than per-admin-user.

Three scopes requested up front so Phase 6 doesn't need re-consent:
  - business.manage       — posting (5a)
  - webmasters.readonly   — Search Console keyword tracking (Phase 6)
  - userinfo.email        — display name on the connect page

Token storage: server-key encrypted via social.crypto.
"""

import datetime as _dt
import logging
import secrets

import requests
from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone

from admin_dashboard.decorators import admin_required
from clients.service_models import SocialChannel
from social.crypto import encrypt_token
from social.models import SocialToken

logger = logging.getLogger(__name__)


SCOPES = ' '.join([
    'https://www.googleapis.com/auth/business.manage',
    'https://www.googleapis.com/auth/webmasters.readonly',
    'https://www.googleapis.com/auth/userinfo.email',
])
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
USERINFO_URL = 'https://www.googleapis.com/oauth2/v2/userinfo'

SESSION_STATE_KEY = 'social_gbp_oauth_state'


def _redirect_uri():
    """Callback URL we send to Google. Must match an Authorized
    redirect URI on the OAuth client in Google Cloud Console."""
    base = getattr(
        settings, 'SITE_BASE_URL', 'https://aspiredwebsites.com')
    return f'{base}/admin-dashboard/social/oauth/google/callback/'


@admin_required
def connect_start(request, channel_id):
    """Kick off OAuth for a specific SocialChannel. We pack the
    channel_id into the state token so the callback knows which
    channel to bind the resulting token to."""
    channel = get_object_or_404(SocialChannel, id=channel_id)
    if channel.platform != 'gbp':
        messages.error(
            request,
            f'Channel platform is "{channel.platform}", not Google '
            'Business Profile. Use the matching connect flow for that '
            'platform.')
        return redirect('admin_dashboard:home')

    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '')
    if not client_id:
        messages.error(
            request,
            'GOOGLE_CLIENT_ID is not configured. Add it to .env and '
            'restart gunicorn before connecting.')
        return redirect('social:connect_page', channel_id=channel.id)

    # state = random || channel_id — the callback verifies both.
    state_token = secrets.token_urlsafe(24)
    request.session[SESSION_STATE_KEY] = f'{state_token}|{channel.id}'

    params = {
        'client_id':     client_id,
        'redirect_uri':  _redirect_uri(),
        'response_type': 'code',
        'scope':         SCOPES,
        'access_type':   'offline',   # we want a refresh_token
        'prompt':        'consent',   # forces consent — guarantees refresh_token
        'state':         state_token,
        'include_granted_scopes': 'true',
    }
    qs = '&'.join(f'{k}={requests.utils.quote(str(v), safe="")}'
                  for k, v in params.items())
    return redirect(f'{AUTH_URL}?{qs}')


@admin_required
def oauth_callback(request):
    """Google redirects here with ?code + ?state. Verify state →
    exchange code for tokens → encrypt + persist."""
    incoming_state = request.GET.get('state') or ''
    stored = request.session.pop(SESSION_STATE_KEY, '')
    if not incoming_state or not stored:
        messages.error(
            request, 'OAuth state missing — try connecting again.')
        return redirect('admin_dashboard:home')

    # stored format: '<state>|<channel_id>'
    try:
        expected_state, channel_id = stored.split('|', 1)
    except ValueError:
        messages.error(
            request, 'OAuth state malformed — try connecting again.')
        return redirect('admin_dashboard:home')

    if incoming_state != expected_state:
        messages.error(
            request,
            'OAuth state mismatch — possible CSRF. Try connecting again.')
        return redirect('admin_dashboard:home')

    channel = SocialChannel.objects.filter(id=channel_id).first()
    if channel is None:
        messages.error(
            request,
            'Channel no longer exists — re-open the client and try again.')
        return redirect('admin_dashboard:home')

    code = request.GET.get('code') or ''
    if not code:
        err = request.GET.get('error') or 'no code returned'
        messages.error(request, f'Google OAuth failed: {err}')
        return redirect('social:connect_page', channel_id=channel.id)

    # Exchange the code for tokens.
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
        return redirect('social:connect_page', channel_id=channel.id)

    access_token = payload.get('access_token') or ''
    refresh_token = payload.get('refresh_token') or ''
    expires_in = int(payload.get('expires_in') or 3600)
    granted_scopes = payload.get('scope') or SCOPES

    if not access_token:
        messages.error(request, 'Google did not return an access token.')
        return redirect('social:connect_page', channel_id=channel.id)

    # Best-effort: fetch the Google account email for display. Failure
    # here is non-fatal — we still save the token.
    provider_account_id = ''
    try:
        ur = requests.get(
            USERINFO_URL,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10)
        if ur.status_code == 200:
            provider_account_id = (ur.json().get('email') or '')[:200]
    except Exception:
        logger.exception('GBP userinfo fetch failed (non-fatal)')

    # Encrypt + persist. If we already have a token row for this channel
    # we update in place. Refresh token preservation: Google omits the
    # refresh_token on subsequent consents, so if `refresh_token` is
    # empty we keep the existing encrypted one.
    existing = SocialToken.objects.filter(channel=channel).first()
    try:
        new_access = encrypt_token(access_token)
        if refresh_token:
            new_refresh = encrypt_token(refresh_token)
        else:
            new_refresh = existing.refresh_token_encrypted if existing else ''
    except RuntimeError as exc:
        # encrypt_token raises if VAULT_SERVER_SECRET is unset.
        messages.error(request, str(exc))
        return redirect('social:connect_page', channel_id=channel.id)

    defaults = {
        'access_token_encrypted':  new_access,
        'refresh_token_encrypted': new_refresh,
        'expires_at':              timezone.now() + _dt.timedelta(
            seconds=max(60, expires_in - 60)),
        'scopes':                  granted_scopes[:500],
        'provider_account_id':     provider_account_id,
        'connected_by':            request.user,
        'last_refresh_at':         timezone.now(),
        'last_refresh_error':      '',
    }
    SocialToken.objects.update_or_create(
        channel=channel, defaults=defaults,
    )

    messages.success(
        request,
        f'Google Business Profile connected for {channel.handle}. '
        'Future scheduled posts will publish automatically.')
    return redirect('social:connect_page', channel_id=channel.id)


@admin_required
def disconnect(request, channel_id):
    """Drop the token row for this channel. POST only."""
    if request.method != 'POST':
        return redirect('social:connect_page', channel_id=channel_id)
    channel = get_object_or_404(SocialChannel, id=channel_id)
    SocialToken.objects.filter(channel=channel).delete()
    messages.info(
        request,
        f'Disconnected Google Business Profile for {channel.handle}. '
        'New posts will not publish until you reconnect.')
    return redirect('social:connect_page', channel_id=channel.id)
