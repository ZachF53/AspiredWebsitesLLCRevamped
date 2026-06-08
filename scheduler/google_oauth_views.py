"""
Google Calendar OAuth — admin-only connect + callback flow.

Two views:
    /admin-dashboard/schedule/connect/         GET  → kick off OAuth
    /admin-dashboard/schedule/google-callback/ GET  → Google redirects
                                                       back with ?code

Uses direct REST against accounts.google.com / oauth2.googleapis.com
(no `google-auth-oauthlib` dependency). Token + refresh stored in
GoogleCalendarToken — one row per admin.
"""

import datetime as _dt
import logging
import secrets

import requests
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from admin_dashboard.decorators import admin_required

from .models import GoogleCalendarToken

logger = logging.getLogger(__name__)


SCOPES = 'https://www.googleapis.com/auth/calendar'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'


def _redirect_uri(request):
    """The callback URL we send to Google. Must match an Authorized
    redirect URI on the OAuth client."""
    base = getattr(
        settings, 'SITE_BASE_URL', 'https://aspiredwebsites.com')
    return f'{base}/admin-dashboard/schedule/google-callback/'


@admin_required
def connect_page(request):
    """Status + connect button. Shows current connection state if any."""
    token = GoogleCalendarToken.objects.filter(user=request.user).first()
    return render(request, 'scheduler/connect.html', {
        'active': 'schedule',
        'token': token,
        'connected': token is not None,
    })


@admin_required
def start_oauth(request):
    """Kick off the OAuth flow — redirect to Google's consent screen."""
    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '')
    if not client_id:
        messages.error(
            request,
            'GOOGLE_CLIENT_ID is not configured on the server. '
            'Set it in .env and restart gunicorn.')
        return redirect('admin_dashboard:schedule_connect')

    # CSRF-like state so the callback can verify the redirect came from
    # our own flow. Stash in session, verify in callback.
    state = secrets.token_urlsafe(24)
    request.session['google_oauth_state'] = state

    params = {
        'client_id': client_id,
        'redirect_uri': _redirect_uri(request),
        'response_type': 'code',
        'scope': SCOPES,
        'access_type': 'offline',     # so we get a refresh_token
        'prompt': 'consent',          # forces consent screen → guarantees refresh_token
        'state': state,
    }
    qs = '&'.join(f'{k}={requests.utils.quote(str(v), safe="")}'
                  for k, v in params.items())
    return redirect(f'{AUTH_URL}?{qs}')


@admin_required
def oauth_callback(request):
    """Google redirects here with ?code + ?state. Exchange for tokens."""
    state = request.GET.get('state') or ''
    expected = request.session.pop('google_oauth_state', '')
    if not state or state != expected:
        messages.error(
            request,
            'OAuth state mismatch — possible CSRF. Try connecting again.')
        return redirect('admin_dashboard:schedule_connect')

    code = request.GET.get('code') or ''
    if not code:
        err = request.GET.get('error') or 'no code returned'
        messages.error(request, f'Google OAuth failed: {err}')
        return redirect('admin_dashboard:schedule_connect')

    # Exchange the code for tokens
    try:
        r = requests.post(TOKEN_URL, data={
            'code': code,
            'client_id': getattr(settings, 'GOOGLE_CLIENT_ID', ''),
            'client_secret': getattr(
                settings, 'GOOGLE_CLIENT_SECRET', ''),
            'redirect_uri': _redirect_uri(request),
            'grant_type': 'authorization_code',
        }, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.exception('Google OAuth token exchange failed')
        messages.error(request, f'Token exchange failed: {exc}')
        return redirect('admin_dashboard:schedule_connect')

    access_token = payload.get('access_token') or ''
    refresh_token = payload.get('refresh_token') or ''
    expires_in = int(payload.get('expires_in') or 3600)

    if not access_token:
        messages.error(request, 'Google did not return an access token.')
        return redirect('admin_dashboard:schedule_connect')

    # Save or update the token row
    token, _created = GoogleCalendarToken.objects.update_or_create(
        user=request.user,
        defaults={
            'access_token': access_token,
            # Only update refresh_token if Google gave us one — on
            # subsequent re-consents Google may omit it.
            'refresh_token': refresh_token or (
                GoogleCalendarToken.objects
                .filter(user=request.user).values_list(
                    'refresh_token', flat=True).first() or ''),
            'expires_at': timezone.now() + _dt.timedelta(seconds=expires_in - 60),
        },
    )
    messages.success(
        request,
        'Google Calendar connected. Confirmed bookings will sync from '
        'now on.')
    return redirect('admin_dashboard:schedule_connect')


@admin_required
def disconnect(request):
    """Drop the token. Future events stop being pushed."""
    if request.method != 'POST':
        return redirect('admin_dashboard:schedule_connect')
    GoogleCalendarToken.objects.filter(user=request.user).delete()
    messages.info(
        request,
        'Google Calendar disconnected. New bookings will be saved to '
        'the database only.')
    return redirect('admin_dashboard:schedule_connect')
