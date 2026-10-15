"""
Promote a completed ``review_system`` onboarding into a ReviewProgram.

Called from ``onboarding.todo_models.build_todos_from_onboarding``, which
the wizard's ``complete()`` view invokes once. Two outputs:

1. A configured ``ReviewProgram`` with ``setup_status='awaiting_a2p'`` —
   everything the operator needs to submit the A2P brand and campaign
   and to build the client's GHL workflow.
2. Exactly one ``ReviewConsentAttestation`` — the signed legal record.

The attestation is why this module is careful rather than a loop of
``setattr``. It must capture what the client was *actually shown*, not a
reference to wording that will change, and it must never be written on
someone else's behalf.
"""

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

# Bump when the R5 wording changes materially. Old attestations keep
# their own version and their own snapshotted text.
ATTESTATION_VERSION = 'v1-2026-10'

_CONSENT_KEYS = (
    'consent_work_order_discloses',
    'consent_numbers_lawful',
    'consent_no_purchased_lists',
    'consent_no_gating',
    'consent_honor_stop',
)

# onboarding question_key -> ReviewProgram field, for the plain copies.
_FIELD_MAP = {
    'legal_business_name': 'legal_business_name',
    'dba_name': 'dba_name',
    'business_entity_type': 'entity_type',
    'business_street': 'business_street',
    'business_city': 'business_city',
    'business_state': 'business_state',
    'business_zip': 'business_zip',
    'business_phone': 'business_phone',
    'business_website_url': 'business_website_url',
    'support_email': 'support_email',
    'business_timezone': 'timezone',
    'google_review_url': 'google_review_url',
    'google_business_name': 'google_business_name',
    'send_delay': 'send_delay',
}


def _answers(onboarding):
    """``{question_key: value}`` for answers that were actually given.

    Explicitly skipped questions are excluded — a skip is a real answer
    ("I have no EIN") and must not be mistaken for a blank.

    Falls back to each question's registry ``default`` when no response
    row exists at all: the wizard saves on change, so a client who
    accepted the pre-filled message wording never generated a row for
    it, and without this their program would end up with no message.
    """
    out = {}
    try:
        from onboarding.registry import visible_sections
        for section in visible_sections(onboarding):
            for q in section.get('questions', []):
                default = q.get('default')
                if default:
                    out[q['key']] = default
    except Exception:
        logger.exception('reviews.materialise: registry defaults failed')

    for r in onboarding.responses.all():
        if r.skipped:
            out.pop(r.question_key, None)
            continue
        out[r.question_key] = r.value or ''
    return out


def _attestation_text(onboarding, answers):
    """The exact wording the client agreed to, rebuilt from the labels
    that were on screen.

    Built from the registry rather than a constant so it can never
    describe a different set of statements than the ones rendered. If
    the registry is unavailable we still record something truthful
    rather than an empty exhibit.
    """
    lines = []
    try:
        from onboarding.registry import visible_sections
        for section in visible_sections(onboarding):
            if section.get('key') != 'R5':
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
                mark = 'YES' if given == 'yes' else (given or 'no answer')
                lines.append(f'[{mark}] {q["label"]}')
    except Exception:
        logger.exception('reviews.materialise: attestation text failed')

    if not lines:
        lines = [
            'The client confirmed they disclose service texts on their '
            'work order, hold the right to text every number they send '
            'us, will not supply purchased lists, understand no review '
            'filtering or incentives are permitted, and understand STOP '
            'is permanent.'
        ]
    return '\n'.join(lines)


def _client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip() or None
    return request.META.get('REMOTE_ADDR') or None


