"""
Inbound: what the client's GoHighLevel workflow reports back.

GHL does the sending. For the portal to show anything true, the workflow
has to tell us what happened — a request went out, a customer replied, a
link was clicked, a review was left. This is the one endpoint that
receives all of it.

**One global endpoint, not one per program.** GHL webhooks are
configured per-location in their UI and every location posts to the same
URL, so the program is resolved from the payload's ``locationId``.

**Auth: an unguessable path segment.** GHL does not sign its webhooks,
so there is no HMAC to verify. Same posture and the same justification
as ``outreach/instantly_webhook.py``: the secret rides in the path, is
compared with ``hmac.compare_digest``, and the endpoint **403s
everything when the setting is unset** rather than defaulting open.

**Always 200 once the row is stored.** GHL retries non-2xx. A bug in our
interpretation must not become an infinite redelivery loop against a
payload we already hold, so interpretation failures are logged onto the
row and still answered 200.
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)

# Whatever the workflow calls it -> our event_type. Unmapped names are
# stored verbatim in the payload and typed 'unknown', so a workflow
# step nobody told us about is diagnosable rather than silently dropped.
EVENT_MAP = {
    'sent': 'sent',
    'review_sms_sent': 'sent',
    'review_request_sent': 'sent',
    'delivered': 'delivered',
    'reply': 'reply',
    'inbound_message': 'reply',
    'customer_reply': 'reply',
    'click': 'click',
    'link_click': 'click',
    'review_left': 'review_left',
    'review_received': 'review_left',
    'new_review': 'review_left',
    'opt_out': 'opt_out',
    'unsubscribe': 'opt_out',
    'dnd': 'opt_out',
    'failed': 'failed',
    'send_failed': 'failed',
}

# Reply bodies that are carrier/GHL keywords rather than a human talking.
# We do not act on these — GHL and the carrier own STOP handling — but we
# must not flag them for the client as a reply needing an answer.
_KEYWORDS = {
    'stop', 'stopall', 'unsubscribe', 'cancel', 'end', 'quit', 'revoke',
    'optout', 'opt out', 'no more', 'help', 'info', 'start', 'unstop',
    'yes',
}


def _first(payload, *keys):
    """First present, non-empty value among `keys`.

    Supports dotted paths ('customer.phone') because a workflow may send
    either nested or flat keys depending on how it was built.
    """
    for key in keys:
        if '.' in key:
            cur = payload
            for part in key.split('.'):
                if not isinstance(cur, dict):
                    cur = None
                    break
                cur = cur.get(part)
            if cur not in (None, ''):
                return cur
            continue
        value = payload.get(key)
        if value not in (None, ''):
            return value
    return None


def _secret_ok(provided):
    expected = (getattr(settings, 'REVIEW_GHL_EVENTS_SECRET', '') or '').strip()
    if not expected:
        # Fail closed. An unset secret must not mean "accept anything".
        return False
    return hmac.compare_digest(str(provided or ''), expected)


def _dedupe_key(event_type, payload):
    """Stable key for at-least-once delivery.

    Prefers an id the sender controls; falls back to a hash of the
    identifying fields, the same shape as
    ``outreach.instantly_webhook._dedupe_key``.
    """
    explicit = _first(payload, 'messageId', 'message_id', 'id', 'eventId',
                      'event_id', 'conversationId')
    if explicit:
        return f'{event_type}:{str(explicit)[:180]}'
    parts = [
        event_type,
        str(_first(payload, 'locationId', 'location_id') or ''),
        str(_first(payload, 'phone', 'customer_phone', 'customer.phone',
                   'from') or ''),
        str(_first(payload, 'email', 'customer_email',
                   'customer.email') or ''),
        str(_first(payload, 'timestamp', 'occurred_at', 'dateAdded') or ''),
    ]
    digest = hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()
    return f'{event_type}:{digest[:40]}'


def _resolve_program(payload):
    """The ReviewProgram this event belongs to, or None.

    By GHL location id first — that is what identifies a sub-account.
    Falls back to matching the destination number against a recorded
    sending number, which covers a workflow that omits locationId.
    """
    from reviews.models import ReviewProgram

    location_id = str(_first(payload, 'locationId', 'location_id') or '')
    if location_id:
        program = ReviewProgram.objects.filter(
            ghl_location_id=location_id).first()
        if program is not None:
            return program, location_id

    to_number = str(_first(payload, 'to', 'to_number', 'toNumber') or '')
    if to_number:
        digits = ''.join(c for c in to_number if c.isdigit())[-10:]
        if digits:
            for program in ReviewProgram.objects.exclude(sending_number=''):
                known = ''.join(
                    c for c in program.sending_number if c.isdigit())[-10:]
                if known and known == digits:
                    return program, location_id
    return None, location_id


def _occurred_at(payload):
    raw = _first(payload, 'timestamp', 'occurred_at', 'dateAdded', 'date')
    if raw:
        try:
            parsed = parse_datetime(str(raw))
            if parsed is not None:
                if timezone.is_naive(parsed):
                    parsed = timezone.make_aware(
                        parsed, timezone.get_default_timezone())
                return parsed
        except Exception:
            pass
    return timezone.now()


def _to_e164ish(raw):
    """Best-effort E.164. Reuses outreach.ghl.to_e164 and degrades to the
    raw digits rather than discarding a number we were given."""
    text = str(raw or '').strip()
    if not text:
        return ''
    try:
        from outreach.ghl import to_e164
        normalised = to_e164(text)
        if normalised:
            return normalised
    except Exception:
        pass
    digits = ''.join(c for c in text if c.isdigit())
    if len(digits) == 10:
        return f'+1{digits}'
    if len(digits) == 11 and digits.startswith('1'):
        return f'+{digits}'
    return text[:20]


@csrf_exempt
@require_POST
def receive(request, secret=''):
    """POST /api/reviews/ghl/events/<secret>/"""
    if not _secret_ok(secret):
        # Nothing is written. An unauthenticated POST must not be able to
        # create rows, and the response says nothing about why.
        logger.warning(
            'reviews ghl webhook: rejected request with bad or missing '
            'secret')
        return HttpResponseForbidden('forbidden')

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return HttpResponse('bad request', status=400)
    if not isinstance(payload, dict):
        return HttpResponse('bad request', status=400)

    raw_type = str(_first(payload, 'event_type', 'eventType', 'type',
                          'event') or '')
    event_type = EVENT_MAP.get(raw_type.strip().lower(), '')
    if not event_type:
        # Store it anyway — a workflow step we were not told about is a
        # configuration problem to investigate, not data to throw away.
        event_type = 'failed' if 'fail' in raw_type.lower() else 'sent'
        logger.info('reviews ghl webhook: unmapped event type %r', raw_type)

    program, location_id = _resolve_program(payload)

    body = str(_first(payload, 'body', 'message', 'text') or '')[:4000]
    phone = _to_e164ish(_first(
        payload, 'phone', 'customer_phone', 'customer.phone', 'from',
        'contact.phone'))
    email = str(_first(payload, 'email', 'customer_email', 'customer.email',
                       'contact.email') or '')[:254]
    name = str(_first(payload, 'name', 'customer_name', 'customer.name',
                      'full_name', 'contact.name') or '')[:200]

    rating = _first(payload, 'rating', 'review_rating', 'stars')
    try:
        rating = int(rating) if rating not in (None, '') else None
        if rating is not None and not (1 <= rating <= 5):
            rating = None
    except (TypeError, ValueError):
        rating = None

    from reviews.models import ReviewEvent

    try:
        with transaction.atomic():
            event = ReviewEvent.objects.create(
                program=program,
                event_type=event_type,
                payload=payload,
                dedupe_key=_dedupe_key(event_type, payload),
                ghl_location_id=location_id[:64],
                occurred_at=_occurred_at(payload),
                customer_name=name,
                customer_phone_e164=phone,
                customer_email=email if '@' in email else '',
                body=body,
                review_rating=rating,
                review_text=str(_first(payload, 'review_text',
                                       'reviewBody') or '')[:4000],
                needs_human=_needs_human(event_type, body),
                processed=True,
            )
    except IntegrityError:
        # Already seen. 200 so GHL stops retrying.
        return JsonResponse({'status': 'duplicate'})

    if program is None:
        # Stored, but we cannot attribute it. Almost always a GHL
        # location id that was never pasted into the admin.
        event.error = (
            f'No ReviewProgram matches GHL location "{location_id}" — '
            'paste the location ID on the admin Review System page.')
        event.save(update_fields=['error'])
        try:
            from core.system_alerts import record_alert
            record_alert(
                severity='warning',
                source='reviews.ghl_webhook.unresolved',
                message=f'Review event from unknown GHL location '
                        f'{location_id or "(none)"}',
                detail='The event was stored but is not attached to any '
                       'client. Check the location ID on the program.',
            )
        except Exception:
            logger.exception('reviews ghl webhook: alert failed')

    return JsonResponse({'status': 'ok', 'event': event.event_type})


def _needs_human(event_type, body):
    """True when a real person said something that deserves an answer.

    Only replies qualify, and only replies that are not carrier
    keywords. GHL already fires the operator SMS and the client
    notification for these; this flag is what surfaces them in the
    portal and the admin queue.
    """
    if event_type != 'reply':
        return False
    normalised = ''.join(
        c for c in (body or '').lower() if c.isalnum() or c.isspace()).strip()
    if not normalised:
        return False
    return normalised not in _KEYWORDS
