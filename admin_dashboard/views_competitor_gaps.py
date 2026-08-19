"""
Competitor content gap tracker.

Split out of admin_dashboard/views.py; re-exported from
`admin_dashboard.views` so urls.py keeps working unchanged.
"""

import json
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
from .utils import _is_uuid
from clients.display import owner_label

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 5 — Competitor Content Gap Tracker
# ────────────────────────────────────────────────────────────────────────────

_COMPETITOR_LIMIT = 3


def _competitors_fragment(request, client):
    """Render the competitors box that HTMX swaps in/out."""
    from clients.models import ClientCompetitor
    competitors = list(
        client.competitors_new.all()[:_COMPETITOR_LIMIT])
    return render(
        request, 'admin_dashboard/_competitors_box.html',
        {
            'client': client,
            'competitors': competitors,
            'can_add': len(competitors) < _COMPETITOR_LIMIT,
            'competitor_limit': _COMPETITOR_LIMIT,
        },
    )


@admin_required
def competitor_add(request, client_id):
    """
    Add a competitor for `client_id`. POST adds + returns the
    refreshed competitors box (HTMX); GET returns the inline form
    fragment.
    """
    from clients.account_models import Website
    from clients.models import ClientCompetitor

    # Competitors are tracked per SITE: the gap analysis compares one
    # site's pages against theirs, so a firm's two brands have different
    # competitors and would otherwise share one list.
    client = get_object_or_404(
        Website.objects.select_related('account'), id=client_id)

    if request.method == 'POST':
        existing = client.competitors_new.count()
        if existing >= _COMPETITOR_LIMIT:
            return HttpResponseBadRequest(
                f'Max {_COMPETITOR_LIMIT} competitors per client.')
        name = (request.POST.get('name') or '').strip()[:200]
        domain = (request.POST.get('domain') or '').strip()[:200]
        notes = (request.POST.get('notes') or '').strip()[:300]
        if not name or not domain:
            return HttpResponseBadRequest('name + domain required.')
        if not domain.startswith(('http://', 'https://')):
            domain = f'https://{domain}'
        if client.competitors.filter(domain=domain).exists():
            return HttpResponseBadRequest(
                'That domain is already tracked for this client.')
        ClientCompetitor.objects.create(
            client=client, name=name, domain=domain, notes=notes,
            sort_order=existing,
        )
        return _competitors_fragment(request, client)

    return render(
        request, 'admin_dashboard/_competitor_form.html',
        {'client': client, 'competitor': None,
         'form_url': reverse(
             'admin_dashboard:competitor_add', args=[client.id])},
    )


@admin_required
def competitor_edit(request, client_id, comp_id):
    """Inline edit; same HTMX contract as add."""
    from clients.account_models import Website
    from clients.models import ClientCompetitor

    client = get_object_or_404(
        Website.objects.select_related('account'), id=client_id)
    comp = get_object_or_404(
        ClientCompetitor, id=comp_id, website_new=client)

    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()[:200]
        domain = (request.POST.get('domain') or '').strip()[:200]
        notes = (request.POST.get('notes') or '').strip()[:300]
        if not name or not domain:
            return HttpResponseBadRequest('name + domain required.')
        if not domain.startswith(('http://', 'https://')):
            domain = f'https://{domain}'
        # Allow same domain on self; reject if a *different* row
        # already uses it.
        if client.competitors.filter(domain=domain).exclude(
                id=comp.id).exists():
            return HttpResponseBadRequest(
                'Another competitor already uses that domain.')
        comp.name = name
        comp.domain = domain
        comp.notes = notes
        comp.save(update_fields=['name', 'domain', 'notes',
                                 'updated_at'])
        return _competitors_fragment(request, client)

    return render(
        request, 'admin_dashboard/_competitor_form.html',
        {'client': client, 'competitor': comp,
         'form_url': reverse(
             'admin_dashboard:competitor_edit',
             args=[client.id, comp.id])},
    )


@admin_required
@require_POST
def competitor_delete(request, client_id, comp_id):
    """Drop a competitor; return the refreshed box."""
    from clients.account_models import Website
    from clients.models import ClientCompetitor

    client = get_object_or_404(
        Website.objects.select_related('account'), id=client_id)
    comp = get_object_or_404(
        ClientCompetitor, id=comp_id, website_new=client)
    comp.delete()
    return _competitors_fragment(request, client)


# ── Competitor gap reports list + detail ───────────────────────────────────

