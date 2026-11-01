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
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from billing.account_provisioning import (
    issue_password_setup_link,
    provision_self_checkout_account,
)
from billing.pricing_models import ServiceTier
from billing.trials import (
    confirm_card_setup,
    record_acceptance,
    start_trial_subscription,
    subscription_metadata,
    subscription_trial_end,
    tier_is_trial_eligible,
    trial_days,
    trial_end_date,
)

logger = logging.getLogger(__name__)
User = get_user_model()


# Stripe price IDs for the hosting move-over upsell line item. The
# annual hosting tier already has a price; we add a fixed $50 discount
# coupon at checkout to land at $100 first year.
HOSTING_FIRST_YEAR_DISCOUNT_CENTS = 5000   # $50 off year one
# Stable, reusable Stripe coupon id for the first-year hosting discount.
HOSTING_COUPON_ID = 'hosting_moveover_first_year_50'


def _ensure_hosting_coupon(stripe):
    """Get-or-create the one-time $50-off coupon used for the first year
    of the hosting move-over. Returns the coupon id, or None on failure."""
    try:
        stripe.Coupon.retrieve(HOSTING_COUPON_ID)
        return HOSTING_COUPON_ID
    except Exception:  # noqa: BLE001 — not found (or transient) → try create
        try:
            stripe.Coupon.create(
                id=HOSTING_COUPON_ID,
                amount_off=HOSTING_FIRST_YEAR_DISCOUNT_CENTS,
                currency='usd',
                duration='once',
                name='Hosting move-over — first year $50 off',
            )
            return HOSTING_COUPON_ID
        except Exception:  # noqa: BLE001
            logger.exception('checkout: hosting coupon ensure failed')
            return None


# "10% off first month" promise from the schedule-a-call add-on opt-in.
ADDON_FIRST_MONTH_COUPON_ID = 'addon_firstmonth_10pct'


def _ensure_addon_firstmonth_coupon(stripe):
    """Get-or-create the reusable 10%-off-first-month coupon. Returns the
    coupon id, or None on failure."""
    try:
        stripe.Coupon.retrieve(ADDON_FIRST_MONTH_COUPON_ID)
        return ADDON_FIRST_MONTH_COUPON_ID
    except Exception:  # noqa: BLE001 — not found (or transient) → try create
        try:
            stripe.Coupon.create(
                id=ADDON_FIRST_MONTH_COUPON_ID,
                percent_off=10,
                duration='once',
                name='Add-on — 10% off first month',
            )
            return ADDON_FIRST_MONTH_COUPON_ID
        except Exception:  # noqa: BLE001
            logger.exception('checkout: addon coupon ensure failed')
            return None


def _addon_optin_lead(email, category):
    """Return the most recent Lead at this email who opted into an add-on of
    the given ServiceTier category when booking a call, or None. This is the
    "10% off first month" promise made on the schedule page.
    """
    if not email:
        return None
    try:
        from billing.pricing_models import ServiceTier
        from outreach.models import Lead
        # Slugs in the same category the buyer is purchasing.
        category_slugs = set(
            ServiceTier.objects.filter(category=category)
            .values_list('slug', flat=True))
        leads = (Lead.objects
                 .filter(email__iexact=email)
                 .exclude(opted_in_addons=[])
                 .order_by('-opted_in_addons_at', '-created_at'))
        for lead in leads:
            opted = set(lead.opted_in_addons or [])
            if opted & category_slugs:
                return lead
    except Exception:  # noqa: BLE001 — discount is best-effort, never block sale
        logger.exception('checkout: addon opt-in lookup failed for %s', email)
    return None


