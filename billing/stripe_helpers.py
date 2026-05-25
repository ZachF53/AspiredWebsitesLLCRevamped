"""
Stripe integration helpers — customers, build invoices, maintenance subs.

Every public function raises StripeNotConfigured if STRIPE_SECRET_KEY is unset,
so callers can degrade gracefully in development.
"""

import logging
from decimal import Decimal

import stripe
from django.conf import settings

logger = logging.getLogger(__name__)


class StripeNotConfigured(RuntimeError):
    """Raised when a Stripe call is attempted without STRIPE_SECRET_KEY set."""


def _init():
    if not settings.STRIPE_SECRET_KEY:
        raise StripeNotConfigured('STRIPE_SECRET_KEY is not set in .env')
    stripe.api_key = settings.STRIPE_SECRET_KEY


def _cents(amount):
    """Convert a dollar Decimal/number to integer cents."""
    return int((Decimal(amount) * 100).to_integral_value())


def create_or_get_customer(client):
    """
    Return the client's Stripe Customer, creating + storing it ONLY if
    we've never had one for them OR the existing one has been
    explicitly hard-deleted at Stripe.

    CRITICAL: this function must NEVER silently swap a stripe_customer_id
    on a transient error. Doing so orphans the customer's saved cards,
    invoices, and subscriptions — they all stay on the old Stripe
    customer but the DB now points elsewhere. From the client's POV
    their card "disappears" mid-flow.

    The old version used `customer.get('deleted')` to detect deletes,
    which raises AttributeError on Stripe Python v15 StripeObjects
    (no `.get` method). The bare `except Exception` swallowed that
    error and silently created a replacement on every single call.
    """
    _init()
    if client.stripe_customer_id:
        try:
            customer = stripe.Customer.retrieve(client.stripe_customer_id)
        except stripe.error.InvalidRequestError as exc:
            # Stripe returns 404 → InvalidRequestError. This is the
            # only safe-to-rotate case: the customer is genuinely
            # gone (manually purged in the dashboard, or never
            # existed in this Stripe environment, e.g. live vs test
            # mode mismatch).
            logger.error(
                'Stripe customer %s NOT FOUND for client %s — '
                'creating a replacement. Old customer\'s cards + '
                'history are orphaned in Stripe; manual recovery '
                'may be needed. Underlying error: %s',
                client.stripe_customer_id, client.pk, exc)
        except Exception:
            # ANY other failure (network, rate limit, parser quirk,
            # auth) — re-raise. We must NOT silently create a new
            # customer; that's exactly the bug that lost a card.
            logger.exception(
                'Stripe customer retrieval error for client %s — '
                'refusing to create a replacement (would orphan '
                'card/history); raising.', client.pk)
            raise
        else:
            # StripeObject inherits from dict so [] indexing works,
            # but `.get()` was removed in v15 — use getattr for safety.
            if not getattr(customer, 'deleted', False):
                return customer
            # Customer exists but is in deleted state — only path
            # where we fall through to recreation.
            logger.error(
                'Stripe customer %s is in DELETED state for client '
                '%s — creating a replacement',
                client.stripe_customer_id, client.pk)

    customer = stripe.Customer.create(
        email=client.user.email,
        name=client.firm_name,
        metadata={'client_profile_id': str(client.id)},
    )
    client.stripe_customer_id = customer.id
    client.save(update_fields=['stripe_customer_id', 'updated_at'])
    return customer


def _create_build_invoice(client, contract, kind, description):
    """Shared deposit/final invoice builder. Stripe auto-emails the invoice."""
    _init()
    customer = create_or_get_customer(client)
    amount = contract.deposit_amount if kind == 'deposit' else contract.final_amount
    stripe.InvoiceItem.create(
        customer=customer.id,
        amount=_cents(amount),
        currency='usd',
        description=description,
    )
    invoice = stripe.Invoice.create(
        customer=customer.id,
        collection_method='send_invoice',
        days_until_due=7,
        metadata={
            'kind': kind,
            'client_profile_id': str(client.id),
            'contract_id': str(contract.id),
        },
    )
    invoice = stripe.Invoice.finalize_invoice(invoice.id)
    stripe.Invoice.send_invoice(invoice.id)
    return invoice


