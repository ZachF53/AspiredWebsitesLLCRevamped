"""
GoHighLevel (GHL) API client — contact upsert for website leads.

Spec: claude-code-website-spec.md, Job 1 (contact form) and the
2026-10-06 callback-form extension. The Private Integration Token
(settings.GHL_API_KEY) is a static, non-expiring, sub-account-scoped
secret. It must never reach client-side JS, template context, or logs
— server-side only, same posture as VAULT_SERVER_SECRET in CLAUDE.md.

Division of labour: Django owns spam filtering, validation, and the
Lead record. GHL owns the CRM pipeline once the relevant tag lands —
tags are what fire GHL workflow automation, so no workflow endpoint
needs to be called from here. Different forms apply different tags
(contact form: 'website-lead', callback form: 'callback-request'),
which is why `tag` is a parameter rather than hardcoded.

TAGS ARE NEVER SENT ON THE UPSERT. GHL replaces a contact's entire
tag array on every /contacts/upsert call — an existing contact tagged
["website-lead", "booked", "client", "hvac", "onboarding-complete"]
who submits a form again would come back as just ["website-lead"],
destroying client status, trade, and onboarding state, and re-firing
intake automation on an existing paying client. The tag is applied in
a SECOND, additive call to /contacts/{contactId}/tags after the
upsert succeeds. See sync_lead_to_ghl.

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


def split_name(full_name):
    """
    First token -> first name, remainder -> last name. A single word
    gets first name only.

    GHL's {{contact.first_name}} merge field is used in multiple
    automated messages — sending "Dave Moreno" whole as firstName
    renders "Hi Dave Moreno," instead of "Hi Dave,".
    """
    parts = (full_name or '').strip().split(None, 1)
    if not parts:
        return '', ''
    if len(parts) == 1:
        return parts[0], ''
    return parts[0], parts[1]


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
                   company_name='', custom_fields=None,
                   source='website-contact-form'):
    """
    Build the /contacts/upsert request body. custom_fields is a dict of
    key -> value (strings); order is preserved in the output list.

    Deliberately no 'tags' key — see module docstring. Tagging happens
    in a separate, additive call via add_tags(), after the upsert.
    """
    custom_fields = dict(custom_fields or {})

    payload = {
        'locationId': getattr(settings, 'GHL_LOCATION_ID', ''),
        'firstName': first_name,
        'source': source,
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


def _post(path, json_body):
    """
    One POST against the GHL API, with retry/error handling shared by
    every endpoint this client calls. Retries HTTP 429 honouring
    Retry-After, with jitter, up to MAX_ATTEMPTS. Raises GHLError on
    any other failure — never returns a partial/ambiguous result.
    """
    base = (getattr(settings, 'GHL_API_BASE', '')
            or 'https://services.leadconnectorhq.com').rstrip('/')
    url = f'{base}/{path.lstrip("/")}'
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
                url, headers=headers, json=json_body, timeout=DEFAULT_TIMEOUT)
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
                f'GHL POST {path} -> HTTP {resp.status_code}: {detail}')

        if not resp.content:
            raise GHLError(f'GHL POST {path} returned an empty response body.')
        try:
            return resp.json()
        except ValueError as exc:
            raise GHLError(
                f'GHL POST {path} returned non-JSON: {resp.text[:200]}') from exc


def push_contact(payload):
    """POST /contacts/upsert."""
    return _post('contacts/upsert', payload)


def add_tags(contact_id, tags):
    """
    POST /contacts/{contactId}/tags — additive, merges with whatever
    tags the contact already has (201 on success). Use this instead of
    putting 'tags' on the upsert; see module docstring.
    """
    return _post(f'contacts/{contact_id}/tags', {'tags': list(tags)})


def sync_lead_to_ghl(*, first_name, last_name='', email='', phone_raw='',
                      company_name='', custom_fields=None,
                      source='website-contact-form', tag='website-lead'):
    """
    Upsert the contact, THEN apply `tag` additively via add_tags().
    Returns (contact_id, trace_id) only once both calls have
    succeeded — tagging is what fires the GHL workflow, so a contact
    that exists but didn't get tagged is treated as a full failure
    (the exception's .payload carries the contact_id so a human can
    tag it by hand from the FailedLeadSubmission record).

    Raises GHLError (incl. GHLNotConfigured) on any failure — callers
    must catch and fall back.
    """
    payload = build_payload(
        first_name=first_name, last_name=last_name, email=email,
        phone_raw=phone_raw, company_name=company_name,
        custom_fields=custom_fields, source=source,
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

    try:
        tag_body = add_tags(contact_id, [tag])
    except GHLError as exc:
        exc.payload = {
            'contact_id': contact_id, 'trace_id': trace_id, 'tags': [tag],
        }
        raise

    tags_added = tag_body.get('tagsAdded') or []
    if not tags_added:
        # Contact already had this tag — a repeat request from someone
        # already mid-flow. Not a failure; just don't claim credit for
        # a re-trigger that didn't happen.
        logger.info(
            'GHL contact %s already had tag %r — not re-triggering',
            contact_id, tag)

    logger.info(
        'GHL upsert+tag ok (contact_id=%s traceId=%s tag=%s added=%s)',
        contact_id, trace_id, tag, bool(tags_added))
    return contact_id, trace_id
