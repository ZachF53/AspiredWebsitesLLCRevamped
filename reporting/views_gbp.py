"""
Phase 5a-pivot — GBP admin views.

Five views:
    dashboard(request)                 cross-site triage — reviews
                                       needing attention, NAP drift,
                                       unconnected sites.
    client_gbp(request, website_id)    per-site deep dive — NAP card,
                                       latest reviews, latest perf
                                       snapshot.
    locations_picker(request, website_id)
                                       bind a GBP location to a site;
                                       lists every location the operator
                                       has access to.
    reviews_list(request, website_id)  review history for one site
                                       with star + reply state.
    nap_history(request, website_id)   GBPSyncCheck history for one site.

All admin-gated. Per-site views guard on ``Website.has_gbp_features()`` —
Essentials clients get a polite "upgrade to Growth+" message instead of
the feature pages.

Scoped per website, not per account. A Google Business Profile describes a
specific business location, and a firm running two brands has two listings;
binding one at account level meant the second site could never be synced at
all. ``gbp_location_name`` now lives on Website for the same reason.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from admin_dashboard.decorators import admin_required
from clients.account_models import Website

from reporting.google_gbp import list_locations
from reporting.models import (
    GBPSyncCheck,
    GbpOperatorToken,
    GbpPerformanceSnapshot,
    GbpReview,
)

logger = logging.getLogger(__name__)

GBP_TIERS = ['maintenance_growth', 'maintenance_dominant']


def _operator_token():
    """Whoever connected first (typically the agency owner). Returns
    None if nobody has connected yet."""
    return GbpOperatorToken.objects.order_by('created_at').first()


def _eligible_websites():
    """Sites whose billed package qualifies for GBP management.

    Comped access is deliberately not folded in here: a comp is stored on
    the Account across three separate fields, so expressing it as a single
    queryset filter would mean four ORed lookups that quietly drift from
    ``Website.has_gbp_features``. The per-site views gate on that method,
    which is the authority; this is only the dashboard's roll-up.
    """
    return Website.objects.filter(package__in=GBP_TIERS)


@admin_required
def dashboard(request):
    """Cross-site triage. Three sections:
        - Reviews needing attention (low star or unreplied)
        - NAP drift flagged
        - Sites with maintenance tier ≥ Growth but no bound location
    """
    token = _operator_token()

    # Reviews needing attention — pull top 20 newest across all sites.
    attention = (
        GbpReview.objects
        .filter(needs_attention=True)
        .select_related('website_new', 'website_new__account')
        .order_by('-review_created_at')[:20]
    )

    # NAP drift — latest unresolved mismatch per site.
    nap_mismatches = (
        GBPSyncCheck.objects
        .filter(is_mismatch=True, resolved=False)
        .select_related('website_new', 'website_new__account')
        .order_by('-checked_at')[:20]
    )

    # Eligible but unconnected sites.
    eligible = _eligible_websites()
    unbound = (eligible.filter(gbp_location_name='')
               .select_related('account')
               .order_by('account__name', 'name'))

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
    """Decorator — hard gating. GBP features are paid (or comped) per
    site; admins don't see the pages for sites that aren't on the
    feature. Use the Account's comp fields to grant tier access without
    billing (dogfood + free trials + agency-internal use)."""
    from functools import wraps

    @wraps(view)
    def _wrapped(request, website_id, *args, **kwargs):
        website = get_object_or_404(
            Website.objects.select_related('account'), id=website_id)
        if not website.has_gbp_features():
            return render(request, 'reporting/gbp/upgrade_required.html', {
                'active_nav': 'gbp',
                'profile':    website,
            })
        return view(request, website_id, *args, **kwargs)
    return _wrapped


def _website(website_id):
    return get_object_or_404(
        Website.objects.select_related('account'), id=website_id)


@admin_required
@_gbp_gate
def client_gbp(request, website_id):
    """Per-site GBP deep dive."""
    website = _website(website_id)
    latest_perf = (
        GbpPerformanceSnapshot.objects
        .filter(website_new=website)
        .order_by('-snapshot_month')
        .first()
    )
    recent_reviews = (
        GbpReview.objects
        .filter(website_new=website)
        .order_by('-review_created_at')[:10]
    )
    latest_nap = (
        GBPSyncCheck.objects
        .filter(website_new=website)
        .order_by('-checked_at')[:5]
    )
    operator_token = _operator_token()

    return render(request, 'reporting/gbp/client_gbp.html', {
        'active_nav':     'gbp',
        'profile':        website,
        'operator_token': operator_token,
        'connected':      operator_token is not None,
        'latest_perf':    latest_perf,
        'recent_reviews': recent_reviews,
        'latest_nap':     latest_nap,
        'bound':          bool(website.gbp_location_name),
        'is_premium':     website.has_gbp_premium_features(),
    })


@admin_required
@_gbp_gate
def locations_picker(request, website_id):
    """Pick which GBP location belongs to this site. Uses the
    operator's token to list every location they manage."""
    website = _website(website_id)
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
            return redirect('gbp:locations_picker', website_id=website.id)
        # One listing describes one location, so binding it to a second
        # site would make both sync the same reviews and silently
        # overwrite each other's NAP checks.
        clash = (Website.objects
                 .filter(gbp_location_name=resource_name)
                 .exclude(pk=website.pk)
                 .first())
        if clash is not None:
            messages.error(
                request,
                f'That location is already bound to {clash.name}. '
                'Unbind it there first.')
            return redirect('gbp:locations_picker', website_id=website.id)
        website.gbp_location_name = resource_name
        website.save(update_fields=['gbp_location_name', 'updated_at'])
        messages.success(
            request,
            f'Bound {website.name} to that GBP location. '
            'Review sync + NAP checks will run from here on.')
        return redirect('gbp:client_gbp', website_id=website.id)

    try:
        locations = list_locations(token)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'locations_picker: list_locations failed for %s', website.id)
        messages.error(
            request, f'Could not fetch GBP locations: {exc}')
        locations = []

    return render(request, 'reporting/gbp/locations_picker.html', {
        'active_nav': 'gbp',
        'profile':    website,
        'locations':  locations,
    })


@admin_required
@_gbp_gate
def reviews_list(request, website_id):
    """All cached reviews for one site. Premium tier can post
    replies inline; non-premium sees read-only."""
    website = _website(website_id)
    reviews = (
        GbpReview.objects
        .filter(website_new=website)
        .order_by('-review_created_at')[:200]
    )
    return render(request, 'reporting/gbp/reviews_list.html', {
        'active_nav': 'gbp',
        'profile':    website,
        'reviews':    reviews,
        'is_premium': website.has_gbp_premium_features(),
    })


@admin_required
@_gbp_gate
def nap_history(request, website_id):
    """GBPSyncCheck history for one site."""
    website = _website(website_id)
    checks = (
        GBPSyncCheck.objects
        .filter(website_new=website)
        .order_by('-checked_at')[:50]
    )
    return render(request, 'reporting/gbp/nap_history.html', {
        'active_nav': 'gbp',
        'profile':    website,
        'checks':     checks,
    })
