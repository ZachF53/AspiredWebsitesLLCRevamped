"""
Custom Stripe Elements checkout flow.

Replaces Stripe-hosted Checkout for the maintenance + social-media
subscriptions. Web Design always goes through the admin invoice path
(unchanged).

Flow:
    /checkout/<tier_slug>/                  GET   →  the checkout page
    /checkout/<tier_slug>/confirm/          POST  →  create Stripe Customer,
                                                     attach payment method,
                                                     create Subscription,
                                                     return next-step JSON
    /checkout/<tier_slug>/email-check/      POST  →  AJAX — does this email
                                                     already match an account?
    /checkout/<tier_slug>/success/          GET   →  success / next steps page

Stripe Elements is loaded client-side; cardholder data never touches
our server. The page uses Payment Intents + Setup Intents to support
SCA / 3DS confirmation right inside the page.
"""

import json
import logging
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from billing.pricing_models import ServiceTier

logger = logging.getLogger(__name__)
User = get_user_model()


# Stripe price IDs for the hosting move-over upsell line item. The
# annual hosting tier already has a price; we add a fixed $50 discount
# coupon at checkout to land at $100 first year.
HOSTING_FIRST_YEAR_DISCOUNT_CENTS = 5000   # $50 off year one


def _stripe():
    """Return a configured stripe module."""
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def _tier_allows_selfcheckout(tier):
    """Web design tiers DO NOT allow self-checkout — they go through
    the Schedule a Call page."""
    return tier.category in ('maintenance', 'social_media')


def checkout_page(request, tier_slug):
    """Render the checkout form. Tier comes from URL slug."""
    tier = get_object_or_404(
        ServiceTier, slug=tier_slug, is_active=True,
    )
    if not _tier_allows_selfcheckout(tier):
        # Web design / hosting / addons → redirect to schedule a call
        return redirect('/design/schedule/')

    # Maintenance customers can opt into hosting move-over inline.
    hosting_tier = ServiceTier.objects.filter(
        category='hosting', is_active=True,
    ).first()
    show_hosting_upsell = (
        tier.category == 'maintenance' and hosting_tier is not None
    )

    # First-year hosting price after the $50 move-over discount — this
    # is the exact amount that rides onto the first invoice, so the JS
    # can show a real combined total (plan + hosting) on the Pay button.
    hosting_first_year_price = None
    if show_hosting_upsell:
        hosting_first_year_price = (
            hosting_tier.price
            - Decimal(HOSTING_FIRST_YEAR_DISCOUNT_CENTS) / Decimal(100)
        )

    return render(request, 'billing/checkout.html', {
        'tier': tier,
        'hosting_tier': hosting_tier,
        'show_hosting_upsell': show_hosting_upsell,
        'hosting_first_year_price': hosting_first_year_price,
        'stripe_publishable_key': getattr(
            settings, 'STRIPE_PUBLISHABLE_KEY', ''),
    })


@require_POST
def checkout_email_check(request, tier_slug):
    """AJAX — given an email, tell the form whether it already exists."""
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return HttpResponse('bad json', status=400)
    email = (payload.get('email') or '').strip().lower()
    if not email:
        return JsonResponse({'exists': False})
    exists = User.objects.filter(email__iexact=email).exists()
    return JsonResponse({'exists': exists})


