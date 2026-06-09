"""
Phase 4.0 — clients/services.py — pure-function business logic.

The AI assistant (Phase 4.1+) and the admin UI both call into these
functions, so the canonical mutation logic lives in ONE place instead
of being copy-pasted across views.

Conventions:
  - No `request` argument anywhere. Pass plain values + the model.
  - Every guard raises a specific exception (ValueError / PermissionError
    / GuardError) with a human-readable message — UI shows it,
    AI assistant relays it back to the operator.
  - Side effects (email, log row) are inside the function — callers
    don't have to remember to do them. Email failures DO NOT block the
    state mutation (best-effort).
  - Functions return either the row that was created/updated OR a
    tuple including a notify-flag where useful.
"""

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


class GuardError(Exception):
    """Raised when a state transition is blocked by a policy guard
    (e.g. `mark_live` while payment_status != 'fully_paid'). The
    message must be human-readable — surfaced verbatim by the UI."""


# ─────────────────────────────────────────────────────────────────────────────
# Stage change
# ─────────────────────────────────────────────────────────────────────────────

def change_client_stage(profile, new_stage, *, set_by='AI assistant',
                        note=''):
    """Move the client's project to a new stage, log it, notify them.

    Guards:
      - Refuses an unknown stage (ValueError).
      - Refuses 'live' unless payment_status == 'fully_paid' (GuardError) —
        CLAUDE.md "Site ownership" + "Payment before work starts" rules.

    Returns:
      (ProjectStageLog, notified: bool) — notified is False if the
      stage-change email failed or had no copy for this stage.
    """
    from clients.emails import send_stage_change_email
    from clients.models import PROJECT_STAGES, ProjectStageLog

    valid = {s for s, _ in PROJECT_STAGES}
    if new_stage not in valid:
        raise ValueError(
            f'Unknown stage "{new_stage}". '
            f'Valid stages: {", ".join(sorted(valid))}')
    if new_stage == 'live' and profile.payment_status != 'fully_paid':
        raise GuardError(
            f'Cannot move {profile.firm_name} to "live" — final payment '
            f'has not cleared yet (payment_status='
            f'{profile.payment_status}).')

    from_stage = profile.stage
    if from_stage == new_stage:
        # Idempotent: no DB write, no log, no email.
        return None, False

    profile.stage = new_stage
    profile.save(update_fields=['stage', 'updated_at'])

    log = ProjectStageLog.objects.create(
        client=profile,
        from_stage=from_stage,
        to_stage=new_stage,
        note=note or '',
        set_by=set_by,
        client_notified=False,
    )

    # Best-effort email — failure here doesn't block the state change.
    notified = False
    try:
        send_stage_change_email(profile, new_stage)
        notified = True
    except Exception:
        logger.exception(
            'change_client_stage: stage-change email failed for %s',
            profile.pk)

    if notified:
        from django.utils import timezone
        log.client_notified = True
        log.notification_sent_at = timezone.now()
        log.save(update_fields=[
            'client_notified', 'notification_sent_at', 'updated_at'])

    return log, notified


# ─────────────────────────────────────────────────────────────────────────────
# Intake completion
# ─────────────────────────────────────────────────────────────────────────────

def mark_intake_complete(profile):
    """Flip the client's IntakeResponse to complete + advance stage.

    Idempotent — if intake is already complete, no-op.

    Returns the IntakeResponse.
    """
    from django.utils import timezone
    from clients.models import IntakeResponse

    intake, _ = IntakeResponse.objects.get_or_create(client=profile)
    if intake.completed:
        return intake
    intake.completed = True
    intake.completed_at = timezone.now()
    intake.save(update_fields=[
        'completed', 'completed_at', 'updated_at'])
    return intake


# ─────────────────────────────────────────────────────────────────────────────
# Revisions
# ─────────────────────────────────────────────────────────────────────────────

