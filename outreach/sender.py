"""
Cold-outreach email generator.

The Celery task ``run_cold_sender_task`` (outreach/tasks.py) calls
``generate_pending_cold_emails`` once a day. We pull every lead whose
next follow-up is due, generate Claude-written copy for the appropriate
sequence step, and write an ``EmailSent`` row with status set by the
trust-level dial:

    - Level 1                → ``pending_approval``
    - Level 2+               → ``approved`` (the drainer dispatches)

Per CLAUDE.md outreach rules:

    * Plain text only — no HTML, no images, no tracking pixels.
    * Max 4 touchpoints. Sequence stops on any reply.
    * From: ``zacherylong@aspiredwebsites.com`` only.
    * Suppression list (unsubscribes) is permanent — never re-contact.

Sequence cadence (business days between steps):

    Step 1  →  Step 2  : 3 days
    Step 2  →  Step 3  : 5 days
    Step 3  →  Step 4  : 7 days
    Step 4  →  done

The cap enforcement: at each generation tick we honour the
``effective_cap_for(today, settings.daily_send_cap)`` from
``outreach.warming`` minus rows already in ``status='sent'`` OR
``status='approved'`` for today (we count approved-waiting because
they WILL send today and would otherwise overshoot the cap).
"""

import datetime
import logging

from django.conf import settings as django_settings
from django.utils import timezone

from outreach.gating import should_queue_for_approval
from outreach.models import EmailSent, Lead, OutreachSettings, SuppressionList
from outreach.warming import effective_cap_for, outreach_blocked_today

logger = logging.getLogger(__name__)


# Business-day spacing between sequence steps. Index = current step;
# value = days to next. Step 0 → 1 has no entry (first touch is
# immediate when lead becomes eligible).
_STEP_CADENCE_DAYS = {
    1: 3,
    2: 5,
    3: 7,
}


def generate_pending_cold_emails(now=None):
    """
    Daily run. Returns a dict with counts for the task log:

        {
            'considered':   int,  # leads we looked at
            'generated':    int,  # EmailSent rows created
            'skipped_cap':  int,  # leads dropped because today's cap was hit
            'skipped_ai':   int,  # leads dropped because Claude errored
            'reason':       str,  # only set when blocked at the gate
        }

    Idempotent within a day: each lead can have at most one EmailSent
    row created per sequence_step, so re-runs of the same day skip
    leads that were already processed.
    """
    now = now or timezone.now()
    today = timezone.localdate()
    config = OutreachSettings.load()

    blocked, reason = outreach_blocked_today(date=today, settings_obj=config)
    if blocked:
        logger.info('cold sender skipped: %s', reason)
        return {
            'considered': 0, 'generated': 0,
            'skipped_cap': 0, 'skipped_ai': 0, 'reason': reason,
        }

    cap = effective_cap_for(today, config.daily_send_cap)
    if cap <= 0:
        return {
            'considered': 0, 'generated': 0,
            'skipped_cap': 0, 'skipped_ai': 0,
            'reason': 'Cap is 0 for today.',
        }

    # Already-counted-against-cap = sent + approved-waiting today. We
    # use created_at for approved rows (they were generated today and
    # WILL dispatch today), sent_at for sent rows.
    sent_today = EmailSent.objects.filter(
        status='sent', sent_at__date=today, kind='cold'
    ).count()
    approved_today = EmailSent.objects.filter(
        status='approved', created_at__date=today, kind='cold'
    ).count()
    budget_left = cap - sent_today - approved_today
    if budget_left <= 0:
        return {
            'considered': 0, 'generated': 0,
            'skipped_cap': sent_today + approved_today,
            'skipped_ai': 0,
            'reason': f'Daily cap of {cap} already met.',
        }

    eligible = _eligible_leads(now=now, limit=budget_left * 2)

    counts = {
        'considered': 0, 'generated': 0,
        'skipped_cap': 0, 'skipped_ai': 0, 'reason': '',
    }
    suppressed_emails = set(
        SuppressionList.objects.values_list('email', flat=True))

    for lead in eligible:
        if counts['generated'] >= budget_left:
            counts['skipped_cap'] += 1
            continue
        counts['considered'] += 1

        # Hard suppression check — survives any race where the lead
        # row's `unsubscribed` flag wasn't synced to SuppressionList yet.
        if lead.email and lead.email.lower() in suppressed_emails:
            continue

        step = lead.sequence_step + 1
        if step > 4:
            continue

        # Idempotency: never create a second EmailSent row for the same
        # (lead, step) combination on any status.
        if EmailSent.objects.filter(lead=lead, sequence_step=step).exists():
            continue

        try:
            subject, body = _generate_email_copy(lead, step)
        except Exception:  # noqa: BLE001
            logger.exception('cold sender: AI generation failed for %s', lead.pk)
            counts['skipped_ai'] += 1
            continue

        queue_for_approval = should_queue_for_approval('cold')
        status = 'pending_approval' if queue_for_approval else 'approved'
        EmailSent.objects.create(
            lead=lead,
            kind='cold',
            status=status,
            subject=subject,
            body=body,
            from_email=_from_address(),
            sequence_step=step,
            approved_at=None if queue_for_approval else now,
        )

        # Advance the lead's sequence pointer immediately so a re-run
        # of the task in the same day doesn't try the same step again,
        # AND so the next followup date is in the future. The Lead's
        # last_contacted_at moves only when the drainer actually sends.
        lead.sequence_step = step
        lead.next_followup_at = _next_followup_at(step, now)
        lead.save(update_fields=['sequence_step', 'next_followup_at', 'updated_at'])
        counts['generated'] += 1

    return counts


