"""
Inbound: the ``VoiceAiCallEnd`` event from each client's GHL sub-account.

Structurally this is ``reviews/ghl_webhook.py`` — one global endpoint,
program resolved from the payload's ``locationId``, an unguessable path
secret compared in constant time, fails closed when the setting is unset,
and always 200 once the row is stored because GHL retries non-2xx.

**What is different, and why it matters more here.** The review webhook
feeds a stats page; a dropped event means a slightly wrong chart. This
one feeds an invoice. A dropped event is revenue we never bill, and a
duplicated one is a charge the client did not earn. So:

* Dedupe is on GHL's own call ``id``, not a synthesised hash. Every
  VoiceAiCallEnd carries one, so there is no fallback path to get wrong.
* ``trialCall`` is excluded from billable minutes. The operator makes
  test calls while building a client's agent, and billing the client for
  our own QA would be indefensible.
* A nightly sweep over GHL's call-logs API reconciles anything this
  endpoint missed — see ``voice/tasks.py``. This endpoint is the fast
  path, not the authority.

## Why `duration` is stored in seconds

That is how GHL sends it. Converting on the way in would bake a rounding
decision into the raw record, and the rounding has to happen once per
billing period rather than once per call — see ``voice/usage.py``.
"""

import hmac
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)

# Action types GHL reports in `executedCallActions`. There is no outcome
# or status field on the payload, so what the agent DID is the only
# evidence of how the call went.
ACTION_TRANSFER = 'CALL_TRANSFER'
ACTION_BOOKING = 'APPOINTMENT_BOOKING'


def _first(payload, *keys):
    """Delegates to core.ghl_payload.first.

    Kept as a module-level name because the tests patch and call it, but
    the implementation is now shared with reviews.ghl_webhook so the two
    endpoints cannot drift in how they read a payload. The shared version
    additionally checks ``customData`` first, which is what makes GHL's
    standard (free, properly-serialised) Webhook action work here.
    """
    from core.ghl_payload import first
    return first(payload, *keys)


def _secret_ok(provided):
    expected = (getattr(settings, 'VOICE_GHL_EVENTS_SECRET', '') or '').strip()
    if not expected:
        # Fail closed. An unset secret must not mean "accept anything" —
        # and here accepting anything means accepting fabricated billable
        # minutes.
        return False
    return hmac.compare_digest(str(provided or ''), expected)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in ('1', 'true', 'yes')


def _action_names(payload):
    """Normalised action-type strings from `executedCallActions`.

    The array holds objects whose type key GHL has spelled more than one
    way across doc revisions, so several are checked. Plain strings are
    tolerated too.
    """
    raw = payload.get('executedCallActions') or []
    names = []
    if not isinstance(raw, list):
        return names
    for item in raw:
        if isinstance(item, str):
            names.append(item.strip().upper())
        elif isinstance(item, dict):
            value = _first(item, 'actionType', 'action_type', 'type',
                           'action', 'name')
            if value:
                names.append(str(value).strip().upper())
    return names


def _resolve_program(payload):
    """The VoiceProgram this call belongs to, or None.

    By GHL location id first — that is what identifies a sub-account.
    Falls back to the answering number, which covers a sub-account whose
    webhook was configured before we recorded its location id.
    """
    from voice.models import VoiceProgram

    location_id = _first(payload, 'locationId', 'location_id', 'location.id')
    if location_id:
        program = VoiceProgram.objects.filter(
            ghl_location_id=str(location_id)).first()
        if program is not None:
            return program

    number = _first(payload, 'toNumber', 'to_number', 'phoneNumber')
    if number:
        digits = ''.join(c for c in str(number) if c.isdigit())[-10:]
        if digits:
            for candidate in VoiceProgram.objects.exclude(
                    answering_number=''):
                existing = ''.join(
                    c for c in candidate.answering_number if c.isdigit())
                if existing[-10:] == digits:
                    return candidate
    return None