def _create_hosting_subscription(stripe, customer_id, payment_method_id,
                                 parent_tier_slug):
    """Create the hosting move-over as its OWN annual subscription
    ($150/yr, $50 off the first year via a one-time coupon), charged
    immediately on the card we just saved. Returns the subscription or
    None. Never raises — a hosting failure must not undo the primary sale.
    """
    hosting_tier = _legacy_hosting_tier()
    if not (hosting_tier and hosting_tier.stripe_price_id):
        logger.warning('checkout: no hosting tier/price configured')
        return None
    coupon = _ensure_hosting_coupon(stripe)
    try:
        kwargs = {
            'customer': customer_id,
            'items': [{'price': hosting_tier.stripe_price_id}],
            'default_payment_method': payment_method_id,
            # product_type='hosting' with NO tier_slug → the
            # subscription.created webhook early-returns (no second
            # account-provision pass for the hosting sub).
            'metadata': {'product_type': 'hosting',
                         'parent_tier_slug': parent_tier_slug},
        }
        if coupon:
            kwargs['discounts'] = [{'coupon': coupon}]
        hosting_sub = stripe.Subscription.create(**kwargs)
        st = hosting_sub['status'] if 'status' in hosting_sub else None
        if st not in ('active', 'trialing'):
            # e.g. the follow-on $100 charge needed SCA and is now
            # incomplete. The primary sale already went through; flag it.
            logger.warning(
                'checkout: hosting subscription status=%s (not active) '
                'for customer %s', st, customer_id)
            try:
                from core.system_alerts import record_alert
                record_alert(
                    severity='warning',
                    source='billing.checkout.hosting_sub',
                    message=f'Hosting move-over subscription not active '
                            f'(status={st}) for {customer_id}',
                    detail='Primary plan charged OK; hosting follow-on '
                           'charge did not complete — chase the card.',
                )
            except Exception:
                pass
        return hosting_sub
    except Exception:  # noqa: BLE001
        logger.exception(
            'checkout: hosting subscription create failed for %s',
            customer_id)
        return None


def _stripe():
    """Return a configured stripe module."""
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def _stripe_error_message(exc, fallback='Your payment could not be '
                          'processed. Please try again.'):
    """Pull a clean, buyer-facing message out of a Stripe exception.

    Card declines surface as CardError; we want "Your card was declined."
    not str(exc), which prepends an ugly "Request req_xxx:". Prefer
    user_message, then the parsed error-body message, then a friendly
    generic fallback.
    """
    msg = getattr(exc, 'user_message', None)
    if not msg:
        body = getattr(exc, 'json_body', None) or {}
        err = body.get('error') if isinstance(body, dict) else None
        if isinstance(err, dict):
            msg = err.get('message')
    return msg or fallback


# Monthly Hosting + Security is self-checkout-able (Sept 2026). The legacy
# $150/yr hosting-annual tier never was — it is only ever the move-over
# upsell on a maintenance checkout.
SELF_CHECKOUT_HOSTING_SLUGS = ('hvac-hosting-security',)

# The standalone $97/mo Review System (Oct 2026). Allowlisted by slug for
# the same reason the hosting tiers are: 'addon' also covers the annual
# domain-registration tiers, which are NOT self-checkout-able and must
# stay on the schedule-a-call path.
SELF_CHECKOUT_ADDON_SLUGS = ('hvac-review-system',
                             'hvac-voice-receptionist')


def _tier_allows_selfcheckout(tier):
    """Web design tiers DO NOT allow self-checkout — they go through
    the Schedule a Call page. Monthly hosting and the monthly standalone
    add-ons do."""
    if tier.category == 'hosting':
        return (tier.slug in SELF_CHECKOUT_HOSTING_SLUGS
                and tier.billing_interval == 'month')
    if tier.category == 'addon':
        return (tier.slug in SELF_CHECKOUT_ADDON_SLUGS
                and tier.billing_interval == 'month')
    return tier.category in ('maintenance', 'social_media')


