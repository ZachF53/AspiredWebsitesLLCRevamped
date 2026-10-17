"""
Turn completed wizard answers into a configured VoiceProgram.

Mirrors ``reviews/materialise.py``, including its registry-defaults
fallback: the wizard saves on change, so a client who accepted a
pre-filled answer (the default greeting, "8:00am - 5:00pm") never
generated a response row, and without the fallback their program would
come out blank in exactly the places we pre-filled for them.

Two things here that the review version does not have to do:

1. **Parse hours into windows.** V2 collects "8:00am - 5:00pm" as text
   because three dropdowns per day times seven days is a form nobody
   finishes. Parsing happens once, here, into ``BusinessHoursWindow``
   rows that ``voice/schedule.py`` can invert.
2. **Prepend the recording disclosure.** If recording is on, the
   greeting must announce it. The client is told we handle that and is
   not asked to write it, so this is where it actually gets added — and
   it is added idempotently, because the client can edit their greeting
   in the portal afterwards.
"""

import logging
import re
from datetime import time

from django.utils import timezone

logger = logging.getLogger(__name__)

ATTESTATION_VERSION = 'voice-v1-2026-10'

_CONSENT_KEYS = (
    'consent_recording_disclosure',
    'consent_numbers_lawful',
)

# Wizard key -> VoiceProgram field, for the answers that map straight
# across. Anything needing interpretation is handled explicitly below.
_FIELD_MAP = {
    'legal_business_name': 'legal_business_name',
    'business_phone': 'business_phone',
    'business_city': 'business_city',
    'business_state': 'business_state',
    'support_email': 'support_email',
    'business_timezone': 'timezone',
    'number_strategy': 'number_strategy',
    'coverage_mode': 'coverage_mode',
    'answer_mode': 'answer_mode',
    'transfer_number': 'transfer_number',
    'transfer_conditions': 'transfer_conditions',
    'after_transfer_fail': 'after_transfer_fail',
    'greeting': 'greeting',
    'services_offered': 'services_offered',
    'service_area': 'service_area',
    'faq_text': 'faq_text',
    'emergency_handling': 'emergency_handling',
}

# What 'collect_fields' selections expand to.
_COLLECT_EXPANSION = {
    'name_phone': ['name', 'phone'],
    'name_phone_issue': ['name', 'phone', 'issue'],
    'name_phone_address_issue': ['name', 'phone', 'address', 'issue'],
    'name_phone_address_issue_urgency': ['name', 'phone', 'address',
                                         'issue', 'urgency'],
}

# The line that makes one-party and two-party consent states both safe.
# Matched loosely on readback so an edited greeting is not double-stamped.
RECORDING_DISCLOSURE = 'This call may be recorded.'
_DISCLOSURE_HINT = re.compile(r'may\s+be\s+recorded', re.IGNORECASE)

_CLOSED_WORDS = {'closed', 'none', 'n/a', 'na', '-', 'shut', ''}
_TIME_RANGE_RE = re.compile(
    r'^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*'
    r'(?:-|to|–|—)\s*'
    r'(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$',
    re.IGNORECASE)


def _answers(onboarding):
    """``{question_key: value}``, registry defaults underneath."""
    out = {}
    try:
        from onboarding.registry import visible_sections
        for section in visible_sections(onboarding):
            for q in section.get('questions', []):
                default = q.get('default')
                if default:
                    out[q['key']] = default
    except Exception:
        logger.exception('voice.materialise: registry defaults failed')

    for r in onboarding.responses.all():
        if r.skipped:
            out.pop(r.question_key, None)
            continue
        out[r.question_key] = r.value or ''
    return out


def _to_time(hour, minute, ampm, *, is_end):
    hour = int(hour)
    minute = int(minute or 0)
    ampm = (ampm or '').lower()
    if ampm == 'pm' and hour != 12:
        hour += 12
    elif ampm == 'am' and hour == 12:
        hour = 0
    elif not ampm:
        # No marker. Business hours are the context, so a bare 1-7 on the
        # closing side means afternoon — "8 - 5" is never 8am to 5am.
        if is_end and 1 <= hour <= 7:
            hour += 12
    return time(min(hour, 23), minute)


def parse_hours(text):
    """``"8:00am - 5:00pm"`` -> ``(time(8,0), time(17,0))``, or None.

    None means closed, which is a real answer and not a failure — it is
    what makes the receptionist cover all of Sunday.
    """
    raw = (text or '').strip()
    if raw.lower() in _CLOSED_WORDS:
        return None
    match = _TIME_RANGE_RE.match(raw)
    if not match:
        logger.info('voice.materialise: unparseable hours %r', raw)
        return None
    sh, sm, sap, eh, em, eap = match.groups()
    # A range written "8:00 - 5:00pm" leaves the start unmarked; borrow
    # the end's marker only when that does not invert the range.
    start = _to_time(sh, sm, sap or ('am' if eap == 'pm' else eap),
                     is_end=False)
    end = _to_time(eh, em, eap, is_end=True)
    return (start, end)


def _write_hours(program, answers):
    """Replace the program's hours from the V2 answers.

    Full replace rather than merge: the wizard is the source of truth for
    these and a stale window left behind would silently widen or narrow
    the agent's coverage.
    """
    from voice.models import BusinessHoursWindow

    plan = []
    weekday = parse_hours(answers.get('hours_mon_fri', ''))
    if weekday:
        for day in range(5):
            plan.append((day, weekday[0], weekday[1]))
    saturday = parse_hours(answers.get('hours_saturday', ''))
    if saturday:
        plan.append((5, saturday[0], saturday[1]))
    sunday = parse_hours(answers.get('hours_sunday', ''))
    if sunday:
        plan.append((6, sunday[0], sunday[1]))

    program.hours.all().delete()
    for day, start, end in plan:
        BusinessHoursWindow.objects.create(
            program=program, day_of_week=day,
            start_time=start, end_time=end)
    return len(plan)


