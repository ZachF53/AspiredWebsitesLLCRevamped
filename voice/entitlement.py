""""Does this client have the AI Voice Receptionist?" — one answer, one place.

This is the sibling of ``reviews/entitlement.py``, and it is deliberately
a **separate module rather than a generalisation** of it. That one
hardcodes review slugs and resolves four grant paths; this one resolves
two. Merging them would mean a parameterised resolver whose hardest case
(bundled-vs-standalone precedence) does not apply here at all.

Two grant paths, and the absence of a third is the product decision:

  * An operator comp — ``Account.comp_voice_receptionist``.
  * A standalone purchase — a ``MaintenancePlan`` row on
    ``hvac-voice-receptionist`` (``category='addon'`` sales ride the
    maintenance plan-row shape; see
    ``billing.checkout_views._provision_product_type``).

**No plan bundles it.** There is no Full Plan path, no
``Website.package`` path, and no ``*_BUNDLED_PACKAGES`` tuple to consult.
The receptionist bills real per-minute vendor cost on every call and is
the only product sellable to a lead with a phone and no website — see
``clients/contract_options.VOICE_RECEPTIONIST_SLUG``.
``clients/tests_never_bundled.py`` asserts that stays true.

Any surface that branches on receptionist access must call
``voice_entitlement`` and nothing else.
"""

import logging
from dataclasses import dataclass

from clients.contract_options import (
    HOSTING_SECURITY_SLUG, VOICE_RECEPTIONIST_SLUG,
)

logger = logging.getLogger(__name__)

# 'cancelled' means "cancels at period end" in this codebase and still
# grants access; 'ended' is terminal and does not. 'paused' is a paid-but-
# suspended state and deliberately does not serve.
_SERVED_STATUSES = ('active', 'awaiting_payment', 'cancelled')


@dataclass(frozen=True)
class VoiceEntitlement:
    has_access: bool = False
    source: str = ''               # '' | 'standalone' | 'comp'
    plan: object = None            # MaintenancePlan carrying the sub, or None
    program: object = None         # VoiceProgram, or None
    setup_status: str = ''
    paused: bool = False
    onboarding: object = None      # the voice_receptionist Onboarding row
    onboarding_complete: bool = False
    can_buy_standalone: bool = False
    current_plan_slug: str = ''
    is_hosting_only: bool = False
    usage: object = None           # the open VoiceUsagePeriod, or None

    @property
    def needs_onboarding(self):
        return self.has_access and not self.onboarding_complete

    @property
    def is_answering(self):
        return (self.has_access and self.setup_status == 'live'
                and not self.paused)

    @property
    def is_over_allowance(self):
        return bool(self.usage and self.usage.is_over)


def _active_plans(account):
    try:
        return list(account.maintenance_plans.filter(
            status__in=_SERVED_STATUSES))
    except Exception:
        return []


def voice_entitlement(account, *, website=None):
    """Resolve receptionist access for an account. Never raises."""
    if account is None:
        return VoiceEntitlement()

    plans = _active_plans(account)
    plan_slugs = [p.tier_slug for p in plans]
    # "Your current plan" for display. The receptionist is never the
    # answer to that question — it is an add-on, not a plan — so it is
    # excluded from the label even when it is the only row present.
    current_plan_slug = next(
        (s for s in plan_slugs if s != VOICE_RECEPTIONIST_SLUG),
        '',
    )

    source, plan = '', None

    # 1 — an operator comp.
    if getattr(account, 'comp_voice_receptionist', False):
        source = 'comp'

    # 2 — a standalone purchase.
    if not source:
        for p in plans:
            if p.tier_slug == VOICE_RECEPTIONIST_SLUG:
                source, plan = 'standalone', p
                break

    has_access = bool(source)
    program = program_for(account, website=website)
    onboarding = _onboarding_row(account)

    return VoiceEntitlement(
        has_access=has_access,
        source=source,
        plan=plan,
        program=program,
        setup_status=(program.setup_status if program else ''),
        paused=bool(program and program.paused),
        onboarding=onboarding,
        onboarding_complete=bool(
            onboarding and onboarding.completed_at is not None),
        can_buy_standalone=not has_access,
        current_plan_slug=current_plan_slug,
        is_hosting_only=(not has_access
                         and current_plan_slug == HOSTING_SECURITY_SLUG),
        usage=current_usage(program),
    )


def has_voice_receptionist(account):
    """Cheap boolean for the sidebar. Expresses intent at the call site."""
    return voice_entitlement(account).has_access


def _onboarding_row(account):
    """The account's ``voice_receptionist`` Onboarding row, or None."""
    user = getattr(account, 'user', None)
    if user is None:
        return None
    try:
        from onboarding.models import Onboarding
        return (Onboarding.objects
                .filter(user=user, product_type='voice_receptionist')
                .order_by('-started_at')
                .first())
    except Exception:
        return None


def program_for(account, *, website=None, create=False):
    """The account's VoiceProgram, optionally creating it.

    Resolves a website-specific row first, then the account-level row, so
    a single-site client and a two-brand client both get the right one.

    Creating also puts the program on the current price cohort, because
    a program with no cohort has no locked price and would be invisible
    to the "spots remaining" counter — which would then let an 11th
    account buy at the 10-account price.
    """
    if account is None:
        return None
    from voice.models import VoiceProgram

    qs = VoiceProgram.objects.filter(account=account)
    program = None
    if website is not None:
        program = qs.filter(website=website).first()
    if program is None:
        program = qs.filter(website__isnull=True).first()
    if program is None:
        program = qs.first()
    if program is None and create:
        program = VoiceProgram.objects.create(account=account)
        try:
            from voice.cohorts import assign_to_current, maybe_escalate
            assign_to_current(program)
            maybe_escalate()
        except Exception:
            logger.exception(
                'voice.entitlement: cohort assignment failed for %s',
                program.pk)
    return program


def current_usage(program):
    """The open usage period for a program, or None.

    Open means "now falls inside it". Deliberately does not create one —
    the webhook creates periods as calls arrive, so a client who has
    simply not been called yet correctly shows no usage rather than a
    0/300 row that implies the month has started billing.
    """
    if program is None:
        return None
    try:
        from django.utils import timezone
        now = timezone.now()
        return (program.usage_periods
                .filter(period_start__lte=now, period_end__gt=now)
                .first())
    except Exception:
        return None


def nav_state(account):
    """Sidebar state: ``{'has', 'setup_status', 'paused', 'badge'}``.

    The badge carries state onto every page. 'Over' outranks the setup
    badges because a client burning paid overage needs to know that now,
    not whenever they next open the receptionist tab.
    """
    blank = {'has': False, 'setup_status': '', 'paused': False, 'badge': ''}
    if account is None:
        return blank
    try:
        ent = voice_entitlement(account)
    except Exception:
        return blank

    badge = ''
    if ent.has_access:
        if ent.is_over_allowance:
            badge = 'Over'
        elif ent.needs_onboarding:
            badge = '!'
        elif ent.setup_status == 'awaiting_number':
            badge = 'Setup'
        elif ent.paused:
            badge = 'Paused'
    return {
        'has': ent.has_access,
        'setup_status': ent.setup_status,
        'paused': ent.paused,
        'badge': badge,
    }