@admin_required
def competitor_gaps_list(request):
    """All gap reports + 4 summary cards."""
    from clients.account_models import Website
    from clients.models import CompetitorGapReport
    from django.db.models import Sum

    qs = (CompetitorGapReport.objects
          .select_related('website_new', 'website_new__account')
          .order_by('-report_month', 'client__firm_name'))

    client_filter = (request.GET.get('client') or '').strip()
    status_filter = (request.GET.get('status') or '').strip()
    month_filter = (request.GET.get('month') or '').strip()

    if client_filter:
        try:
            qs = qs.filter(client_id=client_filter)
        except (ValueError, TypeError):
            pass
    if status_filter and status_filter != 'all':
        qs = qs.filter(status=status_filter)
    if month_filter:
        try:
            y, m = month_filter.split('-')
            qs = qs.filter(report_month__year=int(y),
                           report_month__month=int(m))
        except (ValueError, AttributeError):
            pass

    base = CompetitorGapReport.objects.all()
    summary = {
        'total_reports': base.count(),
        'high_priority': (base.aggregate(
            s=Sum('high_priority_gaps'))['s'] or 0),
        'with_competitors': (
            Website.objects
            .filter(competitors_new__isnull=False,
                    account__is_tester=False, status='active')
            .distinct().count()),
        'without_competitors': (
            Website.objects
            .filter(competitors_new__isnull=True,
                    account__is_tester=False, status='active')
            .distinct().count()),
    }

    clients = (Website.objects
               .filter(competitor_gap_reports_new__isnull=False)
               .select_related('account')
               .distinct().order_by('account__name', 'name'))

    return render(
        request, 'admin_dashboard/competitor_gaps_list.html',
        _admin_context(
            'competitor_gaps',
            reports=qs,
            summary=summary,
            clients=clients,
            filter_client=client_filter,
            filter_status=status_filter,
            filter_month=month_filter,
            status_choices=CompetitorGapReport.STATUS_CHOICES,
        ),
    )


@admin_required
def competitor_gap_detail(request, report_id):
    """Single-report detail page."""
    from clients.models import CompetitorGapReport
    report = get_object_or_404(CompetitorGapReport, id=report_id)

    # Index gaps so the create-suggestion button has a stable handle.
    gaps_indexed = list(enumerate(report.gaps or []))

    # Sort high → medium → low → unknown.
    _PRIORITY = {'high': 0, 'medium': 1, 'low': 2}
    gaps_indexed.sort(
        key=lambda pair: _PRIORITY.get(
            (pair[1].get('priority') or '').lower(), 3))

    return render(
        request, 'admin_dashboard/competitor_gap_detail.html',
        _admin_context(
            'competitor_gaps',
            report=report,
            gaps_indexed=gaps_indexed,
        ),
    )


@admin_required
@require_POST
def competitor_gap_run_now(request, client_id):
    """"Run Analysis Now" — fires the Celery task async."""
    from clients.account_models import Website
    from clients.tasks import run_competitor_gap_analysis

    client = get_object_or_404(
        Website.objects.select_related('account'), id=client_id)
    run_competitor_gap_analysis.apply_async(args=[str(client.id)])

    if request.headers.get('HX-Request') == 'true':
        return HttpResponse(
            '<div class="banner banner--info">'
            'Analysis queued — usually under a minute. '
            'Refresh to see the report.'
            '</div>')
    return redirect('admin_dashboard:competitor_gaps_list')


@admin_required
@require_POST
def gap_create_suggestion(request, report_id, gap_index):
    """
    Convert a single gap → IntelligenceSuggestion(pending_review).
    Idempotent on (report, gap_index) via a marker stamped into the
    gap dict so the operator can't accidentally create two.
    """
    from clients.models import (
        CompetitorGapReport, IntelligenceSuggestion,
    )

    report = get_object_or_404(CompetitorGapReport, id=report_id)
    gaps = list(report.gaps or [])
    if gap_index < 0 or gap_index >= len(gaps):
        return HttpResponseBadRequest('gap_index out of range.')

    gap = gaps[gap_index]
    if gap.get('suggestion_id'):
        # Already converted — give them a link to the existing row.
        return redirect(
            'admin_dashboard:intelligence_suggestion_detail',
            suggestion_id=gap['suggestion_id'])

    competitors_str = ', '.join(
        gap.get('competitors_with_this') or [])
    expected = (
        f'Targeting this gap could help '
        f'{owner_label(report)} compete with '
        f'{competitors_str}'
        f' who already cover this topic.'
        if competitors_str
        else (
            f'Targeting this gap could help '
            f'{owner_label(report)} attract searches that '
            f'currently land on competitor sites.'
        )
    )

    from clients.website_helpers import primary_website
    suggestion = IntelligenceSuggestion.objects.create(
        client=report.client,
        website_new=report.website_new or primary_website(report.client),
        suggestion_type='competitor',
        title=(gap.get('suggested_page_title')
               or gap.get('title') or 'Competitor gap')[:300],
        description=gap.get('description', '') or '',
        expected_impact=expected,
        implementation_notes=(
            gap.get('suggested_action', '') or ''),
        one_time_fee=500,
        is_in_maintenance_scope=False,
        data_sources=['competitor_gaps'],
        ai_reasoning=json.dumps(gap, default=str),
        status='pending_review',
    )

    # Stamp the gap so we don't double-create.
    gaps[gap_index] = {**gap,
                       'suggestion_id': str(suggestion.id)}
    report.gaps = gaps
    report.save(update_fields=['gaps', 'updated_at'])

    return redirect(
        'admin_dashboard:intelligence_suggestion_detail',
        suggestion_id=suggestion.id)