def create_deposit_invoice(client, contract):
    """Create + send the 50% deposit invoice for a build contract."""
    label = contract.get_package_display()
    return _create_build_invoice(
        client, contract, 'deposit', f'{label} — Deposit (50%)',
    )


def create_final_invoice(client, contract):
    """Create + send the final 50% invoice for a build contract."""
    label = contract.get_package_display()
    return _create_build_invoice(
        client, contract, 'final', f'{label} — Final Payment',
    )


def get_maintenance_tier(plan_slug):
    """
    Look up an active maintenance ServiceTier by slug. Raises ValueError
    if the slug is invalid OR the tier has no Stripe Price ID set.

    Public so views can use it for confirm-page render and for early
    validation before hitting Stripe.
    """
    from billing.pricing_models import ServiceTier

    tier = ServiceTier.objects.filter(
        slug=plan_slug, is_active=True, category='maintenance').first()
    if tier is None:
        raise ValueError(f'No active maintenance tier with slug "{plan_slug}".')
    if not tier.stripe_price_id:
        raise ValueError(
            f"No Stripe Price ID set for '{tier.name}'. Run "
            f"`python manage.py sync_stripe_products` to bootstrap it.")
    return tier


# Map a ServiceTier slug to the ClientProfile.PACKAGE_CHOICES code so the
# local `package` column stays in sync with Stripe.
MAINTENANCE_TIER_TO_PACKAGE = {
    'maintenance-essentials': 'maintenance_essentials',
    'maintenance-growth': 'maintenance_growth',
    'maintenance-dominant': 'maintenance_dominant',
}


def create_maintenance_subscription(client, plan_slug):
    """
    Create-or-return a recurring maintenance subscription for the client.

    Idempotent: if the client already has an active/trialing maintenance
    subscription on Stripe, the existing one is returned rather than
    double-billing. If the existing subscription is on a DIFFERENT
    tier from the one requested, callers should use
    `change_maintenance_subscription_tier` instead — this function does
    NOT swap tiers.

    Like `create_hosting_subscription`, we INTENTIONALLY do NOT pass
    `default_payment_method` so renewals fall back to the customer's
    invoice-settings default. Whichever card the client marks Default
    in /portal/subscriptions/ is what Stripe charges.

    Raises:
      StripeNotConfigured — STRIPE_SECRET_KEY missing
      ValueError          — bad plan slug, no Price ID, no PM on file
    """
    _init()
    tier = get_maintenance_tier(plan_slug)
    customer = create_or_get_customer(client)

    # If we already have a maintenance sub on file, return it (or its
    # fresh state) rather than double-creating.
    if client.stripe_subscription_id:
        try:
            existing = stripe.Subscription.retrieve(
                client.stripe_subscription_id)
            existing_status = getattr(existing, 'status', '')
            if existing_status not in ('canceled', 'incomplete_expired'):
                logger.info(
                    'create_maintenance_subscription: client %s already '
                    'has subscription %s (%s) — skipping create',
                    client.pk, existing.id, existing_status)
                return existing
        except Exception:
            client.stripe_subscription_id = ''

    # Confirm the customer has a default payment method on file.
    default_pm = get_customer_default_payment_method(customer.id)
    if not default_pm:
        raise ValueError(
            'No default payment method on file. Add a card on the '
            'subscriptions page before subscribing.')

    subscription = stripe.Subscription.create(
        customer=customer.id,
        items=[{'price': tier.stripe_price_id}],
        metadata={
            'kind': 'maintenance',
            'client_profile_id': str(client.id),
            'plan': tier.slug,
        },
        # `payment_behavior='error_if_incomplete'` makes the API call
        # fail loud if the saved card declines — better than silently
        # creating an `incomplete` subscription that just sits there.
        payment_behavior='error_if_incomplete',
    )
    client.stripe_subscription_id = subscription.id
    client.package = MAINTENANCE_TIER_TO_PACKAGE.get(tier.slug, client.package)
    client.save(update_fields=[
        'stripe_subscription_id', 'package', 'updated_at',
    ])
    logger.info(
        'create_maintenance_subscription: client %s subscribed to %s '
        '(sub %s)', client.pk, tier.slug, subscription.id)
    return subscription


