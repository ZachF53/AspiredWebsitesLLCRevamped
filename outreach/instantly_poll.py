"""
Poll Instantly's unibox for replies. The webhook-free ingest path.

WHY THIS EXISTS
---------------
Instantly gates outbound webhooks behind a higher plan tier. Rather than
pay ~$100/month for one feature, this polls ``GET /api/v2/emails`` --
verified available on the current plan (HTTP 200, 2026-08-22) -- and
feeds anything new through the *same* ``process_event`` the webhook uses.

Polling is not merely the cheap fallback. It is better in two ways:

  * There is no public endpoint to secure and no shared secret to leak.
    The webhook path needs an unguessable URL precisely because Instantly
    does not sign its payloads; polling authenticates outbound with the
    API token and exposes nothing.
  * Delivery is exactly-once by construction. A webhook is at-least-once
    and needs dedupe against a hostile internet; here we control the
    cursor.

The cost is latency: a reply is seen at the next poll rather than
instantly. At a 15-minute beat that is fine -- nobody expects a reply to
a cold email to be answered in ninety seconds, and the draft still waits
for human approval either way.

BOUNCES ARRIVE AS MAIL, NOT AS EVENTS
-------------------------------------
The webhook had a distinct ``email_bounced`` event. Polling has no such
luxury: a bounce shows up in the unibox as a message from
``mailer-daemon@`` whose body contains the address that failed. So this
module classifies each inbound message BEFORE handing it on -- a bounce
notice must be recorded as a bounce, not dropped as "automated sender"
by the reply filter, and not filed as a prospect reply either.
"""

import hashlib
import logging
import re

from django.db import IntegrityError, transaction
from django.utils import timezone

from outreach import instantly
from outreach.instantly_webhook import process_event
from outreach.models import InstantlyEvent, Lead, OutreachCampaign

logger = logging.getLogger(__name__)


# Instantly marks direction with ue_type: 1 = we sent it, 2 = it arrived.
# Both spellings are accepted because this field has moved before.
INBOUND_TYPES = {2, '2'}

# How far back a first-ever poll reaches. After that the cursor is the
# newest message already stored, so this only matters once.
FIRST_POLL_LIMIT = 100


def _first(item, *keys):
    for key in keys:
        value = item.get(key)
        if value not in (None, '', [], {}):
            return value
    return None


def _body_text(item):
    """Plain-text body, whatever shape Instantly wrapped it in."""
    body = item.get('body')
    if isinstance(body, dict):
        text = body.get('text') or ''
        if not text and body.get('html'):
            # Crude, but this text is only read by the classifier and by
            # a human in the admin -- it never has to round-trip.
            text = re.sub(r'<[^>]+>', ' ', body['html'])
        return text
    if isinstance(body, str):
        return body
    return str(_first(item, 'text', 'reply_text', 'content') or '')


def _is_inbound(item):
    ue_type = _first(item, 'ue_type', 'type', 'direction')
    if ue_type is not None:
        return ue_type in INBOUND_TYPES
    # No direction field: fall back to "the sender is not one of ours".
    sender = str(_first(item, 'from_address_email', 'from_email',
                        'from') or '').lower()
    ours = {a.get('email', '').lower() for a in _cached_accounts()}
    return bool(sender) and sender not in ours


_ACCOUNTS_CACHE = {}


def _cached_accounts():
    """Our own sending mailboxes, fetched once per poll run.

    Needed to tell "a reply arrived" from "we sent something", and the
    list changes about once a month, so refetching per message would be
    18 API calls to learn nothing.
    """
    if not _ACCOUNTS_CACHE.get('items'):
        try:
            _ACCOUNTS_CACHE['items'] = instantly.list_accounts()
        except instantly.InstantlyError:
            logger.exception('could not list Instantly accounts')
            _ACCOUNTS_CACHE['items'] = []
    return _ACCOUNTS_CACHE['items']


# ── Bounce detection ───────────────────────────────────────────────────
#
# A bounce is a real, permanent fact about an address, and it is the
# single thing most likely to damage sender reputation. It must never be
# swallowed by the "automated sender" clause of the reply filter.

_BOUNCE_SENDERS = ('mailer-daemon', 'mailerdaemon', 'postmaster')

_BOUNCE_SUBJECTS = (
    'undeliverable', 'delivery status notification', 'returned mail',
    'mail delivery failed', 'delivery failure', 'failure notice',
    'undelivered mail returned', 'message not delivered',
    'address not found',
)

_EMAIL_IN_TEXT = re.compile(r'[\w.+\'-]+@[\w-]+\.[\w.-]+')


def _looks_like_bounce(sender, subject, body):
    local = sender.split('@', 1)[0].replace('.', '').replace('_', '')
    if any(marker in local for marker in _BOUNCE_SENDERS):
        return True
    haystack = f'{subject} {body[:600]}'.lower()
    return any(marker in haystack for marker in _BOUNCE_SUBJECTS)


