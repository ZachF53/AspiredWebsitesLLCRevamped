"""
Business intelligence dashboard and daily focus.

Split out of admin_dashboard/views.py; re-exported from
`admin_dashboard.views` so urls.py keeps working unchanged.
"""

import datetime
import logging

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)
from .decorators import admin_required

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 1 — Business Intelligence dashboard + Daily Focus
# ────────────────────────────────────────────────────────────────────────────

# Sort key for the client-health table: critical first, then at-risk,
# then healthy. Anything outside the choice set drops to the bottom.
_HEALTH_SORT_ORDER = {'critical': 0, 'at_risk': 1, 'healthy': 2}


def get_daily_focus():
    """
    Top-five-most-urgent triage list used by both the Intelligence
    dashboard and the home page Today's Focus widget. Sorted by
    priority (lower number = more urgent).
    """
    from clients.models import ClientHealthScore, ClientProfile

    items = []
    today = timezone.now().date()

    # 1. Critical-band clients flagged today
    critical_scores = (
        ClientHealthScore.objects
        .filter(health_status='critical',
                churn_risk=True,
                calculated_at__date=today)
        .select_related('client')[:3]
    )
    seen_clients = set()
    for hs in critical_scores:
        if hs.client_id in seen_clients:
            continue
        seen_clients.add(hs.client_id)
        items.append({
            'priority': 1,
            'icon': '🔴',
            'title': f'Critical health risk: {hs.client.firm_name}',
            'description': (
                f'Health score: {hs.score}/100 — '
                f'immediate attention needed'),
            'url': reverse('admin_dashboard:client_detail',
                           args=[hs.client.id]),
            'action': 'View Client',
        })

    # 2. Scans with unsent critical findings — and that the admin
    #    hasn't opened yet (been_reviewed=False). The detail-page
    #    view flips the flag on first open so repeat visits don't
    #    nag here forever.
    from reporting.models import VulnerabilityScan
    critical_scans = (
        VulnerabilityScan.objects
        .filter(status='complete', critical_count__gt=0,
                sent_to_client=False, been_reviewed=False)
        .select_related('client')
        .order_by('-completed_at')[:3]
    )
    for scan in critical_scans:
        items.append({
            'priority': 2,
            'icon': '🔴',
            'title': (f'Critical scan findings: '
                      f'{scan.client.firm_name}'),
            'description': (
                f'{scan.critical_count} critical finding'
                f'{"" if scan.critical_count == 1 else "s"} '
                f'not yet sent to client'),
            'url': reverse('admin_dashboard:scan_detail',
                           args=[scan.id]),
            'action': 'Review Scan',
        })

    # 3. Active non-tester clients in 'live' stage with no website
    #    set — uptime monitoring + scans can't run without one.
    # Post-2026-05-25: stage + website on ClientProfile directly.
    no_url = (ClientProfile.objects
              .filter(status='active', is_tester=False,
                      stage='live', website='')
              [:3])
    for client in no_url:
        items.append({
            'priority': 3,
            'icon': '⚠',
            'title': f'No live URL: {client.firm_name}',
            'description': (
                'Uptime monitoring and scans cannot run without a '
                'live URL'),
            'url': reverse('admin_dashboard:client_edit',
                           args=[client.id]),
            'action': 'Add URL',
        })

    items.sort(key=lambda x: x['priority'])
    return items[:5]