def change_maintenance_subscription_tier(client, new_plan_slug):
    """
    Swap an existing maintenance subscription to a different tier.

    Stripe handles proration automatically (`proration_behavior=
    'create_prorations'`) — if upgrading, the client is charged the
    prorated difference on their next invoice; if downgrading, they
    get a prorated credit. The subscription ID stays the same.

    Also un-cancels a subscription that was set to cancel at period
    end (the client effectively re-subscribed by upgrading).

    Raises ValueError if the client has no active subscription to
    modify or the new slug is invalid.
    """
    _init()
    if not client.stripe_subscription_id:
        raise ValueError(
            'No active maintenance subscription to change. Subscribe '
            'first.')

    tier = get_maintenance_tier(new_plan_slug)
    sub = stripe.Subscription.retrieve(client.stripe_subscription_id)
    items_obj = getattr(sub, 'items', None)
    items_data = list(getattr(items_obj, 'data', [])) if items_obj else []
    if not items_data:
        raise ValueError(
            f'Subscription {client.stripe_subscription_id} has no items.')
    item_id = items_data[0].id

    updated = stripe.Subscription.modify(
        client.stripe_subscription_id,
        cancel_at_period_end=False,
        items=[{'id': item_id, 'price': tier.stripe_price_id}],
        proration_behavior='create_prorations',
        metadata={
            **(getattr(sub, 'metadata', {}) or {}),
            'kind': 'maintenance',
            'plan': tier.slug,
        },
    )
    client.package = MAINTENANCE_TIER_TO_PACKAGE.get(tier.slug, client.package)
    client.save(update_fields=['package', 'updated_at'])
    logger.info(
        'change_maintenance_subscription_tier: client %s swapped to %s',
        client.pk, tier.slug)
    return updated


def cancel_maintenance_subscription(client, reason=''):
    """
    Cancel the client's maintenance subscription at period end so they
    keep service through what they've already paid for. Local
    `maintenance_active` flips False only when the period actually
    ends (handled in `customer.subscription.deleted` webhook).
    """
    _init()
    if not client.stripe_subscription_id:
        return None
    try:
        sub = stripe.Subscription.retrieve(client.stripe_subscription_id)
    except Exception:
        client.stripe_subscription_id = ''
        client.maintenance_active = False
        client.save(update_fields=[
            'stripe_subscription_id', 'maintenance_active', 'updated_at'])
        return None
    if getattr(sub, 'status', '') in ('canceled', 'incomplete_expired'):
        return sub
    updated = stripe.Subscription.modify(
        client.stripe_subscription_id,
        cancel_at_period_end=True,
        metadata={**(getattr(sub, 'metadata', {}) or {}),
                  'cancel_reason': reason[:200] if reason else ''},
    )
    logger.info(
        'cancel_maintenance_subscription: client %s sub %s '
        'cancel_at_period_end=True (reason=%s)',
        client.pk, client.stripe_subscription_id, reason)
    return updated


def resume_maintenance_subscription(client):
    """
    Undo a pending cancel-at-period-end on the maintenance sub. No-op
    if the sub already isn't scheduled to cancel.
    """
    _init()
    if not client.stripe_subscription_id:
        return None
    return stripe.Subscription.modify(
        client.stripe_subscription_id, cancel_at_period_end=False,
    )


def get_hosting_price_id():
    """
    Returns the Stripe Price ID for the annual hosting subscription.

    Pulled from settings.STRIPE_PRICE_HOSTING_YEARLY (env var). The
    `sync_stripe_subscription_products` management command bootstraps
    the Stripe Product + Price and prints the ID for the operator to
    paste into .env.
    """
    pid = getattr(settings, 'STRIPE_PRICE_HOSTING_YEARLY', '')
    if not pid:
        raise StripeNotConfigured(
            'STRIPE_PRICE_HOSTING_YEARLY is not set in .env — run '
            '`python manage.py sync_stripe_subscription_products` '
            'to bootstrap the hosting Product + Price, then paste '
            'the printed Price ID into .env.')
    return pid


