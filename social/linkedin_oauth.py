"""
Phase 5c — LinkedIn OAuth + organization picker.

LinkedIn's flow:
  1. Redirect operator to linkedin.com/oauth/v2/authorization with our
     client_id, redirect_uri, state, and scope list.
  2. LinkedIn redirects back with ?code.
  3. POST to /oauth/v2/accessToken to get a 2-month access token.
     LinkedIn also returns a refresh_token if your app has the
     "Marketing Developer Platform" product (otherwise you re-do
     OAuth every 60 days).
  4. Call /v2/me to identify the user, then /v2/organizationAcls?q=
     roleAssignee&projection=(elements*(organization~(...)))
     to enumerate the Organization Pages the user can post to.
  5. Operator picks an org; we persist its URN (urn:li:organization:NNN)
     as SocialToken.provider_account_id.

Scopes:
  - w_organization_social   — post on behalf of an Organization
  - r_organization_social   — read org posts (Phase 5b deferred)
  - rw_organization_admin   — needed by some accounts to enumerate
  - r_basicprofile          — /v2/me readout for connection display

NOTE: w_organization_social requires LinkedIn Marketing Developer
Platform approval, which is a manual process — until approved, only
testers added in the LinkedIn Developer console can complete the flow.
"""

import datetime as _dt
import logging
import secrets

import requests
from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from admin_dashboard.decorators import admin_required
from clients.service_models import SocialChannel

from .crypto import encrypt_token
from .models import SocialToken

logger = logging.getLogger(__name__)


AUTH_URL = 'https://www.linkedin.com/oauth/v2/authorization'
TOKEN_URL = 'https://www.linkedin.com/oauth/v2/accessToken'
ME_URL = 'https://api.linkedin.com/v2/me'
ORGS_URL = 'https://api.linkedin.com/v2/organizationAcls'

# LinkedIn requires space-separated scopes (unlike Meta's comma format).
LINKEDIN_SCOPES = ' '.join([
    'w_organization_social',
    'r_organization_social',
    'rw_organization_admin',
    'r_basicprofile',
])


def _redirect_uri(request):
    base = getattr(settings, 'SITE_BASE_URL', 'https://aspiredwebsites.com')
    return f'{base}/admin-dashboard/social/linkedin/callback/'


@admin_required
def connect_start(request, channel_id):
    """Kick off LinkedIn OAuth for a specific SocialChannel."""
    channel = get_object_or_404(
        SocialChannel, id=channel_id, platform='linkedin',
    )
    client_id = getattr(settings, 'LINKEDIN_CLIENT_ID', '')
    if not client_id:
        messages.error(
            request,
            'LINKEDIN_CLIENT_ID is not configured on the server. '
            'Set it in .env and restart gunicorn.')
        return redirect('social:channels_list')

    state = secrets.token_urlsafe(24)
    request.session['social_linkedin_oauth_state'] = (
        f'{state}|{channel.id}')

    params = {
        'response_type': 'code',
        'client_id': client_id,
        'redirect_uri': _redirect_uri(request),
        'state': state,
        'scope': LINKEDIN_SCOPES,
    }
    qs = '&'.join(f'{k}={requests.utils.quote(str(v), safe="")}'
                  for k, v in params.items())
    return redirect(f'{AUTH_URL}?{qs}')


