"""
The two choices an operator makes when sending an agreement (Sept 2026).

    build: pay_in_full | installment | none
    plan:  full_plan   | hosting     | none

Each choice resolves to a billing ServiceTier by SLUG — prices are never
written here; they are read from the tier (billing/pricing_models.py) and
snapshotted onto ContractService rows when the contract is created.

The Full Plan tier depends on how the build is paid:

  * installment + Full Plan  -> hvac-full-plan ($250/mo: the $105 build
    installment + the $145 plan) for 24 months, then hvac-plan-paid-in-full
    ($145/mo) ongoing. ONE Stripe subscription, driven by a two-phase
    SubscriptionSchedule — the $105 is never billed separately.
  * pay_in_full / no build + Full Plan -> hvac-plan-paid-in-full ($145/mo).

There is no deposit option. Legacy 50/50 contracts keep working through
their own code paths (Contract.payment_option is blank on those rows).
"""

from decimal import Decimal

BUILD_FULL_SLUG = 'hvac-build-full'
BUILD_INSTALLMENT_SLUG = 'hvac-build-installment'
FULL_PLAN_SLUG = 'hvac-full-plan'
PLAN_PAID_IN_FULL_SLUG = 'hvac-plan-paid-in-full'
HOSTING_SECURITY_SLUG = 'hvac-hosting-security'
# The standalone $97/mo Review System. category='addon', so it is NOT a
# plan in the maintenance sense — it never appears in the portal plan
# chooser and has its own signup view.
REVIEW_SYSTEM_SLUG = 'hvac-review-system'

# The standalone AI Voice Receptionist. Also category='addon', for the
# same plan-chooser reason as the Review System above — but with one
# extra rule that the Review System does not have:
#
#   IT IS NEVER BUNDLED INTO ANY PLAN, AT ANY PRICE.
#
# The Review System is free inside the Full Plan because it costs us
# almost nothing to run (a few cents of SMS per job). The receptionist
# bills real per-minute vendor cost on every call, and it is the only
# product sellable to the no-website segment, where the lead has a phone
# and nothing else. Folding it into the $145 plan would both erode that
# plan's margin per call and cap the phone channel's revenue.
#
# Concretely, that means this slug is deliberately absent from:
#   * FULL_PLAN_SLUGS        — it is not a plan
#   * SLUG_TO_PACKAGE        — it never becomes a Website.package code
#   * every *_BUNDLED_* tuple — nothing includes it for free
# clients/tests_never_bundled.py asserts all three, so a future change
# cannot quietly give it away.
VOICE_RECEPTIONIST_SLUG = 'hvac-voice-receptionist'

# Tiers that include automated review generation at no extra cost. Both
# Full Plan variants bundle it, which is why review entitlement can never
# be answered by looking for a single plan row.
FULL_PLAN_SLUGS = (FULL_PLAN_SLUG, PLAN_PAID_IN_FULL_SLUG)
REVIEWS_BUNDLED_PACKAGES = ('hvac_full_plan', 'hvac_plan_paid_in_full')

INSTALLMENT_COUNT = 24
GUARANTEE_DAYS = 30

BUILD_OPTIONS = [
    ('pay_in_full', 'Pay in full at signing'),
    ('installment', '24-month installment'),
    ('none', 'No build'),
]
PLAN_OPTIONS = [
    ('full_plan', 'Full Plan'),
    ('hosting', 'Hosting + Security only'),
    ('none', 'No plan'),
]

# ServiceTier slug -> Website/Contract package code.
SLUG_TO_PACKAGE = {
    BUILD_FULL_SLUG: 'hvac_build',
    BUILD_INSTALLMENT_SLUG: 'hvac_build',
    FULL_PLAN_SLUG: 'hvac_full_plan',
    PLAN_PAID_IN_FULL_SLUG: 'hvac_plan_paid_in_full',
    HOSTING_SECURITY_SLUG: 'hvac_hosting_security',
}


class ContractOptionError(ValueError):
    """An invalid build/plan combination, or a tier that isn't set up.
    The message is operator-facing."""


def build_tier_slug(build_option):
    return {'pay_in_full': BUILD_FULL_SLUG,
            'installment': BUILD_INSTALLMENT_SLUG}.get(build_option)


def plan_tier_slug(build_option, plan_option):
    if plan_option == 'hosting':
        return HOSTING_SECURITY_SLUG
    if plan_option == 'full_plan':
        return (FULL_PLAN_SLUG if build_option == 'installment'
                else PLAN_PAID_IN_FULL_SLUG)
    return None


def _tier(slug):
    from billing.pricing_models import ServiceTier

    tier = ServiceTier.objects.filter(slug=slug, is_active=True).first()
    if tier is None:
        raise ContractOptionError(
            f'Pricing tier "{slug}" is missing or inactive — run '
            'seed_pricing before sending a contract.')
    return tier


def resolve_contract_services(build_option, plan_option, *,
                              custom_build_price=None, platform='custom',
                              build_name=None):
    """Validate the operator's two choices and return the service dicts
    ``generate_combined_contract_text`` and the ContractService writer take.

    Each dict: ``{'service_type', 'tier', 'price', 'name', ...}``. A custom
    build price replaces the tier price on a pay-in-full build only — an
    installment build is always the tier's 24 x monthly price.
    """
    if build_option not in dict(BUILD_OPTIONS):
        raise ContractOptionError('Choose how the build is paid.')
    if plan_option not in dict(PLAN_OPTIONS):
        raise ContractOptionError('Choose a plan (or no plan).')
    if build_option == 'none' and plan_option == 'none':
        raise ContractOptionError(
            'Select a build, a plan, or both — the contract would be empty.')
    if custom_build_price is not None and build_option == 'installment':
        raise ContractOptionError(
            'A custom build price only applies to a pay-in-full build — '
            'installments are always 24 monthly payments at the tier price.')

    services = []
    slug = build_tier_slug(build_option)
    if slug:
        tier = _tier(slug)
        if build_option == 'installment':
            price = Decimal(tier.price)  # the monthly installment
        else:
            price = (Decimal(custom_build_price)
                     if custom_build_price is not None
                     else Decimal(tier.price))
        services.append({
            'service_type': 'build', 'tier': tier, 'price': price,
            'name': build_name or 'Website Build',
            'platform': platform, 'weeks': tier.timeline_weeks or 4,
            'payment_option': build_option,
        })
    slug = plan_tier_slug(build_option, plan_option)
    if slug:
        tier = _tier(slug)
        services.append({
            'service_type': ('hosting' if plan_option == 'hosting'
                             else 'maintenance'),
            'tier': tier, 'price': Decimal(tier.price), 'name': tier.name,
        })
    return services


def payment_option_for(build_option):
    return build_option if build_option in ('pay_in_full',
                                            'installment') else 'none'


def installment_total(monthly):
    return (Decimal(monthly) * INSTALLMENT_COUNT).quantize(Decimal('0.01'))