def attach_payment_method_to_customer(customer_id, payment_method_id,
                                      set_as_default=True):
    """
    Attach a payment method to a Stripe Customer, optionally setting
    it as the default invoice payment method. Idempotent — Stripe
    is fine with re-attaching the same PM.
    """
    _init()
    try:
        stripe.PaymentMethod.attach(
            payment_method_id, customer=customer_id)
    except stripe.error.InvalidRequestError as exc:
        # "already attached to this customer" is OK; anything else
        # is a real error.
        if 'already' not in str(exc).lower():
            raise
    if set_as_default:
        stripe.Customer.modify(
            customer_id,
            invoice_settings={
                'default_payment_method': payment_method_id,
            },
        )


def create_hosting_subscription(client, default_payment_method_id=None):
    """
    Create the annual hosting subscription for a client.

    `trial_period_days=365` so the FIRST recurring charge fires 365
    days from now — the lump-sum payment they just made covers year 1.
    Subsequent renewals are gated by the `invoice.upcoming` webhook in
    billing/webhooks.py.

    We INTENTIONALLY do NOT set `default_payment_method` on the
    subscription itself. Stripe's charge priority is:
      1. subscription.default_payment_method
      2. customer.invoice_settings.default_payment_method
    Skipping #1 means every renewal falls back to the customer
    default, so whichever card the client marks as Default in
    /portal/subscriptions/ is the one Stripe charges. The
    `default_payment_method_id` arg is still accepted for backward
    compatibility but it's IGNORED — passing it would lock the
    subscription to that one card forever.

    Returns the Stripe Subscription object. Idempotent: if the client
    already has a hosting subscription, returns the existing one
    rather than double-billing.
    """
    _init()
    if client.stripe_hosting_subscription_id:
        try:
            existing = stripe.Subscription.retrieve(
                client.stripe_hosting_subscription_id)
            existing_status = getattr(existing, 'status', '')
            if existing_status not in ('canceled', 'incomplete_expired'):
                logger.info(
                    'create_hosting_subscription: client %s already has '
                    'subscription %s — skipping', client.pk, existing.id)
                return existing
        except Exception:
            # Subscription doesn't exist any more on Stripe's side —
            # fall through and create a fresh one.
            client.stripe_hosting_subscription_id = ''

    if not client.stripe_customer_id:
        raise ValueError(
            f'Client {client.pk} has no stripe_customer_id — cannot '
            f'create a subscription.')

    # default_payment_method_id is intentionally NOT passed to
    # stripe.Subscription.create — see docstring. We do still want
    # the customer to HAVE a default though, so the very first
    # renewal doesn't fail; the webhook that calls us already
    # called attach_payment_method_to_customer(set_as_default=True)
    # before reaching here.
    sub = stripe.Subscription.create(
        customer=client.stripe_customer_id,
        items=[{'price': get_hosting_price_id()}],
        trial_period_days=365,
        metadata={
            'kind': 'hosting',
            'client_profile_id': str(client.id),
        },
        payment_behavior='allow_incomplete',
    )
    client.stripe_hosting_subscription_id = sub.id
    client.save(update_fields=[
        'stripe_hosting_subscription_id', 'updated_at'])
    logger.info(
        'create_hosting_subscription: client %s subscribed (sub %s, '
        'trial until %s — renewals will charge customer.default_pm)',
        client.pk, sub.id, getattr(sub, 'trial_end', None))
    return sub


def cancel_hosting_subscription(client, reason=''):
    """
    Cancel the client's hosting subscription at the end of the
    current period (so they don't lose access mid-cycle). Sets
    cancel_at_period_end=True; the row stays on Stripe for history
    but won't generate any future invoices.

    No-op if the client has no hosting sub or it's already canceled.
    """
    _init()
    sub_id = client.stripe_hosting_subscription_id
    if not sub_id:
        return None
    try:
        sub = stripe.Subscription.retrieve(sub_id)
    except Exception:
        # Already gone from Stripe — clear our reference.
        client.stripe_hosting_subscription_id = ''
        client.save(update_fields=[
            'stripe_hosting_subscription_id', 'updated_at'])
        return None
    if sub.get('status') in ('canceled', 'incomplete_expired'):
        return sub
    updated = stripe.Subscription.modify(
        sub_id,
        cancel_at_period_end=True,
        metadata={**(sub.get('metadata') or {}),
                  'cancel_reason': reason[:200] if reason else ''},
    )
    logger.info(
        'cancel_hosting_subscription: client %s sub %s set to '
        'cancel_at_period_end (reason=%s)',
        client.pk, sub_id, reason)
    return updated


