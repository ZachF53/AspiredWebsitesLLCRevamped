"""
v2 Pricing — create and manage ServiceTiers from v2, not Django admin or v1.

v1's ServiceTierForm (admin_dashboard/forms.py) excludes category, slug,
is_recurring, and billing_interval — which is why a tier can be *edited*
on v1 but never *created* there. This module is a from-scratch v2 form
covering every field a tier needs to actually work. v1's pricing views,
forms, and templates are untouched — nothing here is imported by or
imports from admin_dashboard/views_pricing.py.

No Stripe writes happen anywhere in this module. Syncing stays the
`sync_stripe_products` management command, run by hand — every unsynced
tier gets the exact command surfaced on the page, not a button that runs
it. is_active with an empty stripe_price_id is a tier that's billable in
theory, listed everywhere is_active is checked, and cannot actually be
charged — that's the state that cost an hour before is_public existed to
even separate "billable" from "visible", so it's called out hard here.

No delete route. A tier carrying a stripe_price_id may have live
subscriptions against it — out of scope, same reasoning as the v2
live-subscription guards elsewhere in this build.
"""

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from admin_dashboard.decorators import admin_required
from billing.pricing_models import ServiceTier

_CATEGORIES = [
    ('website_build', 'Website Builds'),
    ('maintenance', 'Maintenance Plans'),
    ('social_media', 'Social Media'),
    ('hosting', 'Hosting'),
    ('addon', 'Add-Ons'),
]

# Model help_text: "'month', 'year', or blank for one-time." Constrained
# here (the model field itself has no `choices`) because sync_stripe_products
# passes this straight through as a Stripe recurring.interval — a typo
# would fail silently at sync time rather than at save time.
_BILLING_INTERVALS = ('', 'month', 'year')

_TOGGLE_FIELDS = ('is_active', 'is_public', 'is_featured')


def _validate_tier_fields(post, tier=None):
    """Validate raw POST data for a create/edit submission.

    `tier` is the ServiceTier being edited, or None for create. Returns
    (cleaned, errors): `cleaned` holds only fields that passed validation,
    `errors` is a list of human-readable messages.

    Price is deliberately left OUT of `cleaned` whenever `tier` already
    carries a stripe_price_id — not skipped-when-applying, never even
    parsed. A crafted POST with a `price` field has nothing downstream
    that reads it once this returns; the caller's setattr loop iterates
    `cleaned`, and 'price' is not in it.
    """
    errors = []
    cleaned = {}

    category = (post.get('category') or '').strip()
    if category not in dict(ServiceTier.CATEGORY_CHOICES):
        errors.append('Pick a valid category.')
    else:
        cleaned['category'] = category

    name = (post.get('name') or '').strip()
    if not name:
        errors.append('Name is required.')
    else:
        cleaned['name'] = name

    slug = (post.get('slug') or '').strip()
    if not slug:
        errors.append('Slug is required.')
    else:
        qs = ServiceTier.objects.filter(slug=slug)
        if tier is not None:
            qs = qs.exclude(id=tier.id)
        if qs.exists():
            errors.append(f'Slug "{slug}" is already in use.')
        else:
            cleaned['slug'] = slug

    price_locked = bool(tier and tier.stripe_price_id)
    if not price_locked:
        price_raw = (post.get('price') or '').strip()
        try:
            price = Decimal(price_raw)
            if price <= 0:
                raise InvalidOperation()
            cleaned['price'] = price
        except (InvalidOperation, ValueError):
            errors.append('Price must be a positive number.')

    cleaned['is_recurring'] = post.get('is_recurring') == 'on'

    billing_interval = (post.get('billing_interval') or '').strip()
    if billing_interval not in _BILLING_INTERVALS:
        errors.append("Billing interval must be 'month', 'year', or blank.")
    else:
        cleaned['billing_interval'] = billing_interval

    cleaned['tagline'] = (post.get('tagline') or '').strip()
    cleaned['description'] = (post.get('description') or '').strip()

    cleaned['is_active'] = post.get('is_active') == 'on'
    cleaned['is_public'] = post.get('is_public') == 'on'
    cleaned['is_featured'] = post.get('is_featured') == 'on'

    sort_raw = (post.get('sort_order') or '0').strip()
    try:
        cleaned['sort_order'] = int(sort_raw)
    except ValueError:
        errors.append('Sort order must be a whole number.')

    return cleaned, errors, price_locked


@admin_required
def pricing_list(request):
    tiers = list(
        ServiceTier.objects.all().order_by('category', 'sort_order', 'price'))
    groups = []
    for key, label in _CATEGORIES:
        groups.append({
            'key': key,
            'label': label,
            'tiers': [t for t in tiers if t.category == key],
        })
    unsynced_count = sum(
        1 for t in tiers if t.is_active and not t.stripe_price_id)
    return render(request, 'admin_dashboard/v2/pricing_list.html', {
        'groups': groups,
        'unsynced_count': unsynced_count,
    })


@admin_required
def pricing_create(request):
    if request.method == 'POST':
        cleaned, errors, _ = _validate_tier_fields(request.POST, tier=None)
        if errors:
            for e in errors:
                messages.error(request, e)
            return render(
                request, 'admin_dashboard/v2/pricing_create.html', {
                    'categories': ServiceTier.CATEGORY_CHOICES,
                    'post': request.POST,
                })
        tier = ServiceTier.objects.create(**cleaned)
        messages.success(
            request, f'{tier.name} created. Not yet synced to Stripe — '
                     f'run sync_stripe_products when ready to bill it.')
        return redirect('admin_dashboard:v2_pricing_detail', tier_id=tier.id)

    return render(request, 'admin_dashboard/v2/pricing_create.html', {
        'categories': ServiceTier.CATEGORY_CHOICES,
        'post': {},
    })


@admin_required
def pricing_detail(request, tier_id):
    tier = get_object_or_404(ServiceTier, id=tier_id)
    price_locked = bool(tier.stripe_price_id)

    if request.method == 'POST':
        cleaned, errors, _ = _validate_tier_fields(request.POST, tier=tier)
        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            for field, value in cleaned.items():
                setattr(tier, field, value)
            tier.save()
            messages.success(request, 'Saved.')
            return redirect(
                'admin_dashboard:v2_pricing_detail', tier_id=tier.id)

    return render(request, 'admin_dashboard/v2/pricing_detail.html', {
        'tier': tier,
        'categories': ServiceTier.CATEGORY_CHOICES,
        'price_locked': price_locked,
    })


@admin_required
@require_POST
def pricing_toggle(request, tier_id):
    tier = get_object_or_404(ServiceTier, id=tier_id)
    field = request.POST.get('field')
    if field not in _TOGGLE_FIELDS:
        messages.error(request, 'Unknown field.')
    else:
        setattr(tier, field, not getattr(tier, field))
        tier.save(update_fields=[field, 'updated_at'])
        messages.success(
            request,
            f'{tier.name}: {field} is now '
            f'{"on" if getattr(tier, field) else "off"}.')

    next_url = request.POST.get('next') or ''
    if not next_url.startswith('/'):
        next_url = reverse('admin_dashboard:v2_pricing_list')
    return redirect(next_url)
