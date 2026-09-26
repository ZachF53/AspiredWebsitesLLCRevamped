"""
Who gets the monthly security report.

The public pricing page promises the "Monthly security report" to every
plan — the Full Plan (``hvac-full-plan`` / ``hvac-plan-paid-in-full``),
Hosting + Security (``hvac-hosting-security``) and the legacy
maintenance tiers. This module is the ONE place that decides whether a
website is on such a plan; the monthly send, the pre-summary scan sweep
and the daily scan schedule all read it.
"""

from django.db.models import Q

# Website.package values that are a paid, recurring plan including the
# security report. Website.package stores tier slugs with dashes turned
# into underscores (billing/account_provisioning.py), so both spellings
# are matched.
_PLAN_SLUGS = (
    'hvac-full-plan',
    'hvac-plan-paid-in-full',
    'hvac-hosting-security',
    'maintenance-essentials',
    'maintenance-growth',
    'maintenance-dominant',
)
PAID_PLAN_PACKAGES = frozenset(
    {s for s in _PLAN_SLUGS} | {s.replace('-', '_') for s in _PLAN_SLUGS})

# Operator-comped maintenance (Account.comp_maintenance_package) counts:
# a comp is a commercial decision to give the plan free, service included.
COMP_MAINTENANCE_PACKAGES = frozenset({
    'maintenance_essentials', 'maintenance_growth', 'maintenance_dominant',
})


def security_report_eligibility_q():
    """
    Q for Website rows on an active paid plan that includes the monthly
    security report. A site qualifies when the site AND its account are
    ``status='active'`` and at least one of these holds:

    1. ``Website.maintenance_active`` is True — set by checkout /
       webhooks when a maintenance-category subscription (legacy tiers,
       Full Plan, Full Plan paid-in-full) starts, cleared on cancel.
    2. A ``MaintenancePlan`` with ``status='active'`` covers the site —
       either tied to it directly, or account-level (``website`` NULL),
       which covers every site on the account.
    3. ``Website.package`` is one of ``PAID_PLAN_PACKAGES`` AND the site
       still carries a live Stripe subscription id (maintenance, hosting
       or build-installment). The id check matters: webhooks clear the
       id on cancellation but leave ``package`` behind, so package alone
       would keep emailing churned clients. This is how Hosting +
       Security ($45/mo, a hosting-category subscription that does not
       flip maintenance_active) qualifies.
    4. The account has a comped maintenance tier.

    Callers must ``.distinct()`` — clause 2 joins a to-many relation.
    """
    live_sub = (~Q(stripe_maintenance_subscription_id='')
                | ~Q(stripe_hosting_subscription_id='')
                | ~Q(stripe_build_installment_subscription_id=''))
    plan_q = (
        Q(maintenance_active=True)
        | Q(maintenance_plans__status='active')
        | Q(account__maintenance_plans__status='active',
            account__maintenance_plans__website__isnull=True)
        | (Q(package__in=PAID_PLAN_PACKAGES) & live_sub)
        | Q(account__comp_maintenance_package__in=COMP_MAINTENANCE_PACKAGES)
    )
    return Q(status='active', account__status='active') & plan_q


def security_report_websites():
    """Queryset of every Website that should receive the monthly
    security report (see ``security_report_eligibility_q``)."""
    from clients.account_models import Website
    return (Website.objects
            .filter(security_report_eligibility_q())
            .select_related('account', 'account__user')
            .distinct())


def is_security_report_eligible(website):
    """Single-site form of ``security_report_websites``."""
    if website is None or website.pk is None:
        return False
    return security_report_websites().filter(pk=website.pk).exists()