def list_customer_payment_methods(customer_id):
    """List active card payment methods attached to a customer.

    Returns plain dicts (via .to_dict_recursive()) so the template can
    iterate them as `pm.card.last4` etc. via dot-attribute, and Django
    template lookups don't trip on StripeObject's restrictive
    attribute-only API (no .get())."""
    _init()
    if not customer_id:
        return []
    methods = stripe.PaymentMethod.list(
        customer=customer_id, type='card', limit=20)
    return list(methods.data) if hasattr(methods, 'data') else []


def get_customer_default_payment_method(customer_id):
    """Return the customer's default invoice payment method ID, or ''."""
    _init()
    if not customer_id:
        return ''
    cust = stripe.Customer.retrieve(customer_id)
    # Stripe v8 removed dict-like .get() on StripeObject — use attr
    # access only. invoice_settings may be None on customers that have
    # never had one set, so guard both levels.
    inv = getattr(cust, 'invoice_settings', None)
    if inv is None:
        return ''
    return getattr(inv, 'default_payment_method', '') or ''


def set_customer_default_payment_method(customer_id, payment_method_id):
    """Set the default invoice payment method on a customer."""
    _init()
    stripe.Customer.modify(
        customer_id,
        invoice_settings={
            'default_payment_method': payment_method_id,
        },
    )


def detach_payment_method(payment_method_id):
    """Detach (remove) a payment method from its customer."""
    _init()
    return stripe.PaymentMethod.detach(payment_method_id)


def create_setup_intent_for_customer(customer_id):
    """
    Create a Stripe SetupIntent so a client can save a new card via
    Stripe Elements on the portal without making a payment. Returns
    the SetupIntent object — the caller hands its `client_secret`
    to Stripe.js.
    """
    _init()
    intent = stripe.SetupIntent.create(
        customer=customer_id,
        payment_method_types=['card'],
        usage='off_session',
    )
    return intent


def create_onboarding_payment_intent(*, email, name, line_items,
                                     client_profile_id, invoice_id):
    """
    Create-or-reuse a Stripe Customer + a single PaymentIntent for the
    new on-site onboarding payment flow.

    Replaces `create_onboarding_invoice` (which used Stripe Invoices +
    Stripe-hosted hosted-invoice pages). We DON'T create a Stripe
    Invoice — the line items live on our OnboardingInvoice row and
    render on our own payment page. Stripe just processes the card.

    Settings:
      - `payment_method_types=['card']` — card-only. No Apple Pay /
        Google Pay / Link / Affirm / etc. (Per spec — wallets are
        explicitly off.)
      - `receipt_email` is intentionally NOT set — Stripe only sends
        its built-in receipt when this is provided. We send our own
        branded PDF receipt instead.
      - `metadata.invoice_id` lets the webhook find the OnboardingInvoice
        on `payment_intent.succeeded`.

    Returns (customer, payment_intent).
    """
    _init()
    customer = stripe.Customer.create(
        email=email,
        name=name,
        metadata={
            'source': 'aspired_websites',
            'client_profile_id': str(client_profile_id),
        },
    )

    total = sum(item['amount'] for item in line_items)
    description_lines = ' · '.join(item['description'] for item in line_items)

    payment_intent = stripe.PaymentIntent.create(
        amount=_cents(total),
        currency='usd',
        customer=customer.id,
        # Card-only — wallets explicitly off per spec.
        payment_method_types=['card'],
        # No `receipt_email` => no Stripe receipt; we send our own.
        description=f'Aspired Websites — {description_lines}'[:1000],
        # Save the card off-session so the same card auto-renews the
        # hosting subscription (and any future subs) without asking
        # again. The webhook attaches it to the customer + sets as
        # default after the PI succeeds.
        setup_future_usage='off_session',
        metadata={
            'source': 'aspired_websites',
            'kind': 'onboarding',
            'client_profile_id': str(client_profile_id),
            'invoice_id': str(invoice_id),
        },
    )
    return customer, payment_intent