def _provision_product_type(tier):
    """Which PLAN-ROW shape a self-checkout sale gets.

    Hosting + Security buyers bring a site they already have, and
    standalone Review System buyers often bring no site of ours at all.
    Both ride the maintenance shape: MaintenancePlan already supports a
    null website plus an external_site_url, which is exactly the record
    those customers need.

    Semantics unchanged on purpose — see `_onboarding_product_type` for
    why this is now only half the question.
    """
    return ('maintenance' if tier.category in ('hosting', 'addon')
            else tier.category)


def _onboarding_product_type(tier):
    """Which WIZARD this buyer gets.

    Split out from `_provision_product_type` because one value was doing
    two jobs. A Review System buyer needs the `review_system` wizard
    (EIN, Google review link, job system, consent attestation), not the
    maintenance one that asks for their hosting-panel password — but
    they still need a `MaintenancePlan` row, and the plan-row branch in
    `provision_self_checkout_account` keys off the OTHER function.
    Returning 'review_system' from one function for both purposes would
    skip that branch entirely, leaving someone who just paid $97 with no
    plan row, no entitlement, and a portal offering to sell it to them
    again.
    """
    from clients.contract_options import (
        REVIEW_SYSTEM_SLUG, VOICE_RECEPTIONIST_SLUG,
    )
    if tier.slug == REVIEW_SYSTEM_SLUG:
        return 'review_system'
    if tier.slug == VOICE_RECEPTIONIST_SLUG:
        return 'voice_receptionist'
    return _provision_product_type(tier)


def _legacy_hosting_tier():
    """The $150/yr move-over tier, while it is still sold."""
    return ServiceTier.objects.filter(
        slug='hosting-annual', is_active=True).first()


