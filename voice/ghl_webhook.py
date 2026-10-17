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
import json
import logging

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
    """First present, non-empty value among `keys`, dotted paths allowed."""
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

    location_id = _first(payload, 'locationId', 'location_id')
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

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        # Malformed JSON is not worth a retry — nothing we can store.
        return HttpResponse('bad payload', status=400)
    if not isinstance(payload, dict):
        return HttpResponse('bad payload', status=400)

    call_id = _first(payload, 'id', 'callId', 'call_id', 'messageId')
    if not call_id:
        # Without an id we cannot dedupe, and at-least-once delivery
        # would double-bill. Refuse rather than risk it.
        logger.warning('voice.webhook: payload with no call id; ignoring')
        return JsonResponse({'status': 'ignored', 'reason': 'no id'})

    program = _resolve_program(payload)
    started_at = parse_datetime(
        str(_first(payload, 'createdAt', 'created_at', 'startedAt') or ''))
    if started_at is None:
        started_at = timezone.now()
    elif timezone.is_naive(started_at):
        started_at = timezone.make_aware(started_at)

    try:
        duration = int(float(_first(payload, 'duration') or 0))
    except (TypeError, ValueError):
        duration = 0
    duration = max(0, duration)

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
            _first(payload, 'locationId', 'location_id') or '')[:64],
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
