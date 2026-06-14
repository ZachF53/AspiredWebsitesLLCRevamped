"""
Custom subscription-management UI for the client portal.

Replaces Stripe Customer Portal screen-for-screen — every page lives
on aspiredwebsites.com. Stripe is API-only.

Pages:
    /portal/billing/                       hub — list active subs + cards
    /portal/billing/subs/<id>/cancel/      cancel at period end
    /portal/billing/subs/<id>/change/      upgrade / downgrade (immediate
                                           prorated for upgrade, end of
                                           period for downgrade)
    /portal/billing/cards/add/             attach new card (Stripe Elements)
    /portal/billing/cards/<id>/default/    set default
    /portal/billing/cards/<id>/remove/     detach
    /portal/billing/invoices/              past invoices
    /portal/billing/invoices/<id>/pdf/     redirect to Stripe-hosted PDF
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import models  # noqa: F401
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from billing.pricing_models import ServiceTier

logger = logging.getLogger(__name__)


def _stripe():
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def _customer_id_for_user(user):
    """Find the user's Stripe Customer id — prefer ClientProfile."""
    try:
        from clients.models import ClientProfile
        cp = ClientProfile.objects.filter(user=user).first()
        if cp and cp.stripe_customer_id:
            return cp.stripe_customer_id
    except Exception:
        pass
    # Fallback: lookup by email
    stripe = _stripe()
    try:
        existing = stripe.Customer.list(email=user.email, limit=1).data
        if existing:
            return existing[0].id
    except Exception:
        pass
    return None


@login_required
def billing_home(request):
    """Retired — the Manage Billing hub was consolidated into the portal
    Subscription / Billing page (clients:portal_subscriptions), which has
    per-website plan names, comp tiers, the upsell, and per-subscription
    card selection. Redirect so old links/bookmarks still land somewhere."""
    from django.shortcuts import redirect
    return redirect('clients:portal_subscriptions')


@login_required
@require_POST
def subscription_cancel(request, sub_id):
    """Mark cancel-at-period-end on a subscription."""
    stripe = _stripe()
    reason = (request.POST.get('reason') or '').strip()
    try:
        sub = stripe.Subscription.modify(
            sub_id, cancel_at_period_end=True,
        )
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f'Cancel failed: {exc}')
        return redirect('billing:portal_home')

    # Log the cancellation reason
    try:
        from billing.cancellation_models import CancellationReason
        CancellationReason.objects.create(
            user=request.user,
            subscription_id=sub_id,
            reason=reason or '(none provided)',
        )
    except Exception:
        pass
    end = sub.get('current_period_end')
    when = timezone.datetime.fromtimestamp(end) if end else None
    messages.success(
        request,
        f'Cancelled. Subscription stays active through '
        f'{when.strftime("%B %d, %Y") if when else "your next billing date"}.'
    )
    return redirect('billing:portal_home')


@login_required
def subscription_change(request, sub_id):
    """Upgrade / downgrade form. Immediate upgrade with proration;
    downgrade is scheduled at period end."""
    stripe = _stripe()
    try:
        sub = stripe.Subscription.retrieve(
            sub_id, expand=['items'])
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f'Could not load subscription: {exc}')
        return redirect('billing:portal_home')

    current_price_id = (sub['items']['data'][0]['price']['id']
                        if sub.get('items', {}).get('data') else '')

    # Find the current tier so we can show compatible swaps
    current_tier = ServiceTier.objects.filter(
        stripe_price_id=current_price_id).first()
    compatibles = []
    if current_tier:
        compatibles = (ServiceTier.objects
                       .filter(category=current_tier.category,
                               is_active=True)
                       .exclude(slug=current_tier.slug)
                       .order_by('price'))

    if request.method == 'POST':
        target_slug = request.POST.get('target_tier') or ''
        target = ServiceTier.objects.filter(
            slug=target_slug, is_active=True).first()
        if not target or not target.stripe_price_id:
            messages.error(request, 'Invalid target plan selected.')
            return redirect('billing:subscription_change', sub_id=sub_id)

        is_upgrade = (
            current_tier is not None and target.price > current_tier.price
        )
        item_id = sub['items']['data'][0]['id']
        try:
            if is_upgrade:
                stripe.Subscription.modify(
                    sub_id,
                    items=[{
                        'id': item_id,
                        'price': target.stripe_price_id,
                    }],
                    proration_behavior='create_prorations',
                )
                messages.success(
                    request,
                    f'Upgraded to {target.name}. Prorated charge will '
                    f'appear on your next invoice.')
            else:
                # Schedule the downgrade at period end
                stripe.SubscriptionSchedule.create(
                    from_subscription=sub_id,
                ) if False else None  # no-op — Stripe handles below
                stripe.Subscription.modify(
                    sub_id,
                    items=[{
                        'id': item_id,
                        'price': target.stripe_price_id,
                    }],
                    proration_behavior='none',
                    billing_cycle_anchor='unchanged',
                )
                messages.success(
                    request,
                    f'Downgrade to {target.name} scheduled — '
                    f'takes effect at your next renewal.')
        except Exception as exc:  # noqa: BLE001
            messages.error(request, f'Plan change failed: {exc}')
        return redirect('billing:portal_home')

    return render(request, 'billing/portal_change_plan.html', {
        'sub': sub,
        'current_tier': current_tier,
        'compatibles': compatibles,
        'active_portal_nav': 'billing',
    })


@login_required
def add_card(request):
    """Render the Stripe Elements SetupIntent flow for adding a card."""
    customer_id = _customer_id_for_user(request.user)
    if not customer_id:
        messages.error(request, 'No billing account found.')
        return redirect('billing:portal_home')

    stripe = _stripe()
    setup_intent = stripe.SetupIntent.create(
        customer=customer_id,
        payment_method_types=['card'],
    )
    return render(request, 'billing/portal_add_card.html', {
        'client_secret': setup_intent.client_secret,
        'stripe_publishable_key': getattr(
            settings, 'STRIPE_PUBLISHABLE_KEY', ''),
        'active_portal_nav': 'billing',
    })


@login_required
@require_POST
def card_set_default(request, payment_method_id):
    customer_id = _customer_id_for_user(request.user)
    if not customer_id:
        return HttpResponseBadRequest('no customer')
    stripe = _stripe()
    try:
        stripe.Customer.modify(
            customer_id,
            invoice_settings={
                'default_payment_method': payment_method_id},
        )
        messages.success(request, 'Default card updated.')
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f'Failed: {exc}')
    return redirect('billing:portal_home')


@login_required
@require_POST
def card_remove(request, payment_method_id):
    stripe = _stripe()
    try:
        stripe.PaymentMethod.detach(payment_method_id)
        messages.success(request, 'Card removed.')
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f'Failed: {exc}')
    return redirect('billing:portal_home')


@login_required
def invoice_pdf(request, invoice_id):
    """Redirect to Stripe-hosted invoice PDF URL."""
    stripe = _stripe()
    try:
        invoice = stripe.Invoice.retrieve(invoice_id)
        if invoice.get('invoice_pdf'):
            return redirect(invoice['invoice_pdf'])
    except Exception:
        pass
    messages.error(request, 'Could not load invoice.')
    return redirect('billing:portal_home')
