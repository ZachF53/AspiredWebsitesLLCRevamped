"""
Inbound SendGrid Event Webhook handler.

SendGrid POSTs a JSON array of events (one element per event) to a
URL we configure in their dashboard. Events of interest for outreach:

    - delivered    → confirms SendGrid handed off to receiving server
    - open         → recipient loaded the tracking pixel
    - click        → recipient clicked a tracked link

Each event carries the ``email_sent_id`` we set in the X-SMTPAPI
unique_args at dispatch time — that's how we look up the right
``EmailSent`` row without needing to parse the Message-ID.

Setup (one-time, in SendGrid dashboard):
    Settings → Mail Settings → Event Webhook
      HTTP POST URL:  https://aspiredwebsites.com/sendgrid/events/
      Actions:        check ``Delivered``, ``Opened``, ``Clicked``,
                      and (optionally) ``Bounced``, ``Spam Reports``,
                      ``Unsubscribes``
      Signature Verification: ON
      Copy the verification public key into env var
      ``SENDGRID_WEBHOOK_PUBLIC_KEY`` and restart gunicorn.

Without ``SENDGRID_WEBHOOK_PUBLIC_KEY`` set, the view rejects ALL
inbound posts — keeps the endpoint locked down by default.
"""

import base64
import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from outreach.models import EmailSent, Lead, SuppressionList

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def receive(request):
    """
    Ingest one SendGrid event batch. Each batch is a JSON array.
    Returns 200 on success (SendGrid retries on non-2xx so 4xx/5xx
    means SendGrid will resend the same batch).
    """
    pubkey = getattr(settings, 'SENDGRID_WEBHOOK_PUBLIC_KEY', '') or ''
    if not pubkey:
        logger.warning(
            'sendgrid webhook: rejecting POST — '
            'SENDGRID_WEBHOOK_PUBLIC_KEY not set')
        return HttpResponse('webhook disabled', status=403)

    if not _verify_signature(request, pubkey):
        logger.warning('sendgrid webhook: signature verification FAILED')
        return HttpResponse('bad signature', status=403)

    try:
        events = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return HttpResponse('bad json', status=400)
    if not isinstance(events, list):
        return HttpResponse('expected JSON array', status=400)

    counts = {'open': 0, 'click': 0, 'delivered': 0,
              'bounce': 0, 'spamreport': 0, 'unsubscribe': 0,
              'ignored': 0}
    for evt in events:
        try:
            handled = _process_event(evt)
            counts[handled] = counts.get(handled, 0) + 1
        except Exception:  # noqa: BLE001
            logger.exception(
                'sendgrid webhook: failed to process event %r', evt)
            counts['ignored'] += 1

    return JsonResponse({'received': len(events), **counts})


def _verify_signature(request, pubkey_b64):
    """
    SendGrid signs each event-webhook POST with ECDSA P-256 over
    SHA-256(timestamp + body). Verify with the ``cryptography``
    library (already a transitive dep via paramiko/sendgrid — no
    new package needed).

    Returns False on missing headers, bad key, or bad signature.
    True on a valid signature.
    """
    sig_b64 = request.META.get(
        'HTTP_X_TWILIO_EMAIL_EVENT_WEBHOOK_SIGNATURE', '')
    ts = request.META.get(
        'HTTP_X_TWILIO_EMAIL_EVENT_WEBHOOK_TIMESTAMP', '')
    if not sig_b64 or not ts:
        return False

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        # SendGrid's public key arrives as base64-encoded DER (SPKI).
        pubkey_der = base64.b64decode(pubkey_b64)
        public_key = serialization.load_der_public_key(pubkey_der)
        if not isinstance(public_key, ec.EllipticCurvePublicKey):
            logger.warning('sendgrid webhook: pubkey is not ECDSA')
            return False

        signature = base64.b64decode(sig_b64)
        payload = ts.encode('utf-8') + request.body
        public_key.verify(signature, payload, ec.ECDSA(hashes.SHA256()))
        return True
    except InvalidSignature:
        return False
    except Exception:  # noqa: BLE001
        logger.exception('sendgrid webhook: verify_signature crashed')
        return False


def _process_event(evt):
    """
    Apply one event to the matching EmailSent. Returns a counts key
    string. Unknown / unmatched / spam-folder-only events return
    'ignored' so they're surfaced in the response.
    """
    event_type = evt.get('event', '')
    unique = evt.get('unique_args', {}) or {}
    email_sent_id = (
        evt.get('email_sent_id')        # unique_args bubble to top-level
        or unique.get('email_sent_id')
    )

    if not email_sent_id:
        return 'ignored'

    try:
        email_sent_id = int(email_sent_id)
    except (TypeError, ValueError):
        return 'ignored'

    es = EmailSent.objects.filter(pk=email_sent_id).first()
    if es is None:
        return 'ignored'

    now = timezone.now()
    # ts arrives as an epoch int in evt['timestamp']; use it for
    # accuracy when present.
    epoch = evt.get('timestamp')
    when = (
        timezone.make_aware(timezone.datetime.fromtimestamp(epoch))
        if isinstance(epoch, (int, float)) else now
    )

    if event_type == 'open' and not es.opened:
        es.opened = True
        es.opened_at = when
        es.save(update_fields=['opened', 'opened_at'])
        return 'open'

    if event_type == 'click' and not es.clicked:
        es.clicked = True
        es.clicked_at = when
        # Click implies open even if SendGrid dropped the open ping.
        if not es.opened:
            es.opened = True
            es.opened_at = when
            es.save(update_fields=['opened', 'opened_at',
                                   'clicked', 'clicked_at'])
        else:
            es.save(update_fields=['clicked', 'clicked_at'])
        return 'click'

    if event_type == 'delivered':
        # Already tracked at SMTP-accept; nothing to update but worth
        # counting for visibility.
        return 'delivered'

    if event_type in ('bounce', 'dropped'):
        # Permanent delivery failure — auto-suppress + mark lead.
        _suppress_lead(es.lead, reason=f'SendGrid {event_type}')
        return 'bounce'

    if event_type == 'spamreport':
        _suppress_lead(es.lead, reason='SendGrid spam report')
        return 'spamreport'

    if event_type == 'unsubscribe' or event_type == 'group_unsubscribe':
        _suppress_lead(es.lead, reason='SendGrid unsubscribe')
        return 'unsubscribe'

    return 'ignored'


def _suppress_lead(lead, reason):
    """Mark a lead unsubscribed + add their address to SuppressionList."""
    if lead.email:
        SuppressionList.objects.update_or_create(
            email=lead.email.lower(),
            defaults={
                'domain': lead.email.split('@', 1)[-1].lower(),
                'reason': reason,
            },
        )
    if not lead.unsubscribed:
        lead.unsubscribed = True
        lead.unsubscribed_at = timezone.now()
        lead.sequence_paused = True
        Lead.objects.filter(pk=lead.pk).update(
            unsubscribed=True,
            unsubscribed_at=lead.unsubscribed_at,
            sequence_paused=True,
        )