def _eligible_leads(now, limit):
    """
    Leads ready for the next sequence touch.

    Eligibility:
      - has an email address
      - not unsubscribed
      - not sequence_paused
      - sequence_step < 4 (room for at least one more touch)
      - next_followup_at is null (never contacted) OR <= now
      - has not replied to any prior email in the sequence

    Highest-score first so we burn the daily cap on the best leads.
    """
    qs = (
        Lead.objects
        .filter(unsubscribed=False, sequence_paused=False)
        .exclude(email='')
        .filter(sequence_step__lt=4)
    )
    qs = qs.filter(
        next_followup_at__isnull=True
    ) | qs.filter(next_followup_at__lte=now)
    qs = qs.exclude(
        # Any inbound reply on this lead pauses outbound forever.
        replies__isnull=False
    ).distinct().order_by('-score', '-created_at')
    return list(qs[:limit])


def _next_followup_at(step_just_generated, now):
    """When the NEXT step should fire — None if this was the last step."""
    days = _STEP_CADENCE_DAYS.get(step_just_generated)
    if days is None:
        return None
    return now + datetime.timedelta(days=days)


def _from_address():
    """The single From address per CLAUDE.md. Never an alias or subdomain."""
    return getattr(
        django_settings, 'OUTREACH_FROM_EMAIL',
        'zacherylong@aspiredwebsites.com')


def _generate_email_copy(lead, step):
    """
    Call Claude to generate (subject, body). Plain text, no HTML.

    The system prompt + per-step user prompt are tuned to Aspired's
    voice — professional, direct, security-first positioning. Future:
    A/B variants via OutreachSettings.
    """
    from reporting.ai import MODEL_CONTENT, claude_complete

    system = _system_prompt()
    user_prompt = _user_prompt_for_step(lead, step)
    text = claude_complete(
        messages=[{'role': 'user', 'content': user_prompt}],
        system=system,
        model=MODEL_CONTENT,
        max_tokens=600,
    )
    return _split_subject_body(text, lead, step)


def _system_prompt():
    return (
        "You are Zachery Long, founder of Aspired Websites LLC — a custom "
        "web design agency serving law firms and small businesses in Texas "
        "and Georgia. You have a Masters in Cybersecurity and CISSP "
        "certification; security is the firm's primary differentiator."
        "\n\n"
        "Write cold outreach emails as if you were writing to one person — "
        "plain text only, no HTML, no images, no signature block beyond "
        "your name. Friendly and direct, never salesy. 60–120 words max. "
        "Reference one specific thing about the recipient's business or "
        "website if it's in the lead data. Never make up a fact about them."
        "\n\n"
        "Format your reply exactly as:\n"
        "Subject: <one line subject under 60 chars>\n"
        "\n"
        "<the email body>\n"
        "\n"
        "— Zachery\n"
    )


def _user_prompt_for_step(lead, step):
    facts = []
    facts.append(f'- Business name: {lead.firm_name}')
    # attorney_name is law-firm-first but used for all contact types
    # per CLAUDE.md → Data Model Decisions.
    if lead.attorney_name:
        facts.append(f'- Contact: {lead.attorney_name}')
    if lead.business_type:
        facts.append(f'- Industry: {lead.business_type}')
    loc_parts = [p for p in (lead.city, lead.state) if p]
    if loc_parts:
        facts.append(f'- Location: {", ".join(loc_parts)}')
    if lead.website:
        facts.append(f'- Website: {lead.website}')
    if lead.website_mobile_score is not None:
        facts.append(
            f'- Their site PageSpeed (mobile): '
            f'{lead.website_mobile_score}/100')
    if lead.has_ssl is False:
        facts.append('- Their site is NOT served over HTTPS (security issue).')

    step_brief = {
        1: (
            'STEP 1 — first touch. Introduce yourself briefly, mention one '
            'specific observation about their business (e.g. their PageSpeed '
            'score, missing HTTPS, or location-based reference if no other '
            'detail is available). End with a single low-friction question '
            '(reply yes/no). Do NOT pitch services in the first email.'),
        2: (
            'STEP 2 — follow up to a step-1 email that received no reply. '
            'Mention you reached out previously. Offer one concrete '
            'value-add observation (a specific improvement you would make). '
            'Keep it shorter than step 1.'),
        3: (
            'STEP 3 — second follow-up. Acknowledge they may be busy. Offer '
            'one resource or a 15-minute call. Brief — 3-4 sentences max.'),
        4: (
            'STEP 4 — break-up email. Brief and warm. Say this is the last '
            'email, leave the door open for them to reach out later.'),
    }[step]

    return (
        'Write a cold outreach email.\n\n'
        'About the recipient:\n'
        + '\n'.join(facts)
        + '\n\n'
        + step_brief
    )


def _split_subject_body(text, lead, step):
    """
    Pull out the Subject: line. Falls back to a safe default if Claude
    didn't follow the format.
    """
    lines = text.strip().splitlines()
    subject = ''
    body_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.lower().startswith('subject:'):
            subject = stripped.split(':', 1)[1].strip()
            body_start = i + 1
            break
    if not subject:
        subject = f'Quick question, {lead.firm_name}'
    body = '\n'.join(lines[body_start:]).strip()
    if not body:
        body = text.strip()
    return subject[:255], body
