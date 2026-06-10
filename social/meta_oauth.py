"""
Phase 5b — Meta (Facebook + Instagram) OAuth + token helpers.

Meta's flow:
  1. Redirect operator (acting on behalf of one SocialChannel) to
     facebook.com OAuth dialog with our app id, redirect_uri, state,
     and the scopes we need to publish on the client's behalf.
  2. Meta redirects to our callback with ?code.
  3. We exchange the short-lived user token for a long-lived (60-day)
     user token via /oauth/access_token?grant_type=fb_exchange_token.
  4. We call /me/accounts to list the Pages the operator manages —
     each Page comes with its own permanent Page Access Token.
  5. Operator picks a Page (matches the SocialChannel.handle in the UI).
  6. (Optional) call /<page_id>?fields=instagram_business_account to
     resolve the IG Business Account id linked to that Page.
  7. Persist {page_id, ig_business_id} on the SocialChannel; encrypted
     page-access-token on the SocialToken (no refresh token — Meta page
     tokens don't expire as long as the user token stays valid, which
     we refresh every 50 days via tasks.refresh_expiring_tokens).

Scopes:
  - pages_manage_posts        — post to a Page feed
  - pages_read_engagement     — read insights (Phase 5b deferred — kept for forward-compat)
  - pages_show_list           — enumerate Pages on /me/accounts
  - instagram_basic           — read IG business account info
  - instagram_content_publish — publish IG image / video posts
  - business_management       — required by Meta for Business-asset reads

NOTE: business_management + instagram_content_publish are "advanced"
permissions — they require Meta App Review before production use.
Until your app is approved, only users explicitly added as
testers/developers in the App Dashboard can complete the flow.
"""

import datetime as _dt
import logging
import secrets

import requests
from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from admin_dashboard.decorators import admin_required
from clients.service_models import SocialChannel

from .crypto import encrypt_token
from .models import SocialToken

logger = logging.getLogger(__name__)


AUTH_URL = 'https://www.facebook.com/v19.0/dialog/oauth'
TOKEN_URL = 'https://graph.facebook.com/v19.0/oauth/access_token'
ACCOUNTS_URL = 'https://graph.facebook.com/v19.0/me/accounts'
PAGE_URL = 'https://graph.facebook.com/v19.0/{page_id}'

# Single comma-separated scope list — Meta accepts this format.
META_SCOPES = ','.join([
    'pages_manage_posts',
    'pages_read_engagement',
    'pages_show_list',
    'instagram_basic',
    'instagram_content_publish',
    'business_management',
])


def _redirect_uri(request):
    base = getattr(settings, 'SITE_BASE_URL', 'https://aspiredwebsites.com')
    return f'{base}/admin-dashboard/social/meta/callback/'


@admin_required
def connect_start(request, channel_id):
    """Operator clicks Connect on a SocialChannel — kick off Meta OAuth."""
    channel = get_object_or_404(
        SocialChannel, id=channel_id,
        platform__in=('facebook', 'instagram'),
    )
    app_id = getattr(settings, 'META_APP_ID', '')
    if not app_id:
        messages.error(
            request,
            'META_APP_ID is not configured on the server. '
            'Set it in .env and restart gunicorn.')
        return redirect('social:channels_list')

    state = secrets.token_urlsafe(24)
    # Embed the channel_id in the state cookie so the callback can bind
    # the resulting token to the right SocialChannel.
    request.session['social_meta_oauth_state'] = f'{state}|{channel.id}'

    params = {
        'client_id': app_id,
        'redirect_uri': _redirect_uri(request),
        'response_type': 'code',
        'scope': META_SCOPES,
        'state': state,
    }
    qs = '&'.join(f'{k}={requests.utils.quote(str(v), safe="")}'
                  for k, v in params.items())
    return redirect(f'{AUTH_URL}?{qs}')


