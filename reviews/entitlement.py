"""
"Does this client have the Review System?" — one answer, one place.

Entitlement is **not** a single row, which is the whole reason this
module exists:

  * A Full Plan client (``hvac-full-plan`` or ``hvac-plan-paid-in-full``)
    gets reviews bundled, with no review-specific plan row at all.
  * A standalone buyer has a ``MaintenancePlan`` on
    ``hvac-review-system`` — ``category='addon'`` sales ride the
    maintenance plan-row shape, see
    ``billing.checkout_views._provision_product_type``.
  * An operator can comp it via ``Account.comp_maintenance_package``.
  * A contract-signed client may only have it on ``Website.package``,
    because ``billing/plan_billing.py`` keys differently.

Any surface that branches on review access must call
``review_entitlement`` and nothing else. Four readers today: the sidebar
nav (via the ``portal_services`` context processor), the portal hub, the
public status endpoint, and the admin queue.
"""

from dataclasses import dataclass

from clients.contract_options import (
    FULL_PLAN_SLUGS,
    HOSTING_SECURITY_SLUG,
    REVIEW_SYSTEM_SLUG,
    REVIEWS_BUNDLED_PACKAGES,
)

# A plan the client is still being served under. 'cancelled' means
# "cancels at period end" in this codebase — they keep the service until
# then, the same rule cancel_maintenance_subscription already encodes.
# 'ended' is the terminal state and does NOT grant access.
_SERVED_STATUSES = ('active', 'awaiting_payment', 'cancelled')


@dataclass(frozen=True)
class ReviewEntitlement:
    """Everything a template or view needs to branch, computed once."""

    has_access: bool = False
    source: str = ''               # '' | 'full_plan' | 'standalone' | 'comp'
    plan: object = None            # MaintenancePlan carrying the sub, or None
    program: object = None         # ReviewProgram, or None
    setup_status: str = ''
    paused: bool = False
    onboarding: object = None      # the review_system Onboarding row
    onboarding_complete: bool = False
    can_buy_standalone: bool = False
    can_upgrade_full_plan: bool = False
    current_plan_slug: str = ''
    is_hosting_only: bool = False

    @property
    def needs_onboarding(self):
        """They are entitled but we don't yet have what we need to submit
        the A2P registration."""
        return self.has_access and not self.onboarding_complete

    @property
    def is_sendable(self):
        return (self.has_access and self.setup_status == 'live'
                and not self.paused)


def _active_plans(account):
    """Served maintenance plans, newest first. One query, reused."""
    return list(
        account.maintenance_plans
        .filter(status__in=_SERVED_STATUSES)
        .order_by('-started_at', '-created_at')
    )


def _comp_slug(account):
    """``comp_maintenance_package`` as a tier slug, or ''.

    Comp fields use underscores ('hvac_full_plan'); tier slugs use
    dashes. ``_maintenance_upsell_state`` does the same conversion.
    """
    comp = getattr(account, 'comp_maintenance_package', '') or ''
    return comp.replace('_', '-')


def review_entitlement(account, *, website=None):
    """Resolve review access for an account. Never raises.

    Resolution order, first match wins — bundled beats standalone so a
    Full Plan client is never offered the $97 add-on they already have.
    """
    if account is None:
        return ReviewEntitlement()

    plans = _active_plans(account)
    plan_slugs = [p.tier_slug for p in plans]
    # The slug we show as "your current plan". A bundled plan wins the
    # label even if a standalone row also exists.
    current_plan_slug = ''
    for slug in plan_slugs:
        if slug in FULL_PLAN_SLUGS:
            current_plan_slug = slug
            break
    else:
        current_plan_slug = next(
            (s for s in plan_slugs if s != REVIEW_SYSTEM_SLUG),
            next(iter(plan_slugs), ''),
        )

    source, plan = '', None

    # 1 — a Full Plan row.
    for p in plans:
        if p.tier_slug in FULL_PLAN_SLUGS:
            source, plan = 'full_plan', p
            break

    # 2 — a Website carrying a bundled package. Covers contract-signed
    # clients whose plan row is keyed differently by plan_billing.
    if not source:
        try:
            if account.websites.filter(
                    package__in=REVIEWS_BUNDLED_PACKAGES).exists():
                source = 'full_plan'
        except Exception:
            pass

    # 3 — an operator comp.
    if not source and _comp_slug(account) in FULL_PLAN_SLUGS:
        source = 'comp'

    # 4 — a standalone purchase.
    if not source:
        for p in plans:
            if p.tier_slug == REVIEW_SYSTEM_SLUG:
                source, plan = 'standalone', p
                break

    has_access = bool(source)
    program = program_for(account, website=website)
    onboarding = _onboarding_row(account)

    is_hosting_only = (not has_access
                       and current_plan_slug == HOSTING_SECURITY_SLUG)

    # A legacy maintenance-* client being offered a "Full Plan upgrade"
    # is a conversation, not a button — their plan predates the HVAC
    # lineup and the move needs a human.
    can_upgrade = (not has_access
                   and not current_plan_slug.startswith('maintenance-'))

    return ReviewEntitlement(
        has_access=has_access,
        source=source,
        plan=plan,
        program=program,
        setup_status=(program.setup_status if program else ''),
        paused=bool(program and program.sending_paused),
        onboarding=onboarding,
        onboarding_complete=bool(
            onboarding and onboarding.completed_at is not None),
        can_buy_standalone=not has_access,
        can_upgrade_full_plan=can_upgrade,
        current_plan_slug=current_plan_slug,
        is_hosting_only=is_hosting_only,
    )


def has_review_system(account):
    """Cheap boolean for the sidebar. Still one query via the dataclass,
    but expresses intent at the call site."""
    return review_entitlement(account).has_access


def _onboarding_row(account):
    """The account's ``review_system`` Onboarding row, or None."""
    user = getattr(account, 'user', None)
    if user is None:
        return None
    try:
        from onboarding.models import Onboarding
        return (Onboarding.objects
                .filter(user=user, product_type='review_system')
                .order_by('-started_at')
                .first())
    except Exception:
        return None


def program_for(account, *, website=None, create=False):
    """The account's ReviewProgram, optionally creating it.

    Resolves a website-specific row first, then the account-level row, so
    a single-site client and a two-brand client both get the right one.
    """
    if account is None:
        return None
    from reviews.models import ReviewProgram

    qs = ReviewProgram.objects.filter(account=account)
    program = None
    if website is not None:
        program = qs.filter(website=website).first()
    if program is None:
        program = qs.filter(website__isnull=True).first()
    if program is None:
        program = qs.first()
    if program is None and create:
        program = ReviewProgram.objects.create(account=account)
    return program


def nav_state(account):
    """Sidebar state: ``{'has', 'setup_status', 'paused', 'badge'}``.

    The badge is the whole point — a client who owes us their A2P details
    must see that on every page, not only if they happen to open the
    Review System tab.
    """
    blank = {'has': False, 'setup_status': '', 'paused': False, 'badge': ''}
    if account is None:
        return blank
    try:
        ent = review_entitlement(account)
    except Exception:
        return blank

    badge = ''
    if ent.has_access:
        if ent.needs_onboarding:
            badge = '!'
        elif ent.setup_status == 'awaiting_a2p':
            badge = 'Setup'
        elif ent.paused:
            badge = 'Paused'
    return {
        'has': ent.has_access,
        'setup_status': ent.setup_status,
        'paused': ent.paused,
        'badge': badge,
    }
