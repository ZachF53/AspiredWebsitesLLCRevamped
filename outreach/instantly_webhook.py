"""
Instantly webhook ingest — replies, bounces, unsubscribes, opens.

SECURITY POSTURE
----------------
Instantly does not sign its webhooks. There is no HMAC to verify and no
source IP range worth pinning, so the only thing between this endpoint
and the open internet is an unguessable path segment compared in
constant time. ``INSTANTLY_WEBHOOK_SECRET`` empty means the endpoint
refuses everything — an anonymous POST here can mark leads unsubscribed
and file replies, so failing closed is the only safe default.

WHY EVERY EVENT IS STORED RAW FIRST
-----------------------------------
The previous reply path (IMAP polling in ``reply_ingest.py``) kept no
record of what it received. When it filed ten Google Ads notifications
from Zach's own address as prospect replies, the only way to notice was
to read the resulting EmailReply rows and see they made no sense. Here
the payload is written to ``InstantlyEvent`` before anything is
interpreted, so a misclassification can be diagnosed and replayed.

IDEMPOTENCY
-----------
Webhooks are at-least-once. Instantly retries on any non-2xx, and a
retried ``reply_received`` must not create a second EmailReply. Every
event gets a ``dedupe_key``; a repeat is acknowledged with 200 and
dropped, because returning an error would guarantee another retry.
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from outreach.models import (
    EmailReply, InstantlyEvent, Lead, OutreachCampaign, SuppressionList,
)

logger = logging.getLogger(__name__)


# Instantly's event vocabulary -> our EVENT_CHOICES. Their names have
# changed before, so unmapped values are stored as 'unknown' with the
# original preserved in raw_event_type rather than dropped.
EVENT_MAP = {
    'reply_received': 'reply_received',
    'email_replied': 'reply_received',
    'lead_replied': 'reply_received',
    'email_sent': 'email_sent',
    'email_opened': 'email_opened',
    'email_open': 'email_opened',
    'link_clicked': 'link_clicked',
    'email_bounced': 'email_bounced',
    'email_bounce': 'email_bounced',
    'lead_unsubscribed': 'lead_unsubscribed',
    'unsubscribe': 'lead_unsubscribed',
    'lead_interested': 'lead_interested',
    'lead_not_interested': 'lead_not_interested',
    'campaign_completed': 'campaign_completed',
}


def _first(payload, *keys):
    """First non-empty value among several possible key spellings.

    Instantly's payload keys differ between event types and have moved
    across API versions; guessing one spelling and getting a silent blank
    is how an ingest quietly stops working.
    """
    for key in keys:
        value = payload.get(key)
        if value not in (None, '', [], {}):
            return value
    return None


def _dedupe_key(event_type, payload):
    """Stable identity for one logical event.

    Prefers an id Instantly supplies. Falls back to a hash of the fields
    that make an event unique, so a retry still collapses even when no id
    is present.
    """
    explicit = _first(payload, 'id', 'event_id', 'webhook_id', 'message_id')
    if explicit:
        return f'{event_type}:{explicit}'

    parts = [
        event_type,
        str(_first(payload, 'lead_email', 'email', 'lead') or ''),
        str(_first(payload, 'campaign_id', 'campaign') or ''),
        str(_first(payload, 'timestamp', 'event_timestamp', 'date') or ''),
        str(_first(payload, 'step', 'sequence_step') or ''),
    ]
    digest = hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()
    return f'{event_type}:{digest[:40]}'


def _secret_ok(provided):
    expected = (getattr(settings, 'INSTANTLY_WEBHOOK_SECRET', '') or '').strip()
    if not expected:
        return False
    return hmac.compare_digest(str(provided or ''), expected)


@csrf_exempt
@require_POST
def receive(request, secret=''):
    """POST /outreach/instantly/events/<secret>/

    Always returns 200 once the event is safely stored, even if
    interpretation fails. Instantly retries non-2xx, and a bug in our
    handling should not turn into an infinite redelivery loop against a
    payload we have already captured.
    """
    if not _secret_ok(secret):
        logger.warning(
            'instantly webhook: rejected request with bad or missing secret')
        return HttpResponseForbidden('forbidden')

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        logger.warning('instantly webhook: body was not JSON')
        return HttpResponse('bad request', status=400)

    if not isinstance(payload, dict):
        return HttpResponse('bad request', status=400)

    raw_type = str(_first(payload, 'event_type', 'type', 'event') or '')
    event_type = EVENT_MAP.get(raw_type.lower(), 'unknown')
    if event_type == 'unknown' and raw_type:
        logger.info('instantly webhook: unmapped event type %r', raw_type)

    email = str(_first(payload, 'lead_email', 'email', 'to_email') or '').lower()
    campaign_id = str(_first(payload, 'campaign_id', 'campaign') or '')

    try:
        # Wrapped in a savepoint: a duplicate raises IntegrityError, and
        # without the savepoint the surrounding transaction is left
        # broken so every later query in the same request fails too.
        with transaction.atomic():
            event = InstantlyEvent.objects.create(
                event_type=event_type,
                raw_event_type=raw_type[:80],
                lead_email=email if '@' in email else '',
                campaign=OutreachCampaign.objects.filter(
                    instantly_campaign_id=campaign_id).first() if campaign_id
                else None,
                payload=payload,
                dedupe_key=_dedupe_key(event_type, payload),
            )
    except IntegrityError:
        # Already seen. 200 so Instantly stops retrying.
        return JsonResponse({'status': 'duplicate'})

    try:
        process_event(event)
    except Exception:
        logger.exception('instantly webhook: processing failed for event %s',
                         event.pk)
        event.error = 'processing failed — see logs'
        event.save(update_fields=['error'])

    return JsonResponse({'status': 'ok', 'event': event.event_type})


# ── Interpretation ─────────────────────────────────────────────────────

def _match_lead(event):
    """Find the Lead this event belongs to.

    Instantly's own lead id is the reliable join; email is the fallback
    for events that predate the push or arrive for a lead added in their
    UI.
    """
    instantly_id = _first(event.payload, 'lead_id', 'lead')
    if instantly_id:
        lead = Lead.objects.filter(
            instantly_lead_id=str(instantly_id)).first()
        if lead:
            return lead
    if event.lead_email:
        return Lead.objects.filter(email__iexact=event.lead_email).first()
    return None


def _is_ingestable_reply(event, lead):
    """Whether this 'reply' is actually a human replying to our outreach.

    THE FILTER THAT DID NOT EXIST. The old IMAP path had none, so ten
    Google Ads billing notifications from hello@aspired-ai.com became ten
    prospect replies against a Lead row for Zach's own company. Every
    clause below corresponds to something that actually got through.
    """
    email = (event.lead_email or '').lower()
    if not email or '@' not in email:
        return False, 'no sender address on the event'

    local, _, domain = email.rpartition('@')

    # 1. Our own mail. This is the exact shape of the ten bad rows.
    own_domains = {
        'aspiredwebsites.com', 'aspired-ai.com', 'aspiredautomations.com',
        'getaspiredautomations.com', 'goaspiredautomations.com',
        'seeaspiredautomations.com', 'moonieful.com',
    }
    configured = str(getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '')
    if '@' in configured:
        own_domains.add(configured.rpartition('@')[2].strip('> '))
    if domain in own_domains:
        return False, f'sender is our own domain ({domain})'

    # 2. Automated senders. Never a prospect, always noise. Compared on
    # the normalised local part so no-reply@, no.reply@ and noreply@ are
    # one thing rather than three near-misses.
    from outreach import verify

    automated_locals = {
        'noreply', 'donotreply', 'bounce', 'bounces', 'mailerdaemon',
        'postmaster', 'notifications', 'notification', 'alerts', 'alert',
        'automated', 'system', 'daemon', 'supportnoreply', 'mailer',
    }
    if verify.normalise_local(local) in automated_locals:
        return False, f'automated sender ({local}@)'

    # 3. Out-of-office and vacation autoresponders.
    body = str(_first(event.payload, 'reply_text', 'reply_text_snippet',
                      'body', 'text', 'message') or '').lower()
    subject = str(_first(event.payload, 'reply_subject', 'subject') or '').lower()
    auto_markers = (
        'out of office', 'out-of-office', 'automatic reply', 'auto-reply',
        'autoreply', 'away from my desk', 'on vacation', 'currently away',
        'i am out of the office', 'delivery status notification',
        'undeliverable', 'mail delivery failed',
    )
    haystack = f'{subject} {body}'
    for marker in auto_markers:
        if marker in haystack:
            return False, f'autoresponder ({marker!r})'

    # 4. The lead must be one we actually contacted. A reply from someone
    # we never emailed is not a reply.
    if lead is None:
        return False, 'no matching lead — we never emailed this address'

    return True, ''


def process_event(event):
    """Apply one stored event to the CRM.

    Shared by BOTH ingest paths: the webhook below and
    ``outreach.instantly_poll``. Kept as one function on purpose -- two
    copies of this logic would drift, and the half that drifted would be
    the half that stopped filtering.
    """
    lead = _match_lead(event)
    if lead is not None and event.lead_id is None:
        event.lead = lead

    if event.event_type == 'reply_received':
        # Deliberately runs whether or not a Lead matched. The filter has
        # to apply even when the sender DOES resolve to a lead — that is
        # precisely the prod failure: a Lead row for "Aspired AI LLC"
        # carried hello@aspired-ai.com, so its Google Ads notifications
        # matched a lead and sailed through as prospect replies. Gating
        # the filter behind "did we find a lead" would rebuild the bug.
        _handle_reply(event, lead)
    else:
        handler = {
            'email_bounced': _handle_bounce,
            'lead_unsubscribed': _handle_unsubscribe,
            'lead_interested': _handle_interested,
            'lead_not_interested': _handle_not_interested,
            'email_opened': _handle_open,
            'email_sent': _handle_sent,
        }.get(event.event_type)

        if handler is not None and lead is not None:
            handler(event, lead)
        elif handler is not None:
            event.error = 'no matching lead for this event'

    event.processed = True
    event.processed_at = timezone.now()
    event.save(update_fields=['lead', 'processed', 'processed_at', 'error'])


def _handle_reply(event, lead):
    ok, reason = _is_ingestable_reply(event, lead)
    if not ok:
        event.error = f'not ingested as a reply: {reason}'
        logger.info('instantly webhook: dropped reply — %s', reason)
        return

    body = str(_first(event.payload, 'reply_text', 'reply_text_snippet',
                      'body', 'text', 'message') or '').strip()
    subject = str(_first(event.payload, 'reply_subject', 'subject') or '')

    reply, created = EmailReply.objects.get_or_create(
        inbound_message_id=event.dedupe_key,
        defaults={
            'lead': lead,
            'subject': subject[:255],
            'body': body,
            'needs_human': True,
        },
    )
    if not created:
        return

    lead.status = 'replied'
    lead.sequence_paused = True
    lead.save(update_fields=['status', 'sequence_paused', 'updated_at'])

    # Instantly stops its own sequence on reply, but a lead paused here
    # and not there would keep receiving follow-ups.
    _pause_quietly(lead)

    # Classification and drafting run off the request thread.
    try:
        from outreach.tasks import classify_and_draft_reply_task
        classify_and_draft_reply_task.delay(str(reply.pk))
    except Exception:
        logger.exception('failed to enqueue classification for reply %s',
                         reply.pk)


def _handle_bounce(event, lead):
    """A bounce is a fact about the address — record it permanently.

    Bounced addresses are the thing that destroys sender reputation, so
    the address is suppressed rather than merely flagged.
    """
    from outreach import verify

    lead.email_verification_status = verify.INVALID
    lead.email_verified_at = timezone.now()
    lead.sequence_paused = True
    lead.save(update_fields=[
        'email_verification_status', 'email_verified_at',
        'sequence_paused', 'updated_at'])

    if lead.email:
        SuppressionList.objects.get_or_create(
            email=lead.email.lower(),
            defaults={'reason': 'hard bounce (Instantly)',
                      'domain': lead.email.rpartition('@')[2]},
        )


def _handle_unsubscribe(event, lead):
    """Unsubscribes are permanent — CLAUDE.md business rule 6."""
    lead.unsubscribed = True
    lead.unsubscribed_at = timezone.now()
    lead.status = 'unsubscribed'
    lead.sequence_paused = True
    lead.save(update_fields=[
        'unsubscribed', 'unsubscribed_at', 'status', 'sequence_paused',
        'updated_at'])

    if lead.email:
        SuppressionList.objects.get_or_create(
            email=lead.email.lower(),
            defaults={'reason': 'unsubscribed (Instantly)',
                      'domain': lead.email.rpartition('@')[2]},
        )
    _pause_quietly(lead)


def _handle_interested(event, lead):
    lead.status = 'replied'
    lead.temperature = 'hot'
    lead.sequence_paused = True
    lead.save(update_fields=[
        'status', 'temperature', 'sequence_paused', 'updated_at'])


def _handle_not_interested(event, lead):
    lead.status = 'lost'
    lead.sequence_paused = True
    lead.save(update_fields=['status', 'sequence_paused', 'updated_at'])


def _handle_open(event, lead):
    """Opens are the weakest signal here and are recorded, not acted on.

    Apple Mail Privacy Protection pre-fetches images, so a meaningful
    share of 'opens' are a proxy rather than a person. Useful only in
    aggregate for comparing campaigns.
    """
    if lead.status == 'new':
        lead.status = 'contacted'
        lead.save(update_fields=['status', 'updated_at'])


def _handle_sent(event, lead):
    """Instantly confirming a send is what advances the sequence clock.

    Under SendGrid this advanced at generation time, which froze the
    whole funnel when a draft was never dispatched. Anchoring it to a
    confirmed send is the fix, and this is where that anchor now lives.
    """
    step = _first(event.payload, 'step', 'sequence_step', 'email_step')
    lead.last_contacted_at = timezone.now()
    try:
        step_number = int(step)
    except (TypeError, ValueError):
        step_number = (lead.sequence_step or 0) + 1
    if step_number > (lead.sequence_step or 0):
        lead.sequence_step = step_number
    if lead.status == 'new':
        lead.status = 'contacted'
    lead.save(update_fields=[
        'last_contacted_at', 'sequence_step', 'status', 'updated_at'])


def _pause_quietly(lead):
    """Best-effort stop of the Instantly sequence.

    Never raises: the CRM state above is already correct and committed,
    and an API blip must not roll that back or turn into a webhook retry.
    """
    try:
        from outreach import instantly
        instantly.pause_lead(lead)
    except Exception:
        logger.exception('could not pause Instantly sequence for lead %s',
                         lead.pk)