@admin_required
def oauth_callback(request):
    """Meta redirects here with ?code + ?state. Exchange + select Page."""
    state = request.GET.get('state') or ''
    raw_expected = request.session.pop('social_meta_oauth_state', '')
    expected_state, _, channel_id = raw_expected.partition('|')
    if not state or state != expected_state or not channel_id:
        messages.error(
            request,
            'OAuth state mismatch — possible CSRF. Try connecting again.')
        return redirect('social:channels_list')

    channel = SocialChannel.objects.filter(id=channel_id).first()
    if channel is None:
        messages.error(request, 'Channel no longer exists.')
        return redirect('social:channels_list')

    code = request.GET.get('code') or ''
    if not code:
        err = request.GET.get('error') or 'no code returned'
        messages.error(request, f'Meta OAuth failed: {err}')
        return redirect('social:connect_page', channel_id=channel.id)

    # 1. Exchange code → short-lived user token
    try:
        r = requests.get(TOKEN_URL, params={
            'client_id': getattr(settings, 'META_APP_ID', ''),
            'client_secret': getattr(settings, 'META_APP_SECRET', ''),
            'redirect_uri': _redirect_uri(request),
            'code': code,
        }, timeout=15)
        r.raise_for_status()
        short_payload = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.exception('Meta OAuth code exchange failed')
        messages.error(request, f'Token exchange failed: {exc}')
        return redirect('social:connect_page', channel_id=channel.id)

    short_token = short_payload.get('access_token') or ''
    if not short_token:
        messages.error(request, 'Meta did not return a user access token.')
        return redirect('social:connect_page', channel_id=channel.id)

    # 2. Exchange short → long-lived user token (60-day)
    try:
        r = requests.get(TOKEN_URL, params={
            'grant_type': 'fb_exchange_token',
            'client_id': getattr(settings, 'META_APP_ID', ''),
            'client_secret': getattr(settings, 'META_APP_SECRET', ''),
            'fb_exchange_token': short_token,
        }, timeout=15)
        r.raise_for_status()
        long_payload = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.exception('Meta long-lived exchange failed')
        messages.error(request, f'Long-lived token exchange failed: {exc}')
        return redirect('social:connect_page', channel_id=channel.id)

    long_token = long_payload.get('access_token') or ''
    expires_in = int(long_payload.get('expires_in') or 60 * 24 * 3600)
    if not long_token:
        messages.error(request, 'Meta did not return a long-lived token.')
        return redirect('social:connect_page', channel_id=channel.id)

    # 3. Fetch the Pages the operator manages
    try:
        r = requests.get(ACCOUNTS_URL, params={
            'access_token': long_token,
            'fields': 'id,name,access_token,instagram_business_account',
        }, timeout=15)
        r.raise_for_status()
        pages = r.json().get('data') or []
    except Exception as exc:  # noqa: BLE001
        logger.exception('Meta /me/accounts failed')
        messages.error(request, f'Could not list Pages: {exc}')
        return redirect('social:connect_page', channel_id=channel.id)

    if not pages:
        messages.error(
            request,
            'This Meta account does not manage any Pages. Connect with '
            'an account that has admin access to the client Page.')
        return redirect('social:connect_page', channel_id=channel.id)

    # Stash everything in the session so the picker view can show + persist
    request.session['social_meta_oauth_user_token'] = long_token
    request.session['social_meta_oauth_user_expires_in'] = expires_in
    request.session['social_meta_oauth_channel_id'] = str(channel.id)
    request.session['social_meta_oauth_pages'] = [{
        'id': p.get('id'),
        'name': p.get('name'),
        'access_token': p.get('access_token'),
        'ig_business_id': (p.get('instagram_business_account') or {}).get('id'),
    } for p in pages]

    return redirect('social:meta_page_picker', channel_id=channel.id)


@admin_required
def page_picker(request, channel_id):
    """Operator selects which Page to bind to this SocialChannel.

    Most agency operators have access to many client Pages; the channel
    has to be bound to exactly one.
    """
    channel = get_object_or_404(SocialChannel, id=channel_id)
    pages = request.session.get('social_meta_oauth_pages') or []
    if not pages:
        messages.error(
            request,
            'OAuth session expired — please connect again.')
        return redirect('social:connect_page', channel_id=channel.id)

    if request.method == 'POST':
        page_id = request.POST.get('page_id') or ''
        chosen = next((p for p in pages if p.get('id') == page_id), None)
        if chosen is None:
            messages.error(request, 'Please pick a Page from the list.')
            return render(request, 'social/meta_page_picker.html', {
                'channel': channel,
                'pages': pages,
                'active': 'social',
            })

        # For Instagram channels, require the Page to be linked to an
        # IG Business account — otherwise we can't publish to IG.
        if channel.platform == 'instagram' and not chosen.get('ig_business_id'):
            messages.error(
                request,
                'This Page is not linked to an Instagram Business '
                'account. Convert the IG account to Business and link '
                'it to the Page before connecting.')
            return render(request, 'social/meta_page_picker.html', {
                'channel': channel,
                'pages': pages,
                'active': 'social',
            })

        # Persist: Page Access Token (which is what we use to publish)
        # encrypted; expiry tracked from the user token's expires_in
        # since the page token follows the user token's lifecycle.
        expires_in = int(request.session.get(
            'social_meta_oauth_user_expires_in') or 60 * 24 * 3600)
        provider_id = chosen.get('id') or ''
        if channel.platform == 'instagram':
            provider_id = chosen.get('ig_business_id') or provider_id

        token, _created = SocialToken.objects.update_or_create(
            channel=channel,
            defaults={
                'access_token_encrypted': encrypt_token(
                    chosen.get('access_token') or ''),
                'refresh_token_encrypted': encrypt_token(
                    request.session.get(
                        'social_meta_oauth_user_token') or ''),
                'expires_at': timezone.now() + _dt.timedelta(
                    seconds=expires_in - 86400),  # refresh a day early
                'scopes': META_SCOPES,
                'provider_account_id': provider_id,
                'connected_by': request.user,
                'last_refresh_at': timezone.now(),
                'last_refresh_error': '',
            },
        )

        # Clean up session
        for k in (
            'social_meta_oauth_user_token',
            'social_meta_oauth_user_expires_in',
            'social_meta_oauth_channel_id',
            'social_meta_oauth_pages',
        ):
            request.session.pop(k, None)

        messages.success(
            request,
            f'{channel.get_platform_display()} connected — '
            f'{chosen.get("name")}.')
        return redirect('social:connect_page', channel_id=channel.id)

    return render(request, 'social/meta_page_picker.html', {
        'channel': channel,
        'pages': pages,
        'active': 'social',
    })


@admin_required
def disconnect(request, channel_id):
    """Drop the token. Existing scheduled posts on this channel will fail."""
    if request.method != 'POST':
        return redirect('social:connect_page', channel_id=channel_id)
    channel = get_object_or_404(SocialChannel, id=channel_id)
    SocialToken.objects.filter(channel=channel).delete()
    messages.info(
        request,
        f'{channel.get_platform_display()} disconnected.')
    return redirect('social:connect_page', channel_id=channel.id)
