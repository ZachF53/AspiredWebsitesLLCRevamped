"""
Custom subscription-management UI for the client portal.

Replaces Stripe Customer Portal screen-for-screen — every page lives
on aspiredwebsites.com. Stripe is API-only.

There is deliberately NO link into Stripe's hosted Customer Portal
anywhere on this site. A Nov 2026 revision added one for the free
trial cancel path and it was removed again: every billing screen a
customer sees has to be ours, on our domain, in our branding. The
trial cancel flow lives at billing.checkout_views.trial_manage and
is reachable without a password for the length of the trial.

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


def _customer_id_for_request(request):
    """Stripe Customer id for whoever this request is acting as.

    Takes the request, not the user, so a staff "view as client" session
    resolves the TARGET's customer rather than the operator's. Reading
    request.user here would show an operator their own cards and
    invoices inside the client's portal — the page would quietly
    disagree with every other page around it.

    Prefers the Account record; falls back to a Stripe lookup by email.
    """
    from clients.impersonation import active_target

    target = active_target(request)
    user = request.user
    try:
        from clients.account_models import Account
        account = target or Account.objects.filter(user=user).first()
        if account and account.stripe_customer_id:
            return account.stripe_customer_id
    except Exception:
        pass
    # Fallback: lookup by email. Skipped while impersonating — the
    # operator's email would resolve to the operator's customer, which
    # is precisely the confusion this function exists to avoid.
    if target is not None:
        return None
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


def _subscription_period_end(sub):
    """When the current paid period ends, as an aware datetime or None.

    Two separate reasons the obvious one-liner does not work:

    1. ``sub.get(...)`` is not available. stripe 15's StripeObject has
       no dict methods, so ``.get()`` raises AttributeError. Membership
       is tested with ``in`` and read by subscripting.
    2. ``current_period_end`` moved. Stripe's 2025 API versions removed
       it from the Subscription and put it on each subscription ITEM,
       because a subscription with mixed intervals has no single period.
       We sell one item per subscription, so the first item's value is
       the answer, but it has to be read from there.

    Together those two were a 500 on every successful cancellation: the
    cancel itself applied, then the view raised while rendering the
    confirmation. The customer saw an error page and reasonably assumed
    the cancellation had failed.

    A trialing subscription has no paid period yet; ``trial_end`` is the
    date that matters to that customer, so it is the fallback.
    """
    if sub is None:
        return None

    candidates = []
    if 'items' in sub and sub['items'] and 'data' in sub['items']:
        for item in sub['items']['data'] or []:
            if 'current_period_end' in item and item['current_period_end']:
                candidates.append(item['current_period_end'])
    # Pre-2025 API versions, and any object that still carries it.
    if not candidates and 'current_period_end' in sub:
        if sub['current_period_end']:
            candidates.append(sub['current_period_end'])
    if not candidates and 'trial_end' in sub and sub['trial_end']:
        candidates.append(sub['trial_end'])
    if not candidates:
        return None

    from datetime import datetime, timezone as dt_timezone
    try:
        return datetime.fromtimestamp(int(max(candidates)),
                                      tz=dt_timezone.utc)
    except (TypeError, ValueError, OSError):
        logger.warning('portal: unreadable period end %r', candidates)
        return None


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

    # Everything past this point is confirmation copy. It must not be
    # able to fail: the cancellation has ALREADY been applied at Stripe,
    # so raising here tells the customer their cancellation did not work
    # when it did, and invites a second attempt or a support ticket.
    when = None
    try:
        when = _subscription_period_end(sub)
    except Exception:  # noqa: BLE001
        logger.exception('portal: period end unreadable for %s', sub_id)
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
    customer_id = _customer_id_for_request(request)
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
    customer_id = _customer_id_for_request(request)
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
    """Redirect to the Stripe-hosted invoice PDF, if it is this client's.

    The ownership check is the point. This used to retrieve whatever
    `invoice_id` the URL carried and redirect straight to its PDF, with
    @login_required as the only gate — so ANY authenticated client could
    read ANY other client's invoice (name, address, line items, amounts)
    by putting someone else's invoice id in the path. Stripe invoice ids
    are not secrets: they appear in receipt emails, payment pages and
    Stripe's own customer-facing URLs.

    Comparing against the requester's own customer id closes it. Failure
    returns the same generic message as a missing invoice, so the
    endpoint cannot be used to probe which invoice ids exist.
    """
    customer_id = _customer_id_for_request(request)
    stripe = _stripe()
    try:
        invoice = stripe.Invoice.retrieve(invoice_id)
        if not customer_id or invoice.get('customer') != customer_id:
            logger.warning(
                'invoice_pdf: user %s requested invoice %s belonging to '
                'customer %s (their customer is %s) — refused',
                request.user.pk, invoice_id, invoice.get('customer'),
                customer_id)
            messages.error(request, 'Could not load invoice.')
            return redirect('billing:portal_home')
        if invoice.get('invoice_pdf'):
            return redirect(invoice['invoice_pdf'])
    except Exception:
        pass
    messages.error(request, 'Could not load invoice.')
    return redirect('billing:portal_home')