@admin_required
def intelligence_dashboard(request):
    """
    Business Intelligence — revenue stats + client health table +
    Daily Focus. Admin-only; no client-facing components in this
    phase.
    """
    from clients.health import get_latest_health_score
    from clients.models import (
        ClientHealthScore, ClientProfile, RevenueSnapshot,
    )
    from clients.revenue import (
        get_current_mrr, get_mrr_trend, get_revenue_forecast,
    )

    # ── Revenue ────────────────────────────────────────────────
    mrr = get_current_mrr()
    mrr_trend = get_mrr_trend(months=6)
    forecast = get_revenue_forecast(months=3)
    arr = mrr['mrr_total'] * 12

    # New + churned come from the most recent snapshot — live calc
    # only knows total, not the deltas. Fall back to 0 when no
    # snapshots have been taken yet (fresh install).
    latest_snapshot = RevenueSnapshot.objects.first()
    snap_new = (float(latest_snapshot.mrr_new)
                if latest_snapshot else 0)
    snap_churned = (float(latest_snapshot.mrr_churned)
                    if latest_snapshot else 0)

    # MRR trend chart bar heights — relative to the max so the
    # tallest bar is always 100%. One pass over the data.
    max_mrr = max(
        (row['mrr'] for row in mrr_trend), default=0) or 1
    for row in mrr_trend:
        row['height_pct'] = round(row['mrr'] / max_mrr * 100)

    # ── Client health ─────────────────────────────────────────
    active_clients = (ClientProfile.objects
                      .filter(status='active', is_tester=False)
                      .order_by('firm_name'))
    rows = []
    for client in active_clients:
        rows.append({
            'client': client,
            'health': get_latest_health_score(client),
        })
    rows.sort(key=lambda r: _HEALTH_SORT_ORDER.get(
        r['health'].health_status, 3))

    critical = sum(1 for r in rows
                   if r['health'].health_status == 'critical')
    at_risk = sum(1 for r in rows
                  if r['health'].health_status == 'at_risk')
    healthy = sum(1 for r in rows
                  if r['health'].health_status == 'healthy')

    # ── Intelligence Engine rollups ────────────────────────────
    from clients.models import (
        CompetitorGapReport, IntelligenceSuggestion,
    )
    from django.db.models import Sum as _Sum
    intel_pending = IntelligenceSuggestion.objects.filter(
        status='pending_review').count()
    intel_sent = IntelligenceSuggestion.objects.filter(
        status='sent_to_client').count()
    intel_approved = IntelligenceSuggestion.objects.filter(
        status__in=['client_approved', 'in_scope',
                    'out_of_scope_offered', 'implemented']).count()
    intel_revenue = (IntelligenceSuggestion.objects.filter(
        status__in=['client_approved', 'out_of_scope_offered',
                    'implemented'],
        is_in_maintenance_scope=False,
    ).aggregate(s=_Sum('one_time_fee'))['s'] or 0)

    # ── Competitor gap rollups (Phase 7 Part 5) ────────────────
    gap_reports_run = CompetitorGapReport.objects.filter(
        status='complete').count()
    gap_high_priority = (CompetitorGapReport.objects
        .filter(status='complete')
        .aggregate(s=_Sum('high_priority_gaps'))['s'] or 0)
    gap_suggestions_created = IntelligenceSuggestion.objects.filter(
        suggestion_type='competitor').count()

    return render(request, 'admin_dashboard/intelligence.html',
                  _admin_context(
                      'intelligence',
                      mrr_total=mrr['mrr_total'],
                      arr=arr,
                      mrr_new=snap_new,
                      mrr_churned=snap_churned,
                      active_maintenance_clients=(
                          mrr['active_maintenance_clients']),
                      mrr_breakdown=mrr['breakdown'],
                      mrr_trend=mrr_trend,
                      forecast=forecast,
                      rows=rows,
                      critical_count=critical,
                      at_risk_count=at_risk,
                      healthy_count=healthy,
                      latest_snapshot_month=(
                          latest_snapshot.snapshot_month
                          if latest_snapshot else None),
                      daily_focus=get_daily_focus(),
                      intel_pending=intel_pending,
                      intel_sent=intel_sent,
                      intel_approved=intel_approved,
                      intel_revenue=intel_revenue,
                      gap_reports_run=gap_reports_run,
                      gap_high_priority=gap_high_priority,
                      gap_suggestions_created=(
                          gap_suggestions_created),
                  ))


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_referrals.py
# ──────────────────────────────────────────────────────────────────────────
from .views_referrals import (  # noqa: E402,F401
    referral_mark_conversion,
    referral_toggle_active,
    referrals_list,
)


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_proposals.py
# ──────────────────────────────────────────────────────────────────────────
from .views_proposals import (  # noqa: E402,F401
    _active_proposals_count,
    proposal_detail,
    proposal_generate,
    proposal_lead_autofill,
    proposal_new,
    proposal_send,
    proposal_set_status,
    proposals_list,
)


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_case_studies.py
# ──────────────────────────────────────────────────────────────────────────
from .views_case_studies import (  # noqa: E402,F401
    _client_location,
    case_studies_list,
    case_study_ai_draft,
    case_study_edit,
    case_study_new,
    case_study_toggle_publish,
)