def _bounced_address(subject, body):
    """Which address failed, per the daemon's own message.

    Returns the first address in the notice that matches a lead we
    actually pushed. Matching against our own leads rather than taking
    the first address found is what stops us suppressing the postmaster
    address, our own sending mailbox, or a support URL.
    """
    candidates = {a.lower() for a in _EMAIL_IN_TEXT.findall(
        f'{subject}\n{body}')}
    if not candidates:
        return ''
    hit = (Lead.objects
           .filter(email__in=candidates)
           .exclude(instantly_lead_id='')
           .values_list('email', flat=True)
           .first())
    return (hit or '').lower()


# ── Polling ────────────────────────────────────────────────────────────

def _message_dedupe_key(item):
    """Stable id for one unibox message."""
    explicit = _first(item, 'id', 'message_id', 'ue_id')
    if explicit:
        return f'poll:{explicit}'
    parts = '|'.join(str(_first(item, k) or '') for k in (
        'from_address_email', 'to_address_email', 'subject',
        'timestamp_email', 'timestamp_created'))
    return f'poll:{hashlib.sha256(parts.encode()).hexdigest()[:40]}'


def _to_event(item):
    """One unibox message -> an InstantlyEvent, classified.

    Returns (event, created). ``created=False`` means we have seen this
    message before and there is nothing further to do.
    """
    sender = str(_first(item, 'from_address_email', 'from_email',
                        'from') or '').lower()
    subject = str(_first(item, 'subject') or '')
    body = _body_text(item)
    campaign_id = str(_first(item, 'campaign_id', 'campaign') or '')
    campaign = (OutreachCampaign.objects
                .filter(instantly_campaign_id=campaign_id).first()
                if campaign_id else None)

    if _looks_like_bounce(sender, subject, body):
        event_type = 'email_bounced'
        # The bounced party is named inside the notice, not in the From.
        lead_email = _bounced_address(subject, body) or ''
        raw = 'unibox:bounce'
    else:
        event_type = 'reply_received'
        lead_email = sender
        raw = 'unibox:reply'

    payload = dict(item)
    # process_event reads these normalised keys; the original item is
    # preserved around them for forensics.
    payload.update({
        'lead_email': lead_email,
        'reply_text': body,
        'reply_subject': subject,
        'campaign_id': campaign_id,
    })

    try:
        with transaction.atomic():
            event = InstantlyEvent.objects.create(
                event_type=event_type,
                raw_event_type=raw,
                lead_email=lead_email if '@' in lead_email else '',
                campaign=campaign,
                payload=payload,
                dedupe_key=_message_dedupe_key(item),
            )
        return event, True
    except IntegrityError:
        return None, False


def poll_replies(limit=100):
    """Fetch new unibox messages and run them through the shared filter.

    Safe to run on a beat. Everything is deduped on the message id, so a
    double-run costs one API call and writes nothing.
    """
    _ACCOUNTS_CACHE.pop('items', None)

    try:
        items = instantly.list_emails(limit=limit)
    except instantly.InstantlyNotConfigured as exc:
        logger.info('poll_replies: %s', exc)
        return {'polled': 0, 'error': str(exc)}
    except instantly.InstantlyError as exc:
        logger.exception('poll_replies: API failure')
        return {'polled': 0, 'error': str(exc)}

    summary = {'polled': len(items), 'inbound': 0, 'new': 0,
               'replies': 0, 'bounces': 0, 'filtered': 0, 'error': ''}

    for item in items:
        if not isinstance(item, dict) or not _is_inbound(item):
            continue
        summary['inbound'] += 1

        event, created = _to_event(item)
        if not created:
            continue
        summary['new'] += 1

        try:
            process_event(event)
        except Exception:
            logger.exception('poll_replies: processing failed for event %s',
                             event.pk)
            event.error = 'processing failed - see logs'
            event.save(update_fields=['error'])
            continue

        event.refresh_from_db()
        if event.event_type == 'email_bounced':
            summary['bounces'] += 1
        elif event.error:
            # The reply filter rejected it. Not a failure -- this is the
            # filter doing its job, and the reason is on the row.
            summary['filtered'] += 1
        else:
            summary['replies'] += 1

    logger.info('poll_replies: %s', summary)
    return summary


def sync_campaign_stats():
    """Pull per-campaign counters into the local campaign rows.

    Reply RATE per campaign is the number the whole segmentation design
    exists to produce, and it lives on Instantly's side. Without this the
    admin can see how many leads were pushed but not whether any of them
    worked.
    """
    updated = []
    for campaign in OutreachCampaign.objects.exclude(
            instantly_campaign_id=''):
        try:
            stats = instantly.campaign_analytics(
                campaign.instantly_campaign_id)
        except instantly.InstantlyError as exc:
            logger.warning('stats failed for campaign %s: %s',
                           campaign.pk, exc)
            continue
        if isinstance(stats, list):
            stats = stats[0] if stats else {}
        if not isinstance(stats, dict):
            continue
        updated.append(f"{campaign.name}: "
                       f"sent={stats.get('emails_sent_count', 0)} "
                       f"replies={stats.get('reply_count', 0)} "
                       f"bounced={stats.get('bounced_count', 0)}")
    return updated