def create_onboarding_invoice(*, email, name, line_items, client_profile_id):
    """
    Create + finalize a single one-off Stripe invoice for the new admin
    onboarding-invoice flow (Part 2 of the onboarding build).

    `line_items` is a list of {'description': str, 'amount': Decimal} dicts.
    Returns (customer, invoice) where invoice is already finalized — Stripe
    automatically emails the hosted invoice link to the customer.

    Metadata kind='onboarding_setup' is set on the invoice so the webhook
    handler can distinguish this from contract-flow deposit/final invoices.
    """
    _init()
    customer = stripe.Customer.create(
        email=email,
        name=name,
        metadata={
            'source': 'aspired_websites',
            'client_profile_id': str(client_profile_id),
        },
    )
    for item in line_items:
        stripe.InvoiceItem.create(
            customer=customer.id,
            amount=_cents(item['amount']),
            currency='usd',
            description=item['description'],
        )
    invoice = stripe.Invoice.create(
        customer=customer.id,
        collection_method='send_invoice',
        days_until_due=7,
        auto_advance=True,
        metadata={
            'kind': 'onboarding_setup',
            'client_profile_id': str(client_profile_id),
        },
    )
    invoice = stripe.Invoice.finalize_invoice(invoice.id)
    # finalize_invoice with collection_method='send_invoice' auto-emails
    # the hosted invoice link — no separate send_invoice() call required.
    return customer, invoice


def get_domain_tier(tld):
    """
    Return the ServiceTier that prices a domain in `tld`.

    Premium TLDs (currently just .law) use the 'domain-law' tier;
    everything else uses 'domain-standard'. The mapping lives in
    domains.models.tier_slug_for_tld so adding new premium TLDs is
    a one-line change there.
    """
    from billing.pricing_models import ServiceTier
    from domains.models import tier_slug_for_tld

    slug = tier_slug_for_tld(tld.lower().lstrip('.'))
    tier = ServiceTier.objects.filter(
        slug=slug, is_active=True, category='addon').first()
    if tier is None:
        raise ValueError(
            f'No active domain pricing tier "{slug}" — run seed_pricing.')
    if not tier.stripe_price_id:
        raise ValueError(
            f'Domain tier "{slug}" has no Stripe Price ID — run '
            f'`python manage.py sync_stripe_products`.')
    return tier


def create_domain_subscription(client, registration):
    """
    Create a Stripe Subscription for a domain registration.

    The subscription:
      - charges $75 (or $175 for .law) IMMEDIATELY on the customer's
        default card (no trial — they're paying for year 1 right now)
      - auto-renews in 365 days
      - is gated by invoice.upcoming (we cancel if the domain has
        been transferred out or the client cancelled their plan
        between renewals)

    The webhook for the initial invoice.paid is what marks the
    DomainRegistration row as billed; this function only kicks off
    the charge.

    Returns the Stripe Subscription object. Raises ValueError if the
    customer has no default payment method on file (the portal flow
    forces an add-card step before calling this).
    """
    _init()
    tier = get_domain_tier(registration.tld)
    customer = create_or_get_customer(client)

    default_pm = get_customer_default_payment_method(customer.id)
    if not default_pm:
        raise ValueError(
            'No default payment method on file. Add a card on the '
            'subscriptions page before registering a domain.')

    sub = stripe.Subscription.create(
        customer=customer.id,
        items=[{'price': tier.stripe_price_id}],
        metadata={
            'kind': 'domain',
            'client_profile_id': str(client.id),
            'domain_registration_id': str(registration.id),
            'domain_name': registration.domain_name,
            'tld': registration.tld,
        },
        # Stop the API call if the saved card declines — better than
        # an `incomplete` sub that creates a paid-zero placeholder.
        payment_behavior='error_if_incomplete',
    )
    registration.stripe_subscription_id = sub.id
    registration.pricing_tier_slug = tier.slug
    registration.save(update_fields=[
        'stripe_subscription_id', 'pricing_tier_slug', 'updated_at'])
    logger.info(
        'create_domain_subscription: client %s domain %s sub %s',
        client.pk, registration.domain_name, sub.id)
    return sub


