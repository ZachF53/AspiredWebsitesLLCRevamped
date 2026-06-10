"""
Phase 5a-pivot — GBP admin views.

Five views:
    dashboard(request)                 cross-client triage — reviews
                                       needing attention, NAP drift,
                                       unconnected clients.
    client_gbp(request, client_id)     per-client deep dive — NAP card,
                                       latest reviews, latest perf
                                       snapshot.
    locations_picker(request, client_id)
                                       bind a GBP location to a
                                       client; lists every location
                                       the operator has access to.
    reviews_list(request, client_id)   review history for one client
                                       with star + reply state.
    nap_history(request, client_id)    GBPSyncCheck history for one
                                       client.

All admin-gated. Per-client views guard on
ClientProfile.has_gbp_features() — Essentials clients get a polite
"upgrade to Growth+" message instead of the feature pages.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from admin_dashboard.decorators import admin_required
from clients.models import ClientProfile

from reporting.google_gbp import list_locations
from reporting.models import (
    GBPSyncCheck,
    GbpOperatorToken,
    GbpPerformanceSnapshot,
    GbpReview,
)

logger = logging.getLogger(__name__)


def _operator_token():
    """Whoever connected first (typically the agency owner). Returns
    None if nobody has connected yet."""
    return GbpOperatorToken.objects.order_by('created_at').first()


@admin_required
def dashboard(request):
    """Cross-client triage. Three sections:
        - Reviews needing attention (low star or unreplied)
        - NAP drift flagged
        - Clients with maintenance tier ≥ Growth but no bound location
    """
    token = _operator_token()

    # Reviews needing attention — pull top 20 newest across all clients.
    attention = (
        GbpReview.objects
        .filter(needs_attention=True)
        .select_related('client')
        .order_by('-review_created_at')[:20]
    )

    # NAP drift — latest unresolved mismatch per client.
    nap_mismatches = (
        GBPSyncCheck.objects
        .filter(is_mismatch=True, resolved=False)
        .select_related('client')
        .order_by('-checked_at')[:20]
    )

    # Eligible but unconnected clients.
    eligible = ClientProfile.objects.filter(
        package__in=['maintenance_growth', 'maintenance_dominant'],
    )
    unbound = eligible.filter(gbp_location_name='').order_by('firm_name')

    return render(request, 'reporting/gbp/dashboard.html', {
        'active_nav':     'gbp',
        'operator_token': token,
        'connected':      token is not None,
        'attention':      attention,
        'nap_mismatches': nap_mismatches,
        'unbound':        unbound[:20],
        'unbound_total':  unbound.count(),
        'eligible_total': eligible.count(),
    })


def _gbp_gate(view):
    """Decorator — render an "upgrade required" page when the client's
    maintenance tier doesn't include GBP features."""
    from functools import wraps

    @wraps(view)
    def _wrapped(request, client_id, *args, **kwargs):
        profile = get_object_or_404(ClientProfile, id=client_id)
        if not profile.has_gbp_features():
            return render(request, 'reporting/gbp/upgrade_required.html', {
                'active_nav': 'gbp',
                'profile':    profile,
            })
        return view(request, client_id, *args, **kwargs)
    return _wrapped


@admin_required
@_gbp_gate
def client_gbp(request, client_id):
    """Per-client GBP deep dive."""
    profile = get_object_or_404(ClientProfile, id=client_id)
    latest_perf = (
        GbpPerformanceSnapshot.objects
        .filter(client=profile)
        .order_by('-snapshot_month')
        .first()
    )
    recent_reviews = (
        GbpReview.objects
        .filter(client=profile)
        .order_by('-review_created_at')[:10]
    )
    latest_nap = (
        GBPSyncCheck.objects
        .filter(client=profile)
        .order_by('-checked_at')[:5]
    )
    operator_token = _operator_token()

    return render(request, 'reporting/gbp/client_gbp.html', {
        'active_nav':     'gbp',
        'profile':        profile,
        'operator_token': operator_token,
        'connected':      operator_token is not None,
        'latest_perf':    latest_perf,
        'recent_reviews': recent_reviews,
        'latest_nap':     latest_nap,
        'bound':          bool(profile.gbp_location_name),
        'is_premium':     profile.has_gbp_premium_features(),
    })


@admin_required
@_gbp_gate
def locations_picker(request, client_id):
    """Pick which GBP location belongs to this client. Uses the
    operator's token to list every location they manage."""
    profile = get_object_or_404(ClientProfile, id=client_id)
    token = _operator_token()
    if token is None:
        messages.error(
            request,
            'Connect your Google Business Profile account first.')
        return redirect('gbp:connect_page')

    if request.method == 'POST':
        resource_name = (request.POST.get('location_name') or '').strip()
        if not resource_name.startswith('accounts/'):
            messages.error(request, 'Pick a location from the list.')
            return redirect('gbp:locations_picker', client_id=profile.id)
        profile.gbp_location_name = resource_name
        profile.save(update_fields=['gbp_location_name', 'updated_at'])
        messages.success(
            request,
            f'Bound {profile.firm_name} to that GBP location. '
            'Review sync + NAP checks will run from here on.')
        return redirect('gbp:client_gbp', client_id=profile.id)

    try:
        locations = list_locations(token)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'locations_picker: list_locations failed for %s', profile.id)
        messages.error(
            request, f'Could not fetch GBP locations: {exc}')
        locations = []

    return render(request, 'reporting/gbp/locations_picker.html', {
        'active_nav': 'gbp',
        'profile':    profile,
        'locations':  locations,
    })


@admin_required
@_gbp_gate
def reviews_list(request, client_id):
    """All cached reviews for one client. Premium tier can post
    replies inline; non-premium sees read-only."""
    profile = get_object_or_404(ClientProfile, id=client_id)
    reviews = (
        GbpReview.objects
        .filter(client=profile)
        .order_by('-review_created_at')[:200]
    )
    return render(request, 'reporting/gbp/reviews_list.html', {
        'active_nav': 'gbp',
        'profile':    profile,
        'reviews':    reviews,
        'is_premium': profile.has_gbp_premium_features(),
    })


@admin_required
@_gbp_gate
def nap_history(request, client_id):
    """GBPSyncCheck history for one client."""
    profile = get_object_or_404(ClientProfile, id=client_id)
    checks = (
        GBPSyncCheck.objects
        .filter(client=profile)
        .order_by('-checked_at')[:50]
    )
    return render(request, 'reporting/gbp/nap_history.html', {
        'active_nav': 'gbp',
        'profile':    profile,
        'checks':     checks,
    })
