"""
GoHighLevel (GHL) API client — contact upsert for website leads.

Spec: claude-code-website-spec.md, Job 1. The Private Integration Token
(settings.GHL_API_KEY) is a static, non-expiring, sub-account-scoped
secret. It must never reach client-side JS, template context, or logs
— server-side only, same posture as VAULT_SERVER_SECRET in CLAUDE.md.

Division of labour: Django owns spam filtering, validation, and the
Lead record. GHL owns the CRM pipeline once the 'website-lead' tag
lands — that tag is what fires the GHL workflow automation, so no
workflow endpoint needs to be called from here.

Callers MUST catch GHLError and fall back (fallback email +
FailedLeadSubmission) — a GHL outage must never cost a lead.
"""

import logging
import random
import time

import phonenumbers
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10
MAX_ATTEMPTS = 3


class GHLError(Exception):
    """Any failure pushing a contact to GHL — network, 4xx, 5xx, bad JSON."""


class GHLNotConfigured(GHLError):
    """No GHL_API_KEY set. Same posture as InstantlyNotConfigured."""


def _token():
    token = (getattr(settings, 'GHL_API_KEY', '') or '').strip()
    if not token:
        raise GHLNotConfigured('GHL_API_KEY is not set.')
    return token


def to_e164(raw, region='US'):
    """
    Best-effort E.164 conversion (+1XXXXXXXXXX). Returns None — not ''
    — on failure, so callers can tell "couldn't parse" apart from
    "known empty" and fall back to sending the raw value in a custom
    field instead of dropping the lead (spec §1 step 3).
    """
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(str(raw), region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(
        parsed, phonenumbers.PhoneNumberFormat.E164)


def _custom_field_entry(key, value):
    """
    customFields entries use a GHL field id when settings.GHL_CUSTOM_FIELD_IDS
    has one for this key, else fall back to key. The id is the
    documented-reliable route; key works today because the fields
    don't exist in GHL yet (Zachery is creating them and will supply
    ids — see CLAUDE.md / the spec's "Things that will bite you").
    """
    field_ids = getattr(settings, 'GHL_CUSTOM_FIELD_IDS', {}) or {}
    field_id = field_ids.get(key)
    if field_id:
        return {'id': field_id, 'fieldValue': value}
    return {'key': key, 'fieldValue': value}


def build_payload(*, first_name, last_name='', email='', phone_raw='',
                   company_name='', custom_fields=None):
    """
    Build the /contacts/upsert request body. custom_fields is a dict of
    key -> value (strings); order is preserved in the output list.
    """
    custom_fields = dict(custom_fields or {})

    payload = {
        'locationId': getattr(settings, 'GHL_LOCATION_ID', ''),
        'firstName': first_name,
        # tags OVERWRITES the entire tag set on update — send only this
        # one value, ever. See spec "Things that will bite you".
        'tags': ['website-lead'],
        'source': 'website-contact-form',
    }
    if last_name:
        payload['lastName'] = last_name
    if email:
        payload['email'] = email
    if company_name:
        payload['companyName'] = company_name

    if phone_raw:
        e164 = to_e164(phone_raw)
        if e164:
            payload['phone'] = e164
        else:
            # Could not parse — send the contact anyway rather than
            # drop the lead; raw value preserved for manual follow-up.
            custom_fields['phone_raw'] = phone_raw

    payload['customFields'] = [
        _custom_field_entry(key, value)
        for key, value in custom_fields.items()
        if value not in (None, '')
    ]
    return payload


def push_contact(payload):
    """
    POST /contacts/upsert. Retries HTTP 429 honouring Retry-After, with
    jitter, up to MAX_ATTEMPTS. Raises GHLError on any other failure —
    never returns a partial/ambiguous result.
    """
    base = (getattr(settings, 'GHL_API_BASE', '')
            or 'https://services.leadconnectorhq.com').rstrip('/')
    url = f'{base}/contacts/upsert'
    headers = {
        'Authorization': f'Bearer {_token()}',
        'Version': getattr(settings, 'GHL_API_VERSION', '2021-07-28'),
        'Content-Type': 'application/json',
        'Accept': 'application/json',
    }

    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.post(
                url, headers=headers, json=payload, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise GHLError(f'GHL unreachable: {exc}') from exc

        if resp.status_code == 429 and attempt < MAX_ATTEMPTS:
            try:
                retry_after = float(resp.headers.get('Retry-After', 1))
            except ValueError:
                retry_after = 1.0
            logger.info(
                'GHL 429, retrying (attempt=%s retry_after=%.1fs)',
                attempt, retry_after)
            time.sleep(retry_after + random.uniform(0, 1))
            continue

        if resp.status_code >= 400:
            # Response body may echo submitted fields — truncate hard
            # and never log the request payload itself (spec: no PII
            # in logs beyond a correlation id + GHL's traceId).
            detail = resp.text[:300]
            raise GHLError(
                f'GHL POST /contacts/upsert -> HTTP {resp.status_code}: '
                f'{detail}')

        if not resp.content:
            raise GHLError('GHL returned an empty response body.')
        try:
            return resp.json()
        except ValueError as exc:
            raise GHLError(
                f'GHL returned non-JSON: {resp.text[:200]}') from exc


def sync_lead_to_ghl(*, first_name, last_name='', email='', phone_raw='',
                      company_name='', custom_fields=None):
    """
    Build + send the upsert. Returns (contact_id, trace_id) on success.
    Raises GHLError (incl. GHLNotConfigured) on any failure — callers
    must catch and fall back.
    """
    payload = build_payload(
        first_name=first_name, last_name=last_name, email=email,
        phone_raw=phone_raw, company_name=company_name,
        custom_fields=custom_fields,
    )
    try:
        body = push_contact(payload)
    except GHLError as exc:
        # Attach the attempted payload so callers can log it to
        # FailedLeadSubmission without rebuilding it themselves.
        exc.payload = payload
        raise
    contact_id = (body.get('contact') or {}).get('id', '')
    trace_id = body.get('traceId', '')
    logger.info('GHL upsert ok (contact_id=%s traceId=%s)', contact_id, trace_id)
    return contact_id, trace_id
