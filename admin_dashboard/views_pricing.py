"""
Pricing manager views.

Split out of admin_dashboard/views.py, which had grown to ~10,000 lines
and 512 definitions — a size at which nothing can be found by reading and
every merge touches the same file.

The split is behaviour-preserving. `admin_dashboard.views` re-exports
these names, so `urls.py` (which references them as `views.pricing_list`)
and any other caller keep working unchanged.

Pricing itself stays database-authoritative: these views edit ServiceTier
and TierFeature rows, they do not encode prices. See CLAUDE.md.
"""

from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .decorators import admin_required
from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)
from .forms import ServiceTierForm


# ────────────────────────────────────────────────────────────────────────────
# Pricing manager
# ────────────────────────────────────────────────────────────────────────────

_PRICING_CATEGORIES = [
    ('website_build', 'Website Builds'),
    ('maintenance', 'Maintenance Plans'),
    ('social_media', 'Social Media'),
    ('hosting', 'Hosting'),
]


@admin_required
def pricing_list(request):
    """Pricing manager — tiers grouped by category, plus add-ons."""
    from billing.pricing_models import AddonPricing, ServiceTier

    groups = [
        {
            'label': label,
            'category': key,
            'tiers': list(ServiceTier.objects.filter(category=key)),
        }
        for key, label in _PRICING_CATEGORIES
    ]
    missing_stripe = list(
        ServiceTier.objects.filter(
            is_active=True, is_recurring=True, stripe_price_id=''
        )
    )
    return render(request, 'admin_dashboard/pricing_list.html', _admin_context(
        'pricing',
        groups=groups,
        addons=list(AddonPricing.objects.all()),
        missing_stripe=missing_stripe,
        saved=request.GET.get('saved'),
    ))


@admin_required
def pricing_edit(request, tier_id):
    """Edit a single tier — its fields and its feature bullets."""
    from billing.pricing_models import ServiceTier

    tier = get_object_or_404(ServiceTier, id=tier_id)

    if request.method == 'POST':
        form = ServiceTierForm(request.POST, instance=tier)
        if form.is_valid():
            form.save()
            # Persist inline feature edits (text + sort order).
            for feature in tier.features.all():
                text = request.POST.get(f'feature_text_{feature.id}')
                if text is None:
                    continue
                feature.text = text.strip()
                raw_order = request.POST.get(f'feature_order_{feature.id}', '')
                if raw_order.strip().lstrip('-').isdigit():
                    feature.sort_order = int(raw_order)
                feature.save()
            return redirect(
                f"{reverse('admin_dashboard:pricing_list')}?saved={tier.name}"
            )
    else:
        form = ServiceTierForm(instance=tier)

    return render(request, 'admin_dashboard/pricing_edit.html', _admin_context(
        'pricing',
        tier=tier,
        form=form,
        features=list(tier.features.all()),
        is_build=tier.category == 'website_build',
    ))


@admin_required
@require_POST
def pricing_toggle(request, tier_id):
    """HTMX — flip is_active / is_featured on a tier."""
    from billing.pricing_models import ServiceTier

    tier = get_object_or_404(ServiceTier, id=tier_id)
    field = request.POST.get('field')
    if field not in ('is_active', 'is_featured'):
        return HttpResponseBadRequest('Unknown field')
    setattr(tier, field, not getattr(tier, field))
    tier.save(update_fields=[field, 'updated_at'])
    return render(request, 'admin_dashboard/_pricing_toggle.html', {
        'tier': tier,
        'field': field,
        'value': getattr(tier, field),
    })


@admin_required
@require_POST
def pricing_feature_add(request, tier_id):
    """HTMX — append a new (blank) feature row to a tier."""
    from billing.pricing_models import ServiceTier, TierFeature

    tier = get_object_or_404(ServiceTier, id=tier_id)
    feature = TierFeature.objects.create(
        tier=tier, text='', sort_order=tier.features.count() + 1,
    )
    return render(request, 'admin_dashboard/_pricing_feature_row.html', {
        'feature': feature,
    })


@admin_required
@require_POST
def pricing_feature_delete(request, tier_id, fid):
    """HTMX — delete a feature row."""
    from billing.pricing_models import TierFeature

    TierFeature.objects.filter(id=fid, tier_id=tier_id).delete()
    return HttpResponse('')