@admin_required
def oauth_callback(request):
    """LinkedIn redirects here with ?code + ?state."""
    state = request.GET.get('state') or ''
    raw = request.session.pop('social_linkedin_oauth_state', '')
    expected, _, channel_id = raw.partition('|')
    if not state or state != expected or not channel_id:
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
        err = request.GET.get('error_description') or request.GET.get(
            'error') or 'no code returned'
        messages.error(request, f'LinkedIn OAuth failed: {err}')
        return redirect('social:connect_page', channel_id=channel.id)

    # Exchange code for access token
    try:
        r = requests.post(TOKEN_URL, data={
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': _redirect_uri(request),
            'client_id': getattr(settings, 'LINKEDIN_CLIENT_ID', ''),
            'client_secret': getattr(settings, 'LINKEDIN_CLIENT_SECRET', ''),
        }, headers={
            'Content-Type': 'application/x-www-form-urlencoded',
        }, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.exception('LinkedIn token exchange failed')
        messages.error(request, f'Token exchange failed: {exc}')
        return redirect('social:connect_page', channel_id=channel.id)

    access_token = payload.get('access_token') or ''
    expires_in = int(payload.get('expires_in') or 60 * 24 * 3600)
    refresh_token = payload.get('refresh_token') or ''  # may be empty
    if not access_token:
        messages.error(request, 'LinkedIn did not return an access token.')
        return redirect('social:connect_page', channel_id=channel.id)

    # Enumerate organizations the user has access to
    try:
        r = requests.get(
            ORGS_URL,
            params={
                'q': 'roleAssignee',
                'role': 'ADMINISTRATOR',
                'state': 'APPROVED',
                'projection': '(elements*(organization~(id,localizedName,vanityName)))',
            },
            headers={
                'Authorization': f'Bearer {access_token}',
                'X-Restli-Protocol-Version': '2.0.0',
            }, timeout=15)
        r.raise_for_status()
        raw_elements = (r.json().get('elements') or [])
    except Exception as exc:  # noqa: BLE001
        logger.exception('LinkedIn org list failed')
        messages.error(request, f'Could not list LinkedIn orgs: {exc}')
        return redirect('social:connect_page', channel_id=channel.id)

    orgs = []
    for el in raw_elements:
        org = el.get('organization~') or {}
        org_id = org.get('id')
        if org_id is None:
            continue
        orgs.append({
            'urn': f'urn:li:organization:{org_id}',
            'name': org.get('localizedName')
                    or org.get('vanityName')
                    or f'Organization {org_id}',
        })

    if not orgs:
        messages.error(
            request,
            'This LinkedIn account is not an Administrator of any '
            'organization. Posting requires an admin role.')
        return redirect('social:connect_page', channel_id=channel.id)

    # Stash for picker
    request.session['social_linkedin_oauth_token'] = access_token
    request.session['social_linkedin_oauth_refresh'] = refresh_token
    request.session['social_linkedin_oauth_expires_in'] = expires_in
    request.session['social_linkedin_oauth_orgs'] = orgs
    return redirect('social:linkedin_org_picker', channel_id=channel.id)


@admin_required
def org_picker(request, channel_id):
    """Pick which LinkedIn org page to bind this SocialChannel to."""
    channel = get_object_or_404(SocialChannel, id=channel_id)
    orgs = request.session.get('social_linkedin_oauth_orgs') or []
    if not orgs:
        messages.error(
            request,
            'OAuth session expired — please connect again.')
        return redirect('social:connect_page', channel_id=channel.id)

    if request.method == 'POST':
        chosen_urn = request.POST.get('org_urn') or ''
        chosen = next((o for o in orgs if o['urn'] == chosen_urn), None)
        if chosen is None:
            messages.error(request, 'Please pick an organization.')
            return render(request, 'social/linkedin_org_picker.html', {
                'channel': channel,
                'orgs': orgs,
                'active': 'social',
            })

        expires_in = int(request.session.get(
            'social_linkedin_oauth_expires_in') or 60 * 24 * 3600)
        SocialToken.objects.update_or_create(
            channel=channel,
            defaults={
                'access_token_encrypted': encrypt_token(
                    request.session.get(
                        'social_linkedin_oauth_token') or ''),
                'refresh_token_encrypted': encrypt_token(
                    request.session.get(
                        'social_linkedin_oauth_refresh') or ''),
                'expires_at': timezone.now() + _dt.timedelta(
                    seconds=expires_in - 86400),
                'scopes': LINKEDIN_SCOPES,
                'provider_account_id': chosen['urn'],
                'connected_by': request.user,
                'last_refresh_at': timezone.now(),
                'last_refresh_error': '',
            },
        )

        for k in (
            'social_linkedin_oauth_token',
            'social_linkedin_oauth_refresh',
            'social_linkedin_oauth_expires_in',
            'social_linkedin_oauth_orgs',
        ):
            request.session.pop(k, None)

        messages.success(
            request,
            f'LinkedIn connected — {chosen["name"]}.')
        return redirect('social:connect_page', channel_id=channel.id)

    return render(request, 'social/linkedin_org_picker.html', {
        'channel': channel,
        'orgs': orgs,
        'active': 'social',
    })


@admin_required
def disconnect(request, channel_id):
    """Drop the LinkedIn token."""
    if request.method != 'POST':
        return redirect('social:connect_page', channel_id=channel_id)
    channel = get_object_or_404(SocialChannel, id=channel_id)
    SocialToken.objects.filter(channel=channel).delete()
    messages.info(request, 'LinkedIn disconnected.')
    return redirect('social:connect_page', channel_id=channel.id)
