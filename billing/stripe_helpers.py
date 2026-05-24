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
    """Return the client's Stripe Customer, creating + storing it if needed."""
    _init()
    if client.stripe_customer_id:
        try:
            customer = stripe.Customer.retrieve(client.stripe_customer_id)
            if not customer.get('deleted'):
                return customer
        except Exception:
            logger.warning(
                'Stripe customer %s not retrievable — creating a new one.',
                client.stripe_customer_id,
            )
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


def create_maintenance_subscription(client, plan_slug):
    """Create a recurring maintenance subscription for a ServiceTier slug."""
    from billing.pricing_models import ServiceTier

    _init()
    tier = ServiceTier.objects.filter(slug=plan_slug, is_active=True).first()
    if tier is None:
        raise ValueError(f'No active pricing tier with slug "{plan_slug}".')
    if not tier.stripe_price_id:
        raise ValueError(
            f"No Stripe Price ID set for '{tier.name}'. Go to admin dashboard "
            f"→ Pricing → edit this tier and add the Stripe Price ID."
        )
    customer = create_or_get_customer(client)
    subscription = stripe.Subscription.create(
        customer=customer.id,
        items=[{'price': tier.stripe_price_id}],
        metadata={'client_profile_id': str(client.id), 'plan': tier.slug},
    )
    client.stripe_subscription_id = subscription.id
    client.save(update_fields=['stripe_subscription_id', 'updated_at'])
    return subscription


def cancel_maintenance_subscription(client):
    """Cancel the client's maintenance subscription at period end."""
    _init()
    if client.stripe_subscription_id:
        stripe.Subscription.modify(
            client.stripe_subscription_id, cancel_at_period_end=True,
        )
    client.maintenance_active = False
    client.save(update_fields=['maintenance_active', 'updated_at'])


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

    Returns the Stripe Subscription object. Idempotent: if the client
    already has a hosting subscription, returns the existing one
    rather than double-billing.
    """
    _init()
    if client.stripe_hosting_subscription_id:
        try:
            existing = stripe.Subscription.retrieve(
                client.stripe_hosting_subscription_id)
            if existing.get('status') not in ('canceled', 'incomplete_expired'):
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

    kwargs = {
        'customer': client.stripe_customer_id,
        'items': [{'price': get_hosting_price_id()}],
        'trial_period_days': 365,
        'metadata': {
            'kind': 'hosting',
            'client_profile_id': str(client.id),
        },
        # When the trial ends and the first invoice is created,
        # Stripe should use the customer's default payment method
        # automatically (not error out).
        'payment_behavior': 'allow_incomplete',
        # Generate an invoice ~3 days before each renewal so our
        # invoice.upcoming webhook fires the Droplet check in time
        # to cancel without an accidental charge.
        # (3 days is Stripe's default for upcoming-invoice webhook;
        # no setting needed here.)
    }
    if default_payment_method_id:
        kwargs['default_payment_method'] = default_payment_method_id

    sub = stripe.Subscription.create(**kwargs)
    client.stripe_hosting_subscription_id = sub.id
    client.save(update_fields=[
        'stripe_hosting_subscription_id', 'updated_at'])
    logger.info(
        'create_hosting_subscription: client %s subscribed (sub %s, '
        'trial until %s)', client.pk, sub.id,
        sub.get('trial_end'))
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
