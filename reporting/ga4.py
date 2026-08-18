"""
GA4 auto-provisioning.

On intake completion we create a Google Analytics 4 property + web data
stream for the client's domain under the AGENCY's GA account, using the
operator's Google token (the same GbpOperatorToken used for GBP, with the
analytics.edit / analytics.manage.users scopes added). The Measurement ID
(G-XXXXXXX) is stored on the Website so the build picks it up; the client
is granted access to the property best-effort.

Reuses reporting.google_gbp's token refresh + auth header so there's a
single Google-token code path. Never raises — provisioning failures log and
return None so intake/build flow is never blocked.
"""

import logging

import requests
from django.conf import settings
from django.utils import timezone

from reporting.google_gbp import _auth_header, refresh_if_needed

logger = logging.getLogger(__name__)

ADMIN_BASE = 'https://analyticsadmin.googleapis.com/v1beta'


def _operator_token():
    from reporting.models import GbpOperatorToken
    return GbpOperatorToken.objects.order_by('created_at').first()


def _client_domain(website):
    """The site's own URL. It used to fall back to the legacy profile's
    `website` column for rows the backfill had not reached; the parity
    gate reports no gaps on that column now."""
    domain = (website.url or '').strip()
    if not domain:
        return ''
    if not domain.startswith(('http://', 'https://')):
        domain = 'https://' + domain
    return domain


def provision_ga4_for_website(website):
    """Create a GA4 property + web stream for ``website`` and store the
    Measurement ID on it. Idempotent (skips if already provisioned).
    Returns the measurement id, or None when skipped/failed."""
    if website.ga4_measurement_id:
        return website.ga4_measurement_id

    account_id = (getattr(settings, 'GA4_ACCOUNT_ID', '') or '').strip()
    if not account_id:
        logger.info('GA4: GA4_ACCOUNT_ID not set — skipping provisioning')
        return None

    token = _operator_token()
    if token is None:
        logger.info('GA4: no operator Google token — skipping provisioning')
        return None
    try:
        refresh_if_needed(token)
        headers = _auth_header(token)
    except Exception:
        logger.exception('GA4: token refresh/auth failed')
        return None
    headers['Content-Type'] = 'application/json'

    display_name = (website.name
                    or (website.account.name if website.account else '')
                    or 'Website')[:100]

    # 1) Create the property under the agency GA account.
    try:
        r = requests.post(
            f'{ADMIN_BASE}/properties', headers=headers, timeout=30,
            json={
                'parent': f'accounts/{account_id}',
                'displayName': display_name,
                'timeZone': getattr(settings, 'TIME_ZONE', 'America/Chicago'),
                'currencyCode': 'USD',
            })
        r.raise_for_status()
        property_name = r.json().get('name', '')  # 'properties/123456789'
    except Exception:
        logger.exception(
            'GA4: property create failed for website %s', website.pk)
        return None
    if not property_name:
        return None

    # 2) Create the web data stream → yields the Measurement ID.
    measurement_id = ''
    stream_name = ''
    default_uri = _client_domain(website) or 'https://example.com'
    try:
        r = requests.post(
            f'{ADMIN_BASE}/{property_name}/dataStreams',
            headers=headers, timeout=30,
            json={
                'type': 'WEB_DATA_STREAM',
                'displayName': f'{display_name} — Web'[:255],
                'webStreamData': {'defaultUri': default_uri},
            })
        r.raise_for_status()
        stream = r.json()
        stream_name = stream.get('name', '')
        measurement_id = (
            stream.get('webStreamData', {}) or {}).get('measurementId', '')
    except Exception:
        logger.exception(
            'GA4: data stream create failed for %s', property_name)

    website.ga4_property_id = property_name
    website.ga4_stream_id = stream_name
    website.ga4_measurement_id = measurement_id
    website.ga4_provisioned_at = timezone.now()
    website.save(update_fields=[
        'ga4_property_id', 'ga4_stream_id', 'ga4_measurement_id',
        'ga4_provisioned_at', 'updated_at'])

    # 3) Grant the client access to their property (best-effort).
    from clients.display import owner_recipient

    client_email = owner_recipient(website)[0]
    if client_email:
        try:
            requests.post(
                f'{ADMIN_BASE}/{property_name}/accessBindings',
                headers=headers, timeout=30,
                json={'user': client_email,
                      'roles': ['predefinedRoles/editor']})
        except Exception:
            logger.exception(
                'GA4: grant access to %s failed (non-fatal)', client_email)

    logger.info(
        'GA4: provisioned %s (%s) for website %s',
        property_name, measurement_id, website.pk)
    return measurement_id
