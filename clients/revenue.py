"""
Revenue maths for the Business Intelligence dashboard.

`get_current_mrr()`  — live calculation from `ClientProfile` +
                      maintenance plan prices.
`get_mrr_trend()`    — read-through over the `RevenueSnapshot` table.
`get_revenue_forecast()` — naive flat projection from current MRR.
`take_revenue_snapshot()` — Celery beat target; persists this month's
                      snapshot and back-fills new / churned by diffing
                      against the previous one.

Pricing source order:
  1. `billing.ServiceTier` rows (the source of truth per CLAUDE.md
     — admins edit prices in the DB, never in code).
  2. Hardcoded `_FALLBACK_PRICES` (Phase 7 spec defaults), used only
     when a tier isn't seeded yet on a fresh install.
"""

from datetime import date

from dateutil.relativedelta import relativedelta


# Spec-defined maintenance package codes (match
# ClientProfile.PACKAGE_CHOICES).
_FALLBACK_PRICES = {
    'maintenance_essentials': 299,
    'maintenance_growth': 599,
    'maintenance_dominant': 1199,
}


def _price_for_package(package_code):
    """
    Look up the monthly price for a maintenance package.
    Tries `billing.ServiceTier` first; falls back to `_FALLBACK_PRICES`.
    Returns a float (0 for unknown packages).
    """
    if not package_code:
        return 0
    try:
        from billing.pricing_models import ServiceTier
        tier = ServiceTier.objects.filter(
            name__iexact=package_code).first()
        if tier and tier.price:
            return float(tier.price)
    except Exception:
        # Pricing app may not be migrated on the test DB; fall through.
        pass
    return float(_FALLBACK_PRICES.get(package_code, 0))


def get_current_mrr():
    """
    Live MRR snapshot from active maintenance clients. Returns
    `{mrr_total, active_maintenance_clients, breakdown}` where
    breakdown is the per-client list the dashboard table renders.
    """
    from clients.models import ClientProfile

    active = (ClientProfile.objects
              .filter(maintenance_active=True, status='active',
                      is_tester=False)
              .order_by('firm_name'))

    breakdown = []
    total = 0.0
    for c in active:
        price = _price_for_package(c.package)
        total += price
        breakdown.append({
            'client': c.firm_name,
            'plan': c.get_package_display() or c.package or '—',
            'mrr': price,
        })

    breakdown.sort(key=lambda r: r['mrr'], reverse=True)
    return {
        'mrr_total': total,
        'active_maintenance_clients': active.count(),
        'breakdown': breakdown,
    }


def get_mrr_trend(months=6):
    """
    Most recent `months` snapshots, oldest → newest, ready for the
    bar chart. Each item: `{month, mrr, new, churned}`.
    """
    from clients.models import RevenueSnapshot
    snapshots = list(
        RevenueSnapshot.objects.order_by('-snapshot_month')[:months])
    return [
        {
            'month': s.snapshot_month.strftime('%b %Y'),
            'mrr': float(s.mrr_total),
            'new': float(s.mrr_new),
            'churned': float(s.mrr_churned),
        }
        for s in reversed(snapshots)
    ]


def get_revenue_forecast(months=3):
    """
    Flat projection: assume current MRR continues for the next
    `months` months with no churn or growth. Caller is expected to
    label this prominently — it's intentionally naive.
    """
    current = get_current_mrr()
    mrr = current['mrr_total']
    today = date.today()
    out = []
    for i in range(1, months + 1):
        target = today.replace(day=1) + relativedelta(months=i)
        out.append({
            'month': target.strftime('%b %Y'),
            'projected_mrr': mrr,
            'projected_arr': mrr * 12,
        })
    return out


def take_revenue_snapshot():
    """
    Create / update this month's RevenueSnapshot. Idempotent — the
    1st-of-month Celery beat can run it once and re-running by hand
    just overwrites with fresh numbers. Returns the snapshot row.

    `mrr_new` and `mrr_churned` are derived by comparing against last
    month's snapshot (if any); a positive delta becomes `mrr_new`, a
    negative one becomes `mrr_churned`. Net change is signed.
    """
    from clients.models import ClientProfile, RevenueSnapshot

    snapshot_month = date.today().replace(day=1)
    prev = RevenueSnapshot.objects.filter(
        snapshot_month=snapshot_month - relativedelta(months=1)).first()

    current = get_current_mrr()
    mrr_total = current['mrr_total']

    mrr_new = mrr_churned = 0.0
    if prev:
        delta = mrr_total - float(prev.mrr_total)
        if delta > 0:
            mrr_new = delta
        elif delta < 0:
            mrr_churned = -delta

    in_progress_stages = (
        'intake', 'structure', 'design', 'content',
        'review', 'revisions', 'pre_launch',
    )
    active_project_clients = ClientProfile.objects.filter(
        projects__stage__in=in_progress_stages,
        is_tester=False,
    ).distinct().count()

    snapshot, _ = RevenueSnapshot.objects.update_or_create(
        snapshot_month=snapshot_month,
        defaults={
            'mrr_total': mrr_total,
            'mrr_new': mrr_new,
            'mrr_churned': mrr_churned,
            'mrr_net_change': mrr_new - mrr_churned,
            'active_maintenance_clients': (
                current['active_maintenance_clients']),
            'active_project_clients': active_project_clients,
        },
    )
    return snapshot
