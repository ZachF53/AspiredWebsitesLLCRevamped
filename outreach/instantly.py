"""
Instantly API v2 client — the sending layer.

VERIFIED LIVE against the real workspace on 2026-08-22:

    GET  /api/v2/accounts            200, 12 mailboxes, all on
                                     *aspiredautomations.com,
                                     daily_limit 30, warmup score 100
    GET  /api/v2/campaigns           200, {"items": []}
    GET  /api/v2/lead-lists          200, {"items": []}
    GET  /api/v2/workspaces/current  200, plan pid_g_v2

Auth is ``Authorization: Bearer <token>`` — v2. The older v1 API took an
``api_key`` query parameter; do not mix them, the token shapes differ.

DIVISION OF LABOUR
------------------
Django owns sourcing, verification, enrichment, personalisation,
guardrails and approval. Instantly owns mailbox rotation, warmup,
throttling, the sequence itself, bounce handling and unsubscribe links.
Django pushes a verified lead plus custom variables; the campaign
template in Instantly references those variables.

WHAT THIS MODULE WILL NOT DO
----------------------------
It will not activate a campaign. ``create_campaign`` builds one paused,
and starting it is a deliberate human action in the Instantly UI,
because activating is the irreversible step that puts real mail in front
of real people. Everything here is safe to run against the live
workspace without anyone receiving an email.
"""

import logging

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30

# Instantly rejects oversized bulk writes; leads go up in chunks.
PUSH_CHUNK_SIZE = 100


class InstantlyError(Exception):
    """User-facing Instantly failure — the message is shown in the admin."""


class InstantlyNotConfigured(InstantlyError):
    """No token. Same posture as the Apify and Brave clients."""


def _token():
    token = (getattr(settings, 'INSTANTLY_TOKEN', '') or '').strip()
    if not token:
        raise InstantlyNotConfigured(
            'INSTANTLY_TOKEN is not set. Add it to .env and restart the '
            'worker — nothing can be pushed to a campaign without it.')
    return token


def _base():
    return (getattr(settings, 'INSTANTLY_API_BASE', '')
            or 'https://api.instantly.ai/api/v2').rstrip('/')