@csrf_exempt
@require_POST
def checkout_confirm(request, tier_slug):
    """
    Confirm purchase — server-side create Customer + Subscription.

    Body: {
        email: str,
        payment_method_id: str (from Stripe Elements),
        hosting_upsell: bool,
        billing_address: {...} (optional, captured by AddressElement)
    }

    Returns:
        success → { ok: true, subscription_id, client_secret, status }
        SCA needed → { requires_action: true, client_secret }
        error → { error: '...' }
    """
    tier = get_object_or_404(
        ServiceTier, slug=tier_slug, is_active=True,
    )
    if not _tier_allows_selfcheckout(tier):
        return JsonResponse({'error': 'tier not self-checkoutable'}, status=400)
    if not tier.stripe_price_id:
        return JsonResponse({
            'error': 'this tier is not configured with a Stripe price; '
                     'contact zachery@aspiredwebsites.com'}, status=400)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'bad json'}, status=400)

    email = (payload.get('email') or '').strip().lower()
    payment_method_id = (payload.get('payment_method_id') or '').strip()
    hosting_upsell = bool(payload.get('hosting_upsell'))

    if not email or not payment_method_id:
        return JsonResponse({
            'error': 'email and payment_method_id required'}, status=400)

    stripe = _stripe()

    # 1) Create or look up the Stripe Customer
    try:
        existing = stripe.Customer.list(email=email, limit=1).data
        if existing:
            customer = existing[0]
        else:
            customer = stripe.Customer.create(email=email)
    except Exception as exc:  # noqa: BLE001
        logger.exception('checkout: customer lookup/create failed')
        return JsonResponse({'error': str(exc)}, status=400)

    # 2) Attach payment method to customer + make default
    try:
        stripe.PaymentMethod.attach(payment_method_id, customer=customer.id)
        stripe.Customer.modify(
            customer.id,
            invoice_settings={'default_payment_method': payment_method_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('checkout: payment method attach failed')
        return JsonResponse({'error': str(exc)}, status=400)

    # 3) (Optional) hosting move-over — pre-add pending invoice items so
    #    they ride onto the subscription's first invoice. We pass an
    #    explicit `amount` rather than `price=` because Stripe's 2025+
    #    API removed the `price` param on InvoiceItem.create (it now
    #    400s "unknown parameter: price"). Net = list price - $50.
    if hosting_upsell:
        hosting_tier = ServiceTier.objects.filter(
            category='hosting', is_active=True).first()
        if hosting_tier and hosting_tier.price:
            try:
                stripe.InvoiceItem.create(
                    customer=customer.id,
                    amount=int(round(hosting_tier.price * 100)),
                    currency='usd',
                    description=f'{hosting_tier.name} (first year)',
                )
                stripe.InvoiceItem.create(
                    customer=customer.id,
                    amount=-HOSTING_FIRST_YEAR_DISCOUNT_CENTS,
                    currency='usd',
                    description='Hosting move-over — first-year $50 off',
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    'checkout: hosting upsell line items failed')
                # Continue with subscription anyway; admin can adjust.

    # 4) Create the subscription. payment_behavior=default_incomplete
    #    creates the first invoice + its PaymentIntent but does NOT
    #    charge yet. Stripe's 2025+ API dropped invoice.payment_intent;
    #    the PaymentIntent client secret now lives on the invoice's
    #    `confirmation_secret`, so that's what we expand.
    try:
        subscription = stripe.Subscription.create(
            customer=customer.id,
            items=[{'price': tier.stripe_price_id}],
            payment_behavior='default_incomplete',
            payment_settings={
                'save_default_payment_method': 'on_subscription'},
            expand=['latest_invoice.confirmation_secret'],
            metadata={
                'tier_slug': tier.slug,
                'product_type': tier.category,
                'hosting_upsell': '1' if hosting_upsell else '0',
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('checkout: subscription create failed')
        return JsonResponse({'error': str(exc)}, status=400)

    # 5) Pull the PaymentIntent client secret off the first invoice.
    #    NB: stripe 15's StripeObject has no .get()/.keys() — use `in`
    #    + subscripting, never dict methods, on Stripe objects.
    invoice = (subscription['latest_invoice']
               if 'latest_invoice' in subscription else None)
    client_secret = None
    if invoice is not None and 'confirmation_secret' in invoice:
        cs = invoice['confirmation_secret']
        if cs and 'client_secret' in cs:
            client_secret = cs['client_secret']

    # 6) Confirm the PaymentIntent server-side with the card we just
    #    attached, so the charge actually goes through. If the card
    #    needs SCA/3DS, Stripe returns requires_action and the browser
    #    finishes it via stripe.confirmCardPayment(client_secret).
    status = None
    if client_secret:
        payment_intent_id = client_secret.split('_secret', 1)[0]
        try:
            intent = stripe.PaymentIntent.confirm(
                payment_intent_id,
                payment_method=payment_method_id,
            )
            status = intent['status'] if 'status' in intent else None
        except Exception as exc:  # noqa: BLE001
            # CardError (declines) carries a user-friendly message;
            # surface it, fall back to str() for anything else.
            logger.exception('checkout: payment intent confirm failed')
            msg = getattr(exc, 'user_message', None) or str(exc)
            return JsonResponse({'error': msg}, status=400)

    if status == 'requires_action':
        return JsonResponse({
            'requires_action': True,
            'client_secret': client_secret,
            'subscription_id': subscription.id,
        })

    # succeeded / requires_capture / processing / indeterminate — the
    # webhook finalises the subscription record either way; send the
    # buyer to the success page.
    return JsonResponse({
        'ok': True,
        'subscription_id': subscription.id,
        'status': status,
        'redirect': reverse(
            'billing:checkout_success',
            kwargs={'tier_slug': tier_slug},
        ),
    })


def checkout_success(request, tier_slug):
    """Show the success / set-your-password-soon page."""
    tier = get_object_or_404(
        ServiceTier, slug=tier_slug, is_active=True,
    )
    return render(request, 'billing/checkout_success.html', {
        'tier': tier,
    })