def apply_onboarding(onboarding, *, request=None):
    """Build the ReviewProgram + attestation. Returns the program, or None.

    Idempotent on the program (fields are overwritten with the latest
    answers) and idempotent on the attestation (one per onboarding — a
    second run does not sign again).
    """
    from clients.account_models import Account
    from reviews.entitlement import program_for
    from reviews.templating import normalise_template
    from vault.crypto import server_encrypt

    user = onboarding.user
    account = Account.objects.filter(user=user).first()
    if account is None:
        logger.error(
            'reviews.materialise: no Account for user %s — cannot build a '
            'program', getattr(user, 'pk', '?'))
        return None

    program = program_for(account, create=True)
    answers = _answers(onboarding)

    for key, field in _FIELD_MAP.items():
        if key in answers:
            setattr(program, field, (answers[key] or '').strip())

    # Blank timezone would break every local-time calculation downstream.
    if not program.timezone:
        program.timezone = 'America/Chicago'

    # EIN: encrypted, with only the last four kept in the clear so the
    # admin page can confirm which number is on file without revealing
    # it. Sole proprietors legitimately have none.
    ein_raw = ''.join(c for c in (answers.get('ein') or '') if c.isdigit())
    if ein_raw:
        program.ein_encrypted = server_encrypt(ein_raw)
        program.ein_last4 = ein_raw[-4:]

    # Wording: normalised, so a client who pasted an incentive or
    # stripped the opt-out line cannot persist it even if they bypassed
    # the form validation.
    if 'sms_body_template' in answers:
        program.sms_body_template = normalise_template(
            answers['sms_body_template'], channel='sms')
    if 'email_subject_template' in answers:
        program.email_subject_template = (
            answers['email_subject_template'] or '').strip()[:200]
    if 'email_body_template' in answers:
        program.email_body_template = normalise_template(
            answers['email_body_template'], channel='email')

    program.reminder_enabled = answers.get('reminder_enabled', 'yes') != 'no'

    # The operator has to paste the approved wording into the GHL
    # workflow, so a fresh program starts out-of-sync by definition.
    program.wording_synced_to_ghl = False
    program.wording_updated_at = timezone.now()

    if program.setup_status == 'awaiting_onboarding':
        program.setup_status = 'awaiting_a2p'
    program.save()

    _write_attestation(program, onboarding, answers, request=request)
    _alert_operator(program)
    return program


def _write_attestation(program, onboarding, answers, *, request=None):
    """One attestation per onboarding, ever."""
    from reviews.models import ReviewConsentAttestation

    if ReviewConsentAttestation.objects.filter(
            onboarding=onboarding).exists():
        return None
    try:
        return ReviewConsentAttestation.objects.create(
            program=program,
            onboarding=onboarding,
            user=onboarding.user,
            version=ATTESTATION_VERSION,
            attestation_text=_attestation_text(onboarding, answers),
            answers={k: answers.get(k, '') for k in _CONSENT_KEYS},
            typed_name=(answers.get('attestation_typed_name') or '').strip(),
            typed_title=(answers.get('attestation_title') or '').strip(),
            agreed_at=onboarding.completed_at or timezone.now(),
            ip_address=_client_ip(request),
            user_agent=(
                request.META.get('HTTP_USER_AGENT', '')[:400]
                if request is not None else ''),
        )
    except Exception:
        logger.exception(
            'reviews.materialise: attestation write failed for onboarding '
            '%s — the program must not go live without one', onboarding.pk)
        return None


def _alert_operator(program):
    """Tell the operator there is an A2P submission to make.

    An alert, not a SetupTodo: SetupTodo is the client's checklist and
    this is our work.
    """
    try:
        from core.system_alerts import record_alert
        record_alert(
            severity='info',
            source='reviews.onboarding_complete',
            message=f'Submit A2P for {program.display_name}',
            detail=(
                'Review System onboarding is complete. Create the GHL '
                'sub-account, submit the A2P brand and campaign, then '
                'paste the location ID, token and sending number into '
                f'/admin-dashboard/reviews/{program.pk}/.'
            ),
        )
    except Exception:
        logger.exception('reviews.materialise: operator alert failed')