def checkout_page(request, tier_slug):
    """Render the checkout form. Tier comes from URL slug."""
    tier = ServiceTier.objects.filter(slug=tier_slug).first()
    if tier is None:
        from django.http import Http404
        raise Http404('No such plan')
    if not tier.is_active:
        # A discontinued tier (old links, bookmarks, cached pricing
        # pages): point the visitor at what we sell now instead of a 404.
        return redirect('/design/schedule/')
    if not _tier_allows_selfcheckout(tier):
        # Web design / annual hosting / addons → schedule a call
        return redirect('/design/schedule/')

    # Maintenance customers can opt into the legacy hosting move-over
    # inline — only while that $150/yr tier is still sold. The Full Plan
    # already includes hosting, so with it retired the upsell is gone.
    hosting_tier = _legacy_hosting_tier()
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

    # Hourly rate for the "what happens after you pay" note (never
    # hardcode prices).
    from billing.pricing_models import AddonPricing
    hourly = AddonPricing.objects.filter(
        slug='addon-hourly', is_active=True).first()

    # The summary card appends a "Month-to-month — cancel anytime"
    # bullet for recurring tiers, but several tiers already say it in
    # their own feature bullets ("Month-to-month: cancel anytime",
    # "Month-to-month from day one: cancel anytime"), which printed two
    # near-identical lines in a row. Suppress ours when theirs covers
    # it, rather than editing the seeded features — the tier's own
    # wording is the one an operator controls.
    recurring_category = tier.category in ('maintenance', 'hosting', 'addon')
    already_says_monthly = any(
        'month-to-month' in (feature.text or '').lower()
        for feature in tier.features.all()
    )
    show_monthly_note = recurring_category and not already_says_monthly

    # Free-trial disclosure. The date is computed server-side from
    # TRIAL_DAYS, the same setting handed to Stripe, so the date the
    # buyer is shown before entering a card cannot drift from the date
    # Stripe actually bills. It is the "before the card is taken" half
    # of the trial disclosure rules; the reminder email is a Stripe
    # setting and the easy cancellation is the billing portal.
    is_trial = tier_is_trial_eligible(tier)

    return render(request, 'billing/checkout.html', {
        'tier': tier,
        'hourly_display': hourly.get_price_display() if hourly else '',
        'hosting_tier': hosting_tier,
        'show_hosting_upsell': show_hosting_upsell,
        'hosting_first_year_price': hosting_first_year_price,
        'show_monthly_note': show_monthly_note,
        'is_trial': is_trial,
        'trial_days': trial_days() if is_trial else 0,
        'trial_ends': trial_end_date() if is_trial else None,
        'terms_version': getattr(settings, 'TERMS_VERSION', ''),
        'billing_support_email': getattr(
            settings, 'BILLING_SUPPORT_EMAIL', ''),
        # Pre-purchase, there is no customer yet, so this points at the
        # client portal rather than a Stripe portal session. It is a
        # "here is where you will cancel" reference, not a live link.
        'billing_portal_url': reverse('billing:portal_home'),
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
                     'contact zacherylong@aspiredwebsites.com'}, status=400)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'bad json'}, status=400)

    email = (payload.get('email') or '').strip().lower()
    payment_method_id = (payload.get('payment_method_id') or '').strip()

    # The two $97/mo add-ons start as a free trial instead of charging
    # now. Branched here rather than threaded through the charge-now
    # path below, because that path's coupon handling, hosting upsell
    # and PaymentIntent confirmation are all meaningless on a $0 first
    # invoice, and interleaving the two produced a function where every
    # other line was an `if trial`.
    if tier_is_trial_eligible(tier):
        return _confirm_trial(request, tier, payload)

    # The upsell only exists on a maintenance checkout while the legacy
    # annual hosting tier is sold; ignore a stale flag otherwise.
    hosting_upsell = bool(payload.get('hosting_upsell')) and (
        tier.category == 'maintenance'
        and _legacy_hosting_tier() is not None)
    product_type = _provision_product_type(tier)
    onboarding_product_type = _onboarding_product_type(tier)

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
        # A declined card raises CardError right here, on attach.
        logger.exception('checkout: payment method attach failed')
        return JsonResponse(
            {'error': _stripe_error_message(exc)}, status=400)

    # 3) Hosting move-over is handled AFTER the primary charge as its own
    #    annual subscription (see step 6b) — not as a one-time line on
    #    this invoice — so it actually renews at $150/year.

    # 4) Create the subscription. payment_behavior=default_incomplete
    #    creates the first invoice + its PaymentIntent but does NOT
    #    charge yet. Stripe's 2025+ API dropped invoice.payment_intent;
    #    the PaymentIntent client secret now lives on the invoice's
    #    `confirmation_secret`, so that's what we expand.
    # 10%-off-first-month: if this buyer opted into an add-on of this
    # category when they booked a call, honour that promise with a
    # one-time coupon on the first invoice. Best-effort — never blocks.
    sub_kwargs = {
        'customer': customer.id,
        'items': [{'price': tier.stripe_price_id}],
        'payment_behavior': 'default_incomplete',
        'payment_settings': {
            'save_default_payment_method': 'on_subscription'},
        'expand': ['latest_invoice.confirmation_secret'],
        'metadata': {
            'tier_slug': tier.slug,
            'product_type': product_type,
            # Carried so the webhook backstop reaches the same decision
            # as this synchronous path. A subscription created BEFORE
            # this key existed simply lacks it, and the reader falls
            # back to product_type — the pre-deploy behaviour.
            'onboarding_product_type': onboarding_product_type,
            'hosting_upsell': '1' if hosting_upsell else '0',
        },
    }
    optin_lead = _addon_optin_lead(email, tier.category)
    if optin_lead is not None:
        coupon = _ensure_addon_firstmonth_coupon(stripe)
        if coupon:
            sub_kwargs['discounts'] = [{'coupon': coupon}]
            sub_kwargs['metadata']['addon_firstmonth_discount'] = '1'
            logger.info(
                'checkout: applied 10%% first-month coupon for %s (lead %s)',
                email, optin_lead.pk)
    try:
        subscription = stripe.Subscription.create(**sub_kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.exception('checkout: subscription create failed')
        return JsonResponse({'error': str(exc)}, status=400)

    # 4b) Provision the Django account NOW, synchronously. The webhook is
    #     async and was not reliably firing, leaving buyers paid-but-
    #     account-less. Create the account the moment the subscription
    #     exists so we can drop the buyer straight onto the set-password
    #     screen → dashboard. Idempotent with the webhook backstop.
    #     `redirect` defaults to the success page; a brand-new buyer is
    #     instead sent to /set-password/<token>/ (no login screen).
    setup_redirect = reverse('billing:checkout_success',
                             kwargs={'tier_slug': tier_slug})
    buyer = None
    try:
        buyer = provision_self_checkout_account(
            email=email,
            customer_id=customer.id,
            tier_slug=tier.slug,
            product_type=product_type,
            onboarding_product_type=onboarding_product_type,
            subscription_id=subscription.id,
            hosting_upsell=hosting_upsell,
            customer_name=getattr(customer, 'name', '') or '',
        )
        if buyer is not None and not buyer.has_usable_password():
            setup_redirect = issue_password_setup_link(
                buyer, send_email=True)
    except Exception:  # noqa: BLE001
        # Never fail the purchase over account setup — the webhook
        # backstop + checkout_success email still cover the buyer.
        logger.exception('checkout: account provisioning failed')

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
            # Surface a clean, buyer-facing message for declines etc.
            logger.exception('checkout: payment intent confirm failed')
            return JsonResponse(
                {'error': _stripe_error_message(exc)}, status=400)

    # 6b) Once the primary charge has gone through, add the hosting
    #     move-over as its own annual subscription on the same card, and
    #     record its id on the profile so the portal subscriptions page
    #     lists it.
    if hosting_upsell and status in (
            'succeeded', 'processing', 'requires_capture'):
        hosting_sub = _create_hosting_subscription(
            stripe, customer.id, payment_method_id, tier.slug)
        if hosting_sub is not None and buyer is not None and (
                'id' in hosting_sub):
            try:
                # Hosting is billed per site, so the subscription id
                # belongs on the Website. Stored on the account's only
                # site; with several there is no way to tell which was
                # bought, so the id is left for a human rather than
                # attached to whichever happens to be oldest.
                from clients.account_models import Account, Website
                account = Account.objects.filter(user=buyer).first()
                sites = (list(Website.objects.filter(account=account)[:2])
                         if account is not None else [])
                if len(sites) == 1:
                    site = sites[0]
                    site.stripe_hosting_subscription_id = hosting_sub['id']
                    site.save(update_fields=[
                        'stripe_hosting_subscription_id', 'updated_at'])
                elif len(sites) > 1:
                    logger.error(
                        'checkout: account %s owns %s sites — hosting sub '
                        '%s not attached to any of them',
                        getattr(account, 'pk', '?'), len(sites),
                        hosting_sub['id'])
            except Exception:  # noqa: BLE001
                logger.exception('checkout: store hosting sub id failed')

    if status == 'requires_action':
        # The browser finishes SCA, then navigates to `redirect` — the
        # set-password screen, same as the no-SCA path.
        return JsonResponse({
            'requires_action': True,
            'client_secret': client_secret,
            'subscription_id': subscription.id,
            'redirect': setup_redirect,
        })

    # succeeded / requires_capture / processing / indeterminate — send the
    # buyer straight to set their password (then their dashboard).
    return JsonResponse({
        'ok': True,
        'subscription_id': subscription.id,
        'status': status,
        'redirect': setup_redirect,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Free trial path (AI Voice Receptionist, Review System)
# ─────────────────────────────────────────────────────────────────────────────

def _trial_provision(stripe, *, acceptance, tier, subscription,
                     payment_method_id):
    """Create the Django account for a trial buyer. Returns (user, redirect).

    Same synchronous-provisioning reasoning as the charge-now path: the
    webhook is a backstop, not the primary route, and a buyer who has
    just handed over a card should land on the set-password screen
    rather than wait for an async event that may be minutes behind.

    Never raises. A provisioning failure must not leave the buyer
    believing their trial did not start, because in Stripe it did.
    """
    product_type = _provision_product_type(tier)
    onboarding_product_type = _onboarding_product_type(tier)
    setup_redirect = reverse('billing:trial_started',
                             kwargs={'tier_slug': tier.slug})
    setup_redirect = f'{setup_redirect}?ref={acceptance.pk}'

    buyer = None
    try:
        buyer = provision_self_checkout_account(
            email=acceptance.email,
            customer_id=acceptance.stripe_customer_id,
            tier_slug=tier.slug,
            product_type=product_type,
            onboarding_product_type=onboarding_product_type,
            subscription_id=subscription['id'],
            hosting_upsell=False,
            customer_name='',
            # Marks the voice cohort slot unconsumed until the first
            # invoice is paid. A trial must not move the price ladder.
            trial=True,
        )
        if buyer is not None and not buyer.has_usable_password():
            # Still send the buyer to the trial-started page first, not
            # straight to set-password: the disclosure of what gets
            # charged and when is the thing they need to see, and the
            # password email is already in their inbox.
            issue_password_setup_link(buyer, send_email=True)
    except Exception:  # noqa: BLE001
        logger.exception('checkout: trial provisioning failed for %s',
                         acceptance.email)
    return buyer, setup_redirect


def _confirm_trial(request, tier, payload):
    """Start a free trial: terms gate, SetupIntent, then subscription.

    Returns the same JSON shapes as the charge-now path so checkout.js
    needs no branch of its own, with one addition: `requires_action`
    here means "confirm this SetupIntent then POST to finalize_url",
    not "confirm this PaymentIntent and navigate".
    """
    email = (payload.get('email') or '').strip().lower()
    payment_method_id = (payload.get('payment_method_id') or '').strip()

    if not email or not payment_method_id:
        return JsonResponse({
            'error': 'email and payment_method_id required'}, status=400)

    # ── The terms gate. Server-side, before anything else happens. ──
    # A disabled button is a courtesy, not a control: the browser can
    # POST whatever it likes. This is the check that actually prevents a
    # subscription existing without a recorded acceptance, which is the
    # thing we would need to produce in a chargeback.
    if not payload.get('terms_accepted'):
        return JsonResponse({
            'error': 'Please accept the Terms of Service to continue.',
        }, status=400)

    acceptance = record_acceptance(request, email=email, tier=tier)

    stripe = _stripe()

    # 1) Customer, card, default payment method. Unchanged from the
    #    charge-now path — a trial still collects a real card.
    try:
        existing = stripe.Customer.list(email=email, limit=1).data
        customer = existing[0] if existing else stripe.Customer.create(
            email=email)
    except Exception as exc:  # noqa: BLE001
        logger.exception('checkout: trial customer lookup/create failed')
        return JsonResponse({'error': _stripe_error_message(exc)}, status=400)

    acceptance.stripe_customer_id = customer.id
    acceptance.save(update_fields=['stripe_customer_id', 'updated_at'])

    try:
        stripe.PaymentMethod.attach(payment_method_id, customer=customer.id)
        stripe.Customer.modify(
            customer.id,
            invoice_settings={'default_payment_method': payment_method_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('checkout: trial payment method attach failed')
        return JsonResponse({'error': _stripe_error_message(exc)}, status=400)

    # 2) Confirm a SetupIntent. This is what makes the trial safe: it
    #    authenticates the card and records the off-session mandate NOW,
    #    while the buyer is here, instead of discovering on day 15 that
    #    the card needed 3DS.
    metadata = subscription_metadata(
        acceptance, tier=tier,
        product_type=_provision_product_type(tier),
        onboarding_product_type=_onboarding_product_type(tier))
    try:
        intent = confirm_card_setup(
            stripe, customer_id=customer.id,
            payment_method_id=payment_method_id, metadata=metadata)
    except Exception as exc:  # noqa: BLE001
        logger.exception('checkout: trial SetupIntent failed')
        return JsonResponse({'error': _stripe_error_message(exc)}, status=400)

    status = intent['status'] if 'status' in intent else None
    if status == 'requires_action':
        # Hand the browser the 3DS challenge. No subscription exists
        # yet and none will until the SetupIntent succeeds.
        return JsonResponse({
            'requires_action': True,
            'setup_intent': True,
            'client_secret': (intent['client_secret']
                              if 'client_secret' in intent else ''),
            'finalize_url': reverse('billing:checkout_finalize',
                                    kwargs={'tier_slug': tier.slug}),
            'acceptance_ref': str(acceptance.pk),
        })
    if status != 'succeeded':
        logger.warning('checkout: trial SetupIntent status=%s for %s',
                       status, email)
        return JsonResponse({
            'error': 'We could not verify your card. Please try another '
                     'card, or email zacherylong@aspiredwebsites.com.',
        }, status=400)

    return _finish_trial(stripe, acceptance=acceptance, tier=tier,
                         payment_method_id=payment_method_id)


def _finish_trial(stripe, *, acceptance, tier, payment_method_id):
    """Create the trialing subscription and provision. Shared by both
    the direct path and the post-SCA finalize path."""
    try:
        subscription = start_trial_subscription(
            stripe, acceptance=acceptance, tier=tier,
            payment_method_id=payment_method_id,
            product_type=_provision_product_type(tier),
            onboarding_product_type=_onboarding_product_type(tier))
    except Exception as exc:  # noqa: BLE001
        logger.exception('checkout: trial subscription create failed')
        return JsonResponse({'error': _stripe_error_message(exc)}, status=400)

    _buyer, redirect_to = _trial_provision(
        stripe, acceptance=acceptance, tier=tier, subscription=subscription,
        payment_method_id=payment_method_id)

    return JsonResponse({
        'ok': True,
        'subscription_id': subscription['id'],
        'status': (subscription['status']
                   if 'status' in subscription else None),
        'redirect': redirect_to,
    })


@csrf_exempt
@require_POST
def checkout_finalize(request, tier_slug):
    """Create the trial subscription after the browser completed 3DS.

    Body: { acceptance_ref: uuid, payment_method_id: str }

    Email and Stripe customer are re-read from the TermsAcceptance row
    rather than taken from the request. The browser has already proved
    it holds the acceptance ref; letting it also name the customer would
    let a tampered POST attach a subscription to someone else's Stripe
    customer.
    """
    tier = get_object_or_404(ServiceTier, slug=tier_slug, is_active=True)
    if not tier_is_trial_eligible(tier):
        return JsonResponse({'error': 'tier is not on a trial'}, status=400)

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'bad json'}, status=400)

    ref = (payload.get('acceptance_ref') or '').strip()
    payment_method_id = (payload.get('payment_method_id') or '').strip()
    if not ref or not payment_method_id:
        return JsonResponse({
            'error': 'acceptance_ref and payment_method_id required'},
            status=400)

    acceptance = _acceptance_by_ref(ref, product_slug=tier.slug)
    if acceptance is None:
        return JsonResponse({'error': 'unknown acceptance'}, status=404)
    if acceptance.stripe_subscription_id:
        # Already finalized — a double-submit or a retried fetch. Send
        # them to the page rather than starting a second trial on the
        # same card.
        target = reverse('billing:trial_started',
                         kwargs={'tier_slug': tier.slug})
        return JsonResponse({'ok': True,
                             'redirect': f'{target}?ref={acceptance.pk}'})
    if not acceptance.stripe_customer_id:
        return JsonResponse({'error': 'acceptance has no customer'},
                            status=400)

    stripe = _stripe()

    # Verify the card really was set up. Without this check, a client
    # could skip the 3DS challenge and POST straight here to get a
    # trial on an unauthenticated card.
    try:
        intents = stripe.SetupIntent.list(
            customer=acceptance.stripe_customer_id, limit=10)
        confirmed = any(
            si['status'] == 'succeeded'
            and si['payment_method'] == payment_method_id
            for si in intents.data
        )
    except Exception:  # noqa: BLE001
        logger.exception('checkout: trial SetupIntent verify failed')
        return JsonResponse({
            'error': 'We could not verify your card. Please try again.'},
            status=400)
    if not confirmed:
        logger.warning(
            'checkout: finalize called with no succeeded SetupIntent for %s',
            acceptance.email)
        return JsonResponse({
            'error': 'Your card was not confirmed. Please try again.'},
            status=400)

    return _finish_trial(stripe, acceptance=acceptance, tier=tier,
                         payment_method_id=payment_method_id)


def _acceptance_by_ref(ref, *, product_slug=None):
    """A TermsAcceptance by UUID, or None on anything malformed.

    Scoped to the product when given, so a ref minted on one checkout
    cannot be replayed against the other product's page.

    The ref is parsed as a UUID before it reaches the ORM. A garbage
    value passed to .filter(pk=...) raises ValidationError on a
    UUIDField, which on the trial-started page would be a 500 on a
    mistyped URL rather than the "we could not find that" the visitor
    should get.
    """
    import uuid as _uuid

    from billing.terms_models import TermsAcceptance

    if not ref:
        return None
    try:
        parsed = _uuid.UUID(str(ref))
    except (ValueError, AttributeError, TypeError):
        return None

    qs = TermsAcceptance.objects.filter(pk=parsed)
    if product_slug:
        qs = qs.filter(product_slug=product_slug)
    try:
        return qs.first()
    except DjangoValidationError:
        return None


def trial_started(request, tier_slug):
    """The post-signup disclosure page.

    Shows the authoritative trial end date read back off the Stripe
    subscription, not the one computed from TRIAL_DAYS, because that is
    the date the card will actually be charged.

    Keyed on the TermsAcceptance UUID rather than a Stripe subscription
    id. A bare `sub_...` in a query string is close enough to guessable
    that it would let anyone holding one enumerate other buyers; a
    UUID4 is not enumerable, and what it unlocks is only the product
    name and a date.
    """
    tier = get_object_or_404(ServiceTier, slug=tier_slug, is_active=True)

    acceptance = _acceptance_by_ref(
        (request.GET.get('ref') or '').strip(), product_slug=tier.slug)

    trial_ends = None
    if acceptance is not None and acceptance.stripe_subscription_id:
        try:
            stripe = _stripe()
            sub = stripe.Subscription.retrieve(
                acceptance.stripe_subscription_id)
            trial_ends = subscription_trial_end(sub)
        except Exception:  # noqa: BLE001
            logger.exception('trial_started: subscription retrieve failed')

    # Fall back to the computed date so the page always states a date.
    # A page that says "you will be charged" without saying when is the
    # exact disclosure gap the trial rules are about.
    if trial_ends is None:
        trial_ends = trial_end_date(
            start=acceptance.accepted_at if acceptance else None)

    # The cancel link carries the acceptance ref so it works for a buyer
    # who has not set a password yet. Cancellation has to be at least as
    # easy as signup, and signup required no login.
    portal_url = reverse('billing:stripe_portal')
    if acceptance is not None:
        portal_url = f'{portal_url}?ref={acceptance.pk}'

    return render(request, 'billing/trial_started.html', {
        'tier': tier,
        'acceptance': acceptance,
        'trial_days': trial_days(),
        'trial_ends': trial_ends,
        'billing_portal_url': portal_url,
    })


def checkout_success(request, tier_slug):
    """Show the success / set-your-password-soon page."""
    tier = get_object_or_404(
        ServiceTier, slug=tier_slug, is_active=True,
    )
    return render(request, 'billing/checkout_success.html', {
        'tier': tier,
    })
