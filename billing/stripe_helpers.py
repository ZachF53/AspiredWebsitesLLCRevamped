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