@csrf_exempt
@require_POST
def voice_events(request, secret):
    """POST /api/voice/ghl/events/<secret>/ — one VoiceAiCallEnd."""
    if not _secret_ok(secret):
        return HttpResponseForbidden('forbidden')

    from core.ghl_payload import coerce_duration_seconds, parse_body

    payload, _raw = parse_body(request, 'voice')
    if payload is None:
        # Malformed JSON is not worth a retry — nothing we can store.
        # The raw body has already been logged by parse_body, which is
        # how a template that renders invalid JSON gets diagnosed rather
        # than silently retried forever.
        return HttpResponse('bad payload', status=400)

    call_id = _first(payload, 'id', 'callId', 'call_id', 'messageId',
                     'eventId', 'event_id')
    if not call_id:
        # Without an id we cannot dedupe, and at-least-once delivery
        # would double-bill. Refuse rather than risk it.
        logger.warning('voice.webhook: payload with no call id; ignoring')
        return JsonResponse({'status': 'ignored', 'reason': 'no id'})

    program = _resolve_program(payload)

    raw_duration = _first(payload, 'duration', 'callDuration',
                          'call_duration', 'voice_ai.duration')
    duration, recognised = coerce_duration_seconds(raw_duration)
    if not recognised:
        # Non-empty but unparseable. Billing zero for a real call is a
        # revenue loss with no symptom, so the raw value is logged at
        # error level: this is the one number that has to be right.
        logger.error(
            'voice.webhook: unparseable duration %r on call %s; metered '
            'as 0 seconds. Add this format to '
            'core.ghl_payload.coerce_duration_seconds.',
            raw_duration, call_id)

    started_at = parse_datetime(
        str(_first(payload, 'createdAt', 'created_at', 'startedAt',
                   'dateAdded') or ''))
    if started_at is None:
        # GHL's post-call workflow exposes no start time, so the webhook
        # arrives at the call's END. Reconstruct the start from the
        # duration rather than stamping receipt time: after-hours
        # coverage is computed from this, and a call that began at 19:58
        # and ended at 20:02 is an in-hours call, not an after-hours one.
        # Falls back to now when the duration is unknown too.
        started_at = timezone.now() - timedelta(seconds=duration or 0)
    elif timezone.is_naive(started_at):
        started_at = timezone.make_aware(started_at)

    actions = _action_names(payload)
    trial = _as_bool(_first(payload, 'trialCall', 'trial_call'))

    after_hours = False
    if program is not None:
        try:
            from voice.schedule import is_after_hours
            after_hours = is_after_hours(program, started_at)
        except Exception:
            logger.exception('voice.webhook: after-hours check failed')

    fields = dict(
        program=program,
        ghl_call_id=str(call_id)[:255],
        ghl_location_id=str(
            _first(payload, 'locationId', 'location_id',
                   'location.id') or '')[:64],
        ghl_agent_id=str(_first(payload, 'agentId', 'agent_id') or '')[:64],
        started_at=started_at,
        duration_seconds=duration,
        from_number=str(
            _first(payload, 'fromNumber', 'from_number', 'from') or '')[:20],
        trial_call=trial,
        transferred=ACTION_TRANSFER in actions,
        booked_appointment=ACTION_BOOKING in actions,
        after_hours=after_hours,
        summary=str(_first(payload, 'summary') or ''),
        transcript=str(_first(payload, 'transcript') or ''),
        extracted_data=payload.get('extractedData') or {},
        executed_actions=payload.get('executedCallActions') or [],
        payload=payload,
    )

    from voice.models import VoiceCallRecord

    # ── Secondary dedupe, because the call id is SYNTHETIC ─────────────
    #
    # GHL's post-call workflow exposes no call id, so the body builds one
    # from contact id + a `right_now` timestamp. GHL re-renders merge
    # fields on retry, so a redelivery produces a DIFFERENT id, defeats
    # the primary dedupe, and bills the same call twice.
    #
    # So: same agent, same caller, same duration, within two minutes is
    # treated as a redelivery. Matched on existing columns (ghl_agent_id
    # stands in for the destination number — one agent per number here),
    # so this needs no migration.
    #
    # The trade is deliberate. Two genuinely distinct calls from one
    # caller to one agent with an identical duration inside two minutes
    # would be dropped. That is a callback of exactly equal length; far
    # rarer than a webhook retry, and under-billing one freak call beats
    # double-billing a real one.
    if duration > 0 and fields['from_number']:
        window_start = started_at - timedelta(seconds=120)
        window_end = started_at + timedelta(seconds=120)
        twin = (VoiceCallRecord.objects
                .filter(from_number=fields['from_number'],
                        ghl_agent_id=fields['ghl_agent_id'],
                        duration_seconds=duration,
                        started_at__gte=window_start,
                        started_at__lte=window_end)
                .exclude(ghl_call_id=fields['ghl_call_id'])
                .first())
        if twin is not None:
            logger.info(
                'voice.webhook: call %s looks like a redelivery of %s '
                '(same caller/agent/duration within 120s); not accruing',
                call_id, twin.ghl_call_id)
            return JsonResponse({'status': 'duplicate',
                                 'matched': 'secondary'})

    try:
        with transaction.atomic():
            call, created = VoiceCallRecord.objects.get_or_create(
                ghl_call_id=fields['ghl_call_id'], defaults=fields)
    except IntegrityError:
        # Concurrent delivery of the same call. The other writer won;
        # that is the correct outcome, so report success.
        logger.info('voice.webhook: duplicate call %s', call_id)
        return JsonResponse({'status': 'duplicate'})

    if not created:
        # At-least-once delivery. Already counted — do NOT accrue again.
        return JsonResponse({'status': 'duplicate'})

    if program is None:
        # Stored but unattributed, so the operator can see it and fix the
        # location id. Still a 200: retrying will not teach us the id.
        logger.warning(
            'voice.webhook: no program for location %s (call %s)',
            fields['ghl_location_id'] or '?', call_id)
        return JsonResponse({'status': 'stored', 'attributed': False})

    try:
        from voice.usage import accrue
        accrue(call)
    except Exception:
        # The call is stored; the nightly reconciliation will recompute
        # the period. Never 500 here — GHL would redeliver a call we
        # already hold and the retry cannot fix an accrual bug.
        logger.exception('voice.webhook: accrual failed for call %s', call.pk)

    return JsonResponse({'status': 'ok', 'billable': not trial})
