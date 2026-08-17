"""
Referral programme admin.

Split out of admin_dashboard/views.py. `admin_dashboard.views`
re-exports these names, so urls.py — which references them as
`views.<name>` — keeps working unchanged.
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .decorators import admin_required
from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)
from django.db.models import Count


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 2 — Referrals admin dashboard
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def referrals_list(request):
    """
    Admin view of every ReferralLink with rollup stats at the top and a
    per-link row table beneath. Sorted by conversions then leads so the
    most-effective referrers float up.
    """
    from clients.models import ReferralEvent, ReferralLink

    links = (ReferralLink.objects
             .select_related('client')
             .order_by('-conversions', '-leads_generated',
                       'client__firm_name'))

    totals = ReferralLink.objects.aggregate(
        total_clicks=Count('id'),  # placeholder, overwritten below
    )
    # Use SQL sum, not the placeholder above.
    from django.db.models import Sum
    agg = ReferralLink.objects.aggregate(
        clicks=Sum('clicks'),
        leads=Sum('leads_generated'),
        convs=Sum('conversions'),
        rewards=Sum('total_reward_value'),
    )
    rewards_given = ReferralEvent.objects.filter(
        reward_given=True).aggregate(s=Sum('reward_amount'))['s'] or 0

    return render(request, 'admin_dashboard/referrals_list.html',
                  _admin_context(
                      'referrals',
                      links=links,
                      total_clicks=agg['clicks'] or 0,
                      total_leads=agg['leads'] or 0,
                      total_conversions=agg['convs'] or 0,
                      total_rewards=agg['rewards'] or 0,
                      rewards_given=rewards_given,
                  ))


@admin_required
@require_POST
def referral_toggle_active(request, link_id):
    """Flip ReferralLink.is_active. Returns to the list."""
    from clients.models import ReferralLink
    link = get_object_or_404(ReferralLink, id=link_id)
    link.is_active = not link.is_active
    link.save(update_fields=['is_active', 'updated_at'])
    return redirect('admin_dashboard:referrals_list')


@admin_required
@require_POST
def referral_mark_conversion(request, link_id):
    """
    Record a conversion + optional reward against a ReferralLink.
    POST fields:
      reward_amount  (decimal, default 0)
      reward_note    (text)
    Creates a ReferralEvent(event_type='conversion') and bumps the
    parent link's counters in one go.
    """
    from decimal import Decimal, InvalidOperation

    from clients.models import ReferralEvent, ReferralLink

    link = get_object_or_404(ReferralLink, id=link_id)

    raw = (request.POST.get('reward_amount') or '0').strip()
    try:
        amount = Decimal(raw) if raw else Decimal('0')
    except InvalidOperation:
        amount = Decimal('0')
    note = (request.POST.get('reward_note') or '').strip()[:200]

    ReferralEvent.objects.create(
        referral_link=link,
        event_type='conversion',
        reward_given=amount > 0,
        reward_amount=amount,
        reward_note=note,
    )
    link.conversions = (link.conversions or 0) + 1
    if amount > 0:
        link.total_reward_value = (
            (link.total_reward_value or 0) + amount)
        link.save(update_fields=[
            'conversions', 'total_reward_value', 'updated_at'])
    else:
        link.save(update_fields=['conversions', 'updated_at'])

    return redirect('admin_dashboard:referrals_list')