def add_revision(profile, description, *, is_major=True, source='ai_assistant'):
    """Create a RevisionRequest. If the revision pushes the client over
    their `revision_limit`, also create a pending MiniInvoice for the
    out-of-scope work (the admin sets the amount + sends it via the
    Phase 1.3 admin action).

    Guards:
      - Refuses while there's any unpaid out-of-scope MiniInvoice
        (Phase 1.4 work-blocking rule).

    Returns:
      (RevisionRequest, mini_invoice_or_None)
    """
    from clients.models import RevisionRequest

    if profile.has_unpaid_out_of_scope():
        raise GuardError(
            f'{profile.firm_name} has an unpaid out-of-scope invoice — '
            f'cannot accept new revisions until it clears.')

    revision = RevisionRequest.objects.create(
        client=profile,
        description=(description or '').strip(),
        is_major=bool(is_major),
        source=source,
        counts_against_limit=bool(is_major),
    )

    if is_major:
        profile.revision_count += 1
        profile.save(update_fields=['revision_count', 'updated_at'])

    mini = None
    if profile.revision_count > profile.revision_limit:
        from billing.models import MiniInvoice
        revision.status = 'out_of_scope'
        revision.save(update_fields=['status', 'updated_at'])
        mini = MiniInvoice.objects.create(
            client=profile,
            revision=revision,
            description=(f'Out-of-scope revision: '
                         f'{revision.description[:120]}'),
            amount=0,           # admin sets the amount then sends
            hours=0,
            status='pending',
        )

    return revision, mini


# ─────────────────────────────────────────────────────────────────────────────
# Mark live (final payment + launch)
# ─────────────────────────────────────────────────────────────────────────────

def mark_live(profile, *, set_by='AI assistant'):
    """Move the project to 'live'. Hard-gated on `payment_status ==
    'fully_paid'` — never mark live without final payment clearing.

    Returns whatever change_client_stage returns.
    """
    return change_client_stage(
        profile, 'live',
        set_by=set_by, note='Launched')


# ─────────────────────────────────────────────────────────────────────────────
# Approve staging (review → pre_launch)
# ─────────────────────────────────────────────────────────────────────────────

def approve_staging(profile, *, set_by='AI assistant'):
    """Move review → pre_launch. If the deposit is paid but final isn't,
    a final invoice should be issued (done by Phase 1's billing flow on
    stage transition — we don't double-fire here, just move the stage)."""
    return change_client_stage(
        profile, 'pre_launch',
        set_by=set_by, note='Staging approved')


# ─────────────────────────────────────────────────────────────────────────────
# Out-of-scope invoice
# ─────────────────────────────────────────────────────────────────────────────

def create_out_of_scope_invoice(profile, description, amount, *,
                                hours=None):
    """Create a MiniInvoice for ad-hoc out-of-scope work (NOT tied to a
    RevisionRequest — those use add_revision). Amount must be > 0.

    Returns the MiniInvoice (status='pending'). To actually send it via
    Stripe, run the Phase 1.3 admin action."""
    from billing.models import MiniInvoice

    amt = Decimal(str(amount or 0))
    if amt <= 0:
        raise ValueError(
            f'Out-of-scope invoice amount must be > 0 (got {amount!r})')

    return MiniInvoice.objects.create(
        client=profile,
        description=(description or '').strip()[:255],
        amount=amt,
        hours=Decimal(str(hours or 0)),
        status='pending',
    )


# ─────────────────────────────────────────────────────────────────────────────
# Get status (read-only — for the AI assistant preview card)
# ─────────────────────────────────────────────────────────────────────────────

def get_client_status(profile):
    """Return a plain dict snapshot used by the AI assistant preview
    card so the operator sees the current state alongside the proposed
    action."""
    from clients.models import SupportTicket
    open_tickets = SupportTicket.objects.filter(
        client=profile, status__in=['open', 'in_progress']).count()
    has_unpaid_mini = profile.has_unpaid_out_of_scope()
    return {
        'firm_name': profile.firm_name,
        'stage': profile.stage,
        'payment_status': profile.payment_status,
        'revision_count': profile.revision_count,
        'revision_limit': profile.revision_limit,
        'over_revision_limit': (
            profile.revision_count >= profile.revision_limit),
        'launch_date': profile.launch_date,
        'maintenance_active': profile.maintenance_active,
        'site_status': profile.site_status,
        'has_unpaid_out_of_scope': has_unpaid_mini,
        'open_tickets': open_tickets,
        'package': profile.package,
        'payment_failure_active': (
            profile.payment_failure_started_at is not None),
    }