def cancel_domain_subscription(registration, reason=''):
    """
    Cancel a domain Stripe Subscription at period end so the client
    keeps the domain through what they've already paid for. They get
    a transfer-out email immediately so they can move it elsewhere
    before the grace period ends.

    Returns the updated Stripe Subscription object or None if no sub
    on file.
    """
    _init()
    sub_id = registration.stripe_subscription_id
    if not sub_id:
        return None
    try:
        sub = stripe.Subscription.retrieve(sub_id)
    except Exception:
        registration.stripe_subscription_id = ''
        registration.save(update_fields=[
            'stripe_subscription_id', 'updated_at'])
        return None
    if getattr(sub, 'status', '') in ('canceled', 'incomplete_expired'):
        return sub
    updated = stripe.Subscription.modify(
        sub_id,
        cancel_at_period_end=True,
        metadata={**(getattr(sub, 'metadata', {}) or {}),
                  'cancel_reason': reason[:200] if reason else ''},
    )
    logger.info(
        'cancel_domain_subscription: domain %s sub %s '
        'cancel_at_period_end=True', registration.domain_name, sub_id)
    return updated


def resume_domain_subscription(registration):
    """
    Undo a pending cancel-at-period-end on a domain Stripe sub. Used
    by `resume_domain` when a client (or admin) changes their mind
    after starting the transfer-out flow.

    No-op if no sub on file (sandbox registrations) or if the sub
    isn't currently scheduled to cancel.
    """
    _init()
    sub_id = registration.stripe_subscription_id
    if not sub_id:
        return None
    try:
        sub = stripe.Subscription.retrieve(sub_id)
    except Exception:
        registration.stripe_subscription_id = ''
        registration.save(update_fields=[
            'stripe_subscription_id', 'updated_at'])
        return None
    if not getattr(sub, 'cancel_at_period_end', False):
        return sub                          # already not scheduled to cancel
    updated = stripe.Subscription.modify(
        sub_id, cancel_at_period_end=False)
    logger.info(
        'resume_domain_subscription: client %s domain %s '
        'cancel_at_period_end reset', registration.client_id,
        registration.domain_name)
    return updated


def refund_failed_domain_registration(stripe_subscription_id, reason=''):
    """
    Best-effort cleanup when a Stripe charge succeeded but the
    follow-up Namecheap registration FAILED. Cancels the
    subscription + refunds the most recent charge so the client
    isn't out money for a domain we couldn't register.

    Returns True on a successful refund, False otherwise (caller
    logs + alerts admin so it can be handled manually).
    """
    _init()
    if not stripe_subscription_id:
        return False
    try:
        # Cancel immediately so no future invoices generate.
        stripe.Subscription.cancel(
            stripe_subscription_id, invoice_now=False, prorate=False)
    except Exception:
        logger.exception(
            'refund_failed_domain_registration: subscription cancel '
            'failed for %s', stripe_subscription_id)

    # Find the most recent invoice on the sub and refund its PI.
    try:
        invs = stripe.Invoice.list(
            subscription=stripe_subscription_id, limit=1)
        invs_data = list(getattr(invs, 'data', None) or [])
        if not invs_data:
            return False
        invoice = invs_data[0]
        pi = getattr(invoice, 'payment_intent', None)
        if not pi:
            return False
        stripe.Refund.create(
            payment_intent=pi,
            reason='duplicate' if not reason else 'requested_by_customer',
            metadata={'note': reason[:200] if reason else ''},
        )
        return True
    except Exception:
        logger.exception(
            'refund_failed_domain_registration: refund failed for %s',
            stripe_subscription_id)
        return False


def issue_deposit_invoice(contract):
    """
    Best-effort deposit invoice send, called right after a contract is signed.
    Logs and returns None if Stripe is unconfigured — never breaks signing.
    """
    try:
        return create_deposit_invoice(contract.client, contract)
    except StripeNotConfigured:
        logger.warning(
            'Stripe not configured — deposit invoice for contract %s not sent.',
            contract.pk,
        )
    except Exception:
        logger.exception(
            'Failed to issue deposit invoice for contract %s', contract.pk,
        )
    return None