def _request(method, path, *, params=None, json=None, timeout=None):
    """One HTTP call against the v2 API, with errors made legible.

    Instantly returns its failures as JSON with a ``message`` key far
    more often than as a plain status, so the body is unpacked rather
    than surfaced as "HTTP 400".
    """
    url = f'{_base()}/{path.lstrip("/")}'
    headers = {
        'Authorization': f'Bearer {_token()}',
        'Content-Type': 'application/json',
    }
    try:
        resp = requests.request(
            method, url, headers=headers, params=params, json=json,
            timeout=timeout or DEFAULT_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise InstantlyError(f'Instantly unreachable: {exc}') from exc

    if resp.status_code >= 400:
        detail = resp.text[:400]
        try:
            body = resp.json()
            detail = (body.get('message') or body.get('error')
                      or str(body))[:400]
        except ValueError:
            pass
        raise InstantlyError(
            f'Instantly {method} {path} -> HTTP {resp.status_code}: {detail}')

    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise InstantlyError(
            f'Instantly returned non-JSON from {path}: '
            f'{resp.text[:200]}') from exc


def _paginate(path, params=None, limit_per_page=100, max_pages=50):
    """Walk Instantly's cursor pagination and yield every item.

    v2 pages with ``starting_after`` carrying the last item's id, and
    signals more results by returning ``next_starting_after``. The
    ``max_pages`` bound is a runaway guard, not a real limit — 50 pages
    is 5,000 records.
    """
    params = dict(params or {})
    params['limit'] = limit_per_page
    cursor = None
    for _ in range(max_pages):
        if cursor:
            params['starting_after'] = cursor
        page = _request('GET', path, params=params)
        items = page.get('items') or []
        for item in items:
            yield item
        cursor = page.get('next_starting_after')
        if not cursor or not items:
            return


# ── Read ───────────────────────────────────────────────────────────────

def list_accounts():
    """Every connected sending mailbox.

    Field names confirmed against the live workspace: ``email``,
    ``daily_limit``, ``warmup_status``, ``stat_warmup_score``,
    ``status``. There is no ``id`` or ``name`` key — an earlier probe
    asked for those and got nulls back.
    """
    return list(_paginate('accounts'))


def sending_capacity():
    """Total emails/day the connected mailboxes can send between them.

    The number that actually bounds the funnel. Twelve mailboxes at 30/day
    is 360/day; the list-quality work upstream is what decides whether
    using it is worth anything.
    """
    accounts = list_accounts()
    active = [a for a in accounts
              if a.get('status') == 1 and not a.get('setup_pending')]
    return {
        'mailboxes_total': len(accounts),
        'mailboxes_active': len(active),
        'daily_capacity': sum(int(a.get('daily_limit') or 0) for a in active),
        'domains': sorted({
            str(a.get('email', '')).rpartition('@')[2]
            for a in accounts if a.get('email')
        }),
        'warmup_scores': [a.get('stat_warmup_score') for a in active],
    }


def list_campaigns():
    return list(_paginate('campaigns'))


def get_campaign(campaign_id):
    return _request('GET', f'campaigns/{campaign_id}')


def campaign_analytics(campaign_id=None):
    """Per-campaign counters — sends, opens, replies, bounces.

    This is the feedback loop: reply rate per campaign is how a niche
    proves itself, and it is the number the whole segmentation design
    exists to produce.
    """
    params = {'id': campaign_id} if campaign_id else None
    result = _request('GET', 'campaigns/analytics', params=params)
    return result if isinstance(result, list) else result.get('items', result)


# ── Write ──────────────────────────────────────────────────────────────

def create_campaign(name, sequence_steps, *, daily_limit=None,
                    account_emails=None):
    """Create a campaign, PAUSED.

    ``sequence_steps`` is a list of dicts:
        [{'subject': '...', 'body': '...', 'delay_days': 0}, ...]

    Body text may reference custom variables with Instantly's
    double-brace syntax — ``{{firstName}}``, ``{{companyName}}``,
    ``{{icebreaker}}``. ``icebreaker`` is ours, pushed per lead by
    ``push_leads``.

    The campaign is created in a paused state and this module offers no
    way to start it. Activating puts real mail in front of real people,
    and that stays a deliberate click in Instantly's UI.
    """
    if not sequence_steps:
        raise InstantlyError('A campaign needs at least one sequence step.')

    steps = []
    for i, step in enumerate(sequence_steps):
        body = (step.get('body') or '').strip()
        subject = (step.get('subject') or '').strip()
        if not body:
            raise InstantlyError(f'Sequence step {i + 1} has an empty body.')
        steps.append({
            # delay is "days to wait BEFORE this step". Instantly wants
            # it on the step itself, and step 1 is always 0.
            'delay': 0 if i == 0 else int(step.get('delay_days', 3)),
            'variants': [{
                # A follow-up with a blank subject threads under the
                # previous email rather than starting a new one, which is
                # how a 4-touch sequence should read.
                'subject': subject,
                'body': body,
            }],
        })

    payload = {
        'name': name,
        'campaign_schedule': {
            'schedules': [{
                'name': 'Business hours',
                'timing': {'from': '09:00', 'to': '17:00'},
                'days': {'1': True, '2': True, '3': True,
                         '4': True, '5': True},
                'timezone': 'America/Chicago',
            }],
        },
        'sequences': [{'steps': steps}],
    }
    if daily_limit:
        payload['daily_limit'] = int(daily_limit)
    if account_emails:
        payload['email_list'] = list(account_emails)

    result = _request('POST', 'campaigns', json=payload)
    logger.info('created Instantly campaign %s (%s), paused',
                result.get('id'), name)
    return result


def _lead_payload(lead, campaign_id):
    """One Lead -> Instantly's lead shape.

    ``custom_variables`` is where the personalisation lives. Everything
    in it is referenceable from the campaign template as ``{{key}}``.
    """
    contact = (lead.attorney_name or '').strip()
    first, _, last = contact.partition(' ')

    payload = {
        'campaign': campaign_id,
        'email': lead.email,
        'first_name': first,
        'last_name': last.strip(),
        'company_name': lead.firm_name,
        'personalization': lead.icebreaker or '',
        'custom_variables': {
            'icebreaker': lead.icebreaker or '',
            'city': lead.city or '',
            'state': lead.state or '',
            'website': lead.website or '',
            'business_type': lead.business_type or '',
            # Carried so copy can reference the observation directly
            # without Instantly needing to know how it was derived.
            'aspired_lead_id': str(lead.pk),
        },
    }
    if lead.website:
        payload['website'] = lead.website
    if lead.phone:
        payload['phone'] = lead.phone
    return payload


def push_leads(leads, campaign):
    """Push verified leads into an Instantly campaign.

    Every lead is re-checked here rather than trusted from the caller.
    This function is the last gate before an address becomes a real send,
    and the whole reason the funnel produced nothing was a missing gate
    at exactly this point — so it does not assume anyone upstream did
    their job.

    Returns a summary dict. Leads that fail a gate are reported by
    reason rather than silently dropped.
    """
    from outreach import verify
    from outreach.models import SuppressionList

    if not campaign.instantly_campaign_id:
        raise InstantlyError(
            f'Campaign "{campaign.name}" has no instantly_campaign_id — '
            'create it in Instantly first.')
    if not campaign.active:
        raise InstantlyError(
            f'Campaign "{campaign.name}" is paused. Activate it in Django '
            'before pushing leads.')

    summary = {
        'total': 0, 'pushed': 0, 'skipped_unsendable': 0,
        'skipped_suppressed': 0, 'skipped_no_icebreaker': 0,
        'skipped_already_pushed': 0, 'skipped_unsubscribed': 0,
        'errors': 0, 'reasons': {},
    }

    def _note(reason):
        summary['reasons'][reason] = summary['reasons'].get(reason, 0) + 1

    eligible = []
    for lead in leads:
        summary['total'] += 1

        if lead.instantly_lead_id:
            summary['skipped_already_pushed'] += 1
            continue
        if lead.unsubscribed:
            summary['skipped_unsubscribed'] += 1
            continue
        if not verify.is_sendable(lead.email_verification_status):
            summary['skipped_unsendable'] += 1
            _note(verify.rejection_reason(lead.email_verification_status)
                  or lead.email_verification_status)
            continue
        if SuppressionList.objects.filter(
                email=(lead.email or '').lower()).exists():
            summary['skipped_suppressed'] += 1
            continue
        if not (lead.icebreaker or '').strip():
            # A lead with no personalised line is a mail merge, which is
            # the thing this whole pipeline exists to stop sending.
            summary['skipped_no_icebreaker'] += 1
            continue
        eligible.append(lead)

    cap = int(getattr(settings, 'INSTANTLY_MAX_PUSH_PER_DAY', 200) or 0)
    if cap and len(eligible) > cap:
        logger.warning(
            'push_leads: %d eligible leads exceeds INSTANTLY_MAX_PUSH_PER_DAY'
            ' (%d) — pushing the first %d, the rest stay queued',
            len(eligible), cap, cap)
        _note(f'Deferred by daily push cap ({cap})')
        eligible = eligible[:cap]

    for start in range(0, len(eligible), PUSH_CHUNK_SIZE):
        chunk = eligible[start:start + PUSH_CHUNK_SIZE]
        for lead in chunk:
            try:
                result = _request(
                    'POST', 'leads',
                    json=_lead_payload(lead, campaign.instantly_campaign_id))
                lead.instantly_lead_id = str(result.get('id') or '')
                lead.pushed_to_instantly_at = timezone.now()
                lead.campaign = campaign
                if lead.status == 'new':
                    lead.status = 'contacted'
                lead.save(update_fields=[
                    'instantly_lead_id', 'pushed_to_instantly_at',
                    'campaign', 'status', 'updated_at'])
                summary['pushed'] += 1
            except InstantlyError as exc:
                logger.exception('push failed for lead %s', lead.pk)
                summary['errors'] += 1
                _note(str(exc)[:160])

    campaign.leads_pushed = (campaign.leads_pushed or 0) + summary['pushed']
    campaign.last_push_at = timezone.now()
    campaign.last_push_error = (
        '' if not summary['errors']
        else f"{summary['errors']} lead(s) failed — see reasons")
    campaign.save(update_fields=[
        'leads_pushed', 'last_push_at', 'last_push_error', 'updated_at'])

    return summary


def pause_lead(lead):
    """Stop the sequence for one lead — used on reply and unsubscribe.

    Instantly stops a sequence on reply by itself, but an unsubscribe or
    a hostile reply classified on our side must also stop it, and that
    cannot wait for the next sync.
    """
    if not lead.instantly_lead_id:
        return False
    _request('POST', f'leads/{lead.instantly_lead_id}/pause')
    return True


def connection_status():
    """Cheap health check for the admin page. Never raises.

    Returns a dict the template renders directly, because a page that
    explains *why* the integration is down beats one that shows a blank
    panel.
    """
    try:
        capacity = sending_capacity()
    except InstantlyNotConfigured as exc:
        return {'connected': False, 'reason': str(exc), 'configured': False}
    except InstantlyError as exc:
        return {'connected': False, 'reason': str(exc), 'configured': True}

    return {
        'connected': True,
        'configured': True,
        'reason': '',
        **capacity,
    }
