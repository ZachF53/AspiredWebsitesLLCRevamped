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
                        note='', payment_verified=False):
    """Move the client's project to a new stage, log it, notify them.

    Guards:
      - Refuses an unknown stage (ValueError).
      - Refuses 'live' unless payment_status == 'fully_paid' (GuardError) —
        CLAUDE.md "Site ownership" + "Payment before work starts" rules.
      - Refuses 'live' when that fully_paid claim has no ledger evidence
        behind it, unless the caller passes payment_verified=True to
        record that a human checked. See clients.payment_evidence.

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
            f'Cannot move {profile.name} to "live" — final payment '
            f'has not cleared yet (payment_status='
            f'{profile.payment_status}).')

    # `fully_paid` is a plain field any screen, webhook, or backfill can
    # set. Before it releases the launch gate, something in the ledger has
    # to corroborate it — otherwise we launch on the strength of a flag
    # nobody checked. Payments taken outside Stripe are legitimate; they
    # just have to be acknowledged deliberately via payment_verified.
    if new_stage == 'live' and not payment_verified:
        from clients.payment_evidence import (
            is_fully_paid_without_evidence, unverified_payment_message)

        # `profile` IS the site being launched. This used to resolve the
        # account's oldest website instead, which on a multi-site account
        # checked the wrong site's ledger — and once `profile` became a
        # Website, resolved to nothing at all and left the gate open.
        if is_fully_paid_without_evidence(profile):
            raise GuardError(unverified_payment_message(profile))

    from_stage = profile.stage
    if from_stage == new_stage:
        # Idempotent: no DB write, no log, no email.
        return None, False

    profile.stage = new_stage
    profile.save(update_fields=['stage', 'updated_at'])

    log = ProjectStageLog.objects.create(
        website_new=profile,
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

def mark_intake_complete(profile, *, set_by='admin'):
    """Admin override — flip a Website's intake to complete and unlock
    the onboarding gate, WITHOUT triggering droplet provisioning or the
    client confirmation email (those belong to the client's own
    submission — see `_on_intake_submitted` in clients/views.py).

    The one function every "mark intake complete" entry point calls:
    the v1 admin override button, the v2 admin override button, and the
    AI assistant's "mark X intake complete" command. Used to unblock a
    client who doesn't need the form (legacy import, phone/email
    intake, test account) or is stuck on a stale gate — CLAUDE.md's own
    command-pattern spec for this ("Intake.completed = True, stage
    unlocked") requires flipping BOTH the IntakeResponse and the
    Website's onboarding gate; a version that only touched the
    IntakeResponse left the portal decorator's `pending_intake` check
    (clients/decorators.py) still locking the client out after this
    supposedly unblocked them.

    Idempotent — an already-complete intake / already-advanced website
    is a no-op on those fields; the audit log entry is written every
    call regardless, same as a stage change's own audit trail.

    Returns the IntakeResponse.
    """
    from django.utils import timezone

    from clients.account_models import Website, WebsiteStageLog
    from clients.models import IntakeResponse

    intake, _ = IntakeResponse.objects.get_or_create(website_new=profile)
    if not intake.completed:
        intake.completed = True
        intake.completed_at = timezone.now()
        intake.save(update_fields=[
            'completed', 'completed_at', 'updated_at'])

    if isinstance(profile, Website):
        if profile.onboarding_status == 'pending_intake':
            profile.onboarding_status = 'intake_complete'
            profile.save(update_fields=['onboarding_status', 'updated_at'])
        WebsiteStageLog.objects.create(
            website=profile,
            from_stage=profile.stage,
            to_stage=profile.stage,  # no stage change, just an annotation
            note='Intake marked complete by admin override (no droplet).',
            set_by=set_by,
        )

    return intake


# ─────────────────────────────────────────────────────────────────────────────
# Contract signing — admin override
# ─────────────────────────────────────────────────────────────────────────────

def admin_mark_contract_signed(contract, *, reason, set_by='admin'):
    """Admin override — record a Contract as signed WITHOUT a real
    e-signature. For a client who signed a physical copy, signed over
    email, or agreed by phone — outside the portal's own
    clients:contract_sign flow.

    A real signature (clients:contract_sign) stamps signed_ip,
    signed_user_agent, and signed_content_hash — captured specifically
    for ESIGN/UETA enforceability (see the Contract model's Phase 2.3
    comment). This override deliberately leaves all three blank: there
    is no browser session to capture them from, and writing fake values
    would make an unevidenced signature indistinguishable from a real
    one if this contract were ever disputed. `reason` is required and
    becomes part of signed_name (prefixed "Admin override:"), so the
    record itself — everywhere signed_name is shown, including the
    generated PDF — visibly says this isn't a real e-signature rather
    than silently looking like one.

    Idempotent — an already-signed contract is returned unchanged.
    """
    from django.utils import timezone

    from clients.account_models import WebsiteStageLog

    if contract.signed:
        return contract

    reason = (reason or '').strip()
    if not reason:
        raise GuardError(
            'A reason is required to mark a contract signed without a '
            'real signature.')

    contract.signed = True
    contract.signed_at = timezone.now()
    contract.signed_name = f'Admin override: {reason}'
    contract.save(update_fields=['signed', 'signed_at', 'signed_name', 'updated_at'])

    website = contract.website_new
    if website is not None:
        WebsiteStageLog.objects.create(
            website=website,
            from_stage=website.stage,
            to_stage=website.stage,  # no stage change, just an annotation
            note=(f'Contract marked signed by admin override — no '
                  f'e-signature captured. Reason: {reason}'),
            set_by=set_by,
        )

    return contract


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance onboarding — admin override
# ─────────────────────────────────────────────────────────────────────────────

def resolve_maintenance_onboarding(website):
    """(MaintenancePlan, Onboarding) for a website's active maintenance
    plan, or (None, None) / (plan, None) when either doesn't exist yet.

    The Onboarding row (`onboarding` app — the post-purchase wizard:
    current site details, access handoff, approval workflow) is keyed
    on (user, product_type, tier_slug), the same key the wizard itself
    uses — NOT on the website directly, since Onboarding predates the
    per-website MaintenancePlan split and is account/user-level.
    """
    plan = website.maintenance_plans.filter(status='active').first()
    if plan is None:
        return None, None

    from onboarding.models import Onboarding

    ob = (Onboarding.objects
          .filter(user=website.account.user, product_type='maintenance',
                  tier_slug=plan.tier_slug)
          .order_by('-started_at').first())
    return plan, ob


def admin_mark_maintenance_onboarding_complete(website, *, set_by='admin'):
    """Admin override — mark a client's maintenance Onboarding complete
    without requiring every question answered. For a client who
    explained everything by phone/email, or is otherwise a known
    quantity.

    Returns None (no-op) if there's no active maintenance plan on this
    website, or no Onboarding row has been created for it yet —
    nothing exists to mark complete.

    Unlike a real completion (onboarding.views.complete), this does NOT
    call build_todos_from_onboarding — there are no real answers to
    build SetupTodo rows from.

    Idempotent — an already-complete row is returned unchanged.
    """
    from django.utils import timezone

    from clients.account_models import WebsiteStageLog

    _plan, ob = resolve_maintenance_onboarding(website)
    if ob is None:
        return None

    if ob.completed_at is None:
        ob.completed_at = timezone.now()
        ob.save(update_fields=['completed_at'])
        WebsiteStageLog.objects.create(
            website=website,
            from_stage=website.stage,
            to_stage=website.stage,  # no stage change, just an annotation
            note='Maintenance intake marked complete by admin override.',
            set_by=set_by,
        )

    return ob


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
            f'{profile.name} has an unpaid out-of-scope invoice — '
            f'cannot accept new revisions until it clears.')

    revision = RevisionRequest.objects.create(
        website_new=profile,
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
            website_new=profile,
            account_new=profile.account,
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

def mark_live(profile, *, set_by='AI assistant', payment_verified=False):
    """Move the project to 'live'. Hard-gated on `payment_status ==
    'fully_paid'` — never mark live without final payment clearing.

    `payment_verified=True` records that an operator confirmed a payment
    the ledger cannot show (cheque, Zelle, bank transfer). It does not
    fabricate a PaymentRecord; it only states that a human looked.

    Returns whatever change_client_stage returns.
    """
    return change_client_stage(
        profile, 'live',
        set_by=set_by, note='Launched',
        payment_verified=payment_verified)


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
        website_new=profile,
        account_new=profile.account,
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
    # Per site: a ticket is raised about a specific build.
    open_tickets = SupportTicket.objects.filter(
        website_new=profile, status__in=['open', 'in_progress']).count()
    has_unpaid_mini = profile.has_unpaid_out_of_scope()
    return {
        'firm_name': profile.name,
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
            # Dunning is account-level: one card, one relationship.
            getattr(profile.account, 'payment_failure_started_at', None)
            is not None),
    }
