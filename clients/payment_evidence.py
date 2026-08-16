"""
Does a "fully paid" claim have anything in the ledger behind it?

``payment_status = 'fully_paid'`` releases the launch gate: it is the flag
that says the site may go live and the balance is settled. It is also a
plain CharField that any admin screen, webhook, or backfill can set. When
it is set without a corresponding payment record, the system believes money
arrived that it has no record of receiving.

The 2026-08-16 real-data rehearsal found exactly one such row: a client
marked ``fully_paid`` with no PaymentRecord, no deposit or final timestamp,
no Stripe invoice, and no signed contract, still sitting at stage
``design``. That is not necessarily wrong — an owner-operator gets paid by
cheque and Zelle, and marks the account by hand — but it is unverified, and
the launch gate must not treat an unverified claim as settled.

This module does NOT invent evidence. It never writes a PaymentRecord,
timestamp, contract, or Stripe object to make the books balance. It reports
what is missing and makes the operator confirm.
"""


def ledger_evidence_for(website):
    """Return the list of ledger facts backing this Website's paid status.

    Each entry is a short human string naming a concrete artifact. An empty
    list means nothing in the system corroborates the payment.
    """
    evidence = []

    # An operator confirming a payment that arrived outside Stripe is
    # real evidence — a named person, on a date, attesting to it. It is
    # listed first because it is the answer to "why does this site say
    # fully paid with nothing behind it?".
    if website.payment_verified_at:
        who = website.payment_verified_by or 'operator'
        evidence.append(
            f'verified by {who} on '
            f'{website.payment_verified_at:%Y-%m-%d}')

    if website.final_paid_at:
        evidence.append(
            f'final_paid_at {website.final_paid_at:%Y-%m-%d}')
    if website.deposit_paid_at:
        evidence.append(
            f'deposit_paid_at {website.deposit_paid_at:%Y-%m-%d}')
    if website.stripe_invoice_id:
        evidence.append(f'stripe_invoice {website.stripe_invoice_id}')

    payments = list(website.payment_records.all()[:5])
    if not payments and website.account_id:
        payments = list(website.account.payment_records.all()[:5])
    for payment in payments:
        evidence.append(
            f'PaymentRecord {payment.kind} {payment.amount}')

    if website.account_id:
        from clients.models import OnboardingInvoice
        paid_invoice = OnboardingInvoice.objects.filter(
            account_new=website.account, status='paid').first()
        if paid_invoice is not None:
            evidence.append(
                f'paid OnboardingInvoice {paid_invoice.total_amount}')

        signed = website.account.contracts.filter(
            signed=True, build_price__isnull=False).first()
        if signed is not None:
            evidence.append(
                f'signed contract {signed.build_price}')

    return evidence


def is_fully_paid_without_evidence(website):
    """True when the site claims settled payment the ledger cannot support."""
    if website.payment_status != 'fully_paid':
        return False
    return not ledger_evidence_for(website)


def unverified_payment_message(website):
    """The operator-facing explanation for a blocked launch."""
    return (
        f'{website.name} is marked fully_paid but nothing in the ledger '
        'supports it — no payment record, no deposit or final timestamp, '
        'no Stripe invoice, no signed contract with a build price. '
        'If the payment arrived outside Stripe, record that once and it '
        'stops being questioned:\n'
        f'    python manage.py verify_website_payment {website.slug} '
        '--by "Your Name" --note "how it was paid" --apply')