def _greeting_with_disclosure(greeting, recording_enabled):
    """Add the recording notice when recording is on, exactly once.

    The client is told at intake that we handle this line, so leaving it
    to them would be a promise we broke. Appending blindly would also be
    wrong: a client who edits their greeting in the portal and keeps the
    line must not end up with two.
    """
    text = (greeting or '').strip()
    if not recording_enabled:
        return text
    if _DISCLOSURE_HINT.search(text):
        return text
    return f'{text} {RECORDING_DISCLOSURE}'.strip()


def _attestation_text(onboarding, answers):
    """The exact wording the client agreed to, rebuilt from the labels
    that were on screen, so the exhibit can never describe different
    statements than the ones rendered."""
    lines = []
    try:
        from onboarding.registry import visible_sections
        for section in visible_sections(onboarding):
            if section.get('key') != 'V5':
                continue
            intro = section.get('intro') or ''
            if intro:
                lines.append(intro)
                lines.append('')
            for q in section.get('questions', []):
                key = q['key']
                if key not in _CONSENT_KEYS:
                    continue
                given = answers.get(key, '')
                mark = 'YES' if given in ('yes', 'true', 'True') else (
                    given or 'no answer')
                lines.append(f'[{mark}] {q["label"]}')
    except Exception:
        logger.exception('voice.materialise: attestation text failed')

    if not lines:
        lines = [
            'The client confirmed they understand callers will be told '
            'the call may be recorded, that Aspired Websites adds that '
            'disclosure to the greeting automatically, and that they are '
            'authorised to route the business number they gave us.'
        ]
    return '\n'.join(lines)


def _client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip() or None
    return request.META.get('REMOTE_ADDR') or None


def _write_attestation(program, onboarding, answers, request):
    """One attestation per onboarding, ever. A second run does not sign
    again — it is an append-only legal record, not a settings row."""
    from voice.models import VoiceConsentAttestation

    if VoiceConsentAttestation.objects.filter(
            onboarding=onboarding).exists():
        return None

    user = getattr(onboarding, 'user', None)
    return VoiceConsentAttestation.objects.create(
        program=program,
        onboarding=onboarding,
        user=user,
        version=ATTESTATION_VERSION,
        attestation_text=_attestation_text(onboarding, answers),
        answers={k: answers.get(k, '') for k in _CONSENT_KEYS},
        typed_name=(answers.get('attestation_typed_name') or '')[:200],
        typed_title=(answers.get('attestation_title') or '')[:120],
        agreed_at=timezone.now(),
        ip_address=_client_ip(request),
        user_agent=(request.META.get('HTTP_USER_AGENT', '')[:400]
                    if request is not None else ''),
    )


def _alert_operator(program):
    """Put the new program on the operator's radar.

    A SystemAlert rather than a SetupTodo: SetupTodo is the client's
    checklist and building their GHL agent is our work. Same split as
    ``reviews/materialise._alert_operator``.
    """
    try:
        from core.system_alerts import record_alert
        record_alert(
            severity='info',
            source='voice.onboarding_complete',
            message=f'Build the receptionist for {program.display_name}',
            detail=(
                'Receptionist onboarding is complete. Create the GHL '
                'Voice AI agent, attach a phone number, paste the '
                'location id / agent id / token on the program, then '
                'mark it live. Coverage: '
                f'{program.get_coverage_mode_display()}. Answer mode: '
                f'{program.get_answer_mode_display()}.'
            ),
        )
    except Exception:
        logger.exception('voice.materialise: operator alert failed')


def apply_onboarding(onboarding, *, request=None):
    """Build the VoiceProgram + hours + attestation. Returns the program.

    Idempotent on the program and the hours (both overwritten from the
    latest answers) and idempotent on the attestation (one per
    onboarding).
    """
    from clients.account_models import Account
    from voice.entitlement import program_for

    account = Account.objects.filter(user=onboarding.user).first()
    if account is None:
        logger.warning(
            'voice.materialise: no account for user %s', onboarding.user_id)
        return None

    program = program_for(account, create=True)
    answers = _answers(onboarding)

    for key, field in _FIELD_MAP.items():
        if key in answers:
            setattr(program, field, answers[key] or '')

    program.recording_enabled = answers.get(
        'recording_enabled', 'true') in ('yes', 'true', 'True', '1', True)
    program.greeting = _greeting_with_disclosure(
        program.greeting, program.recording_enabled)
    program.collect_fields = _COLLECT_EXPANSION.get(
        answers.get('collect_fields', ''),
        _COLLECT_EXPANSION['name_phone_address_issue'])

    for key, field, default in (
            ('ring_seconds', 'ring_seconds', 20),
            ('max_call_minutes', 'max_call_minutes', 10)):
        try:
            setattr(program, field, int(answers.get(key, default) or default))
        except (TypeError, ValueError):
            setattr(program, field, default)

    # The operator still has to build the agent in GHL and attach a
    # number, so the program is not live — it is waiting on us, and the
    # portal says so rather than implying the client is the blocker.
    if program.setup_status == 'awaiting_onboarding':
        program.setup_status = 'awaiting_number'

    # Everything above is config GHL does not have yet.
    program.config_synced_to_ghl = False
    program.config_updated_at = timezone.now()
    program.save()

    _write_hours(program, answers)
    _write_attestation(program, onboarding, answers, request)
    _alert_operator(program)

    return program
