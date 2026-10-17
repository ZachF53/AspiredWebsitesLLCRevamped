"""
Billing overage minutes onto the client's next Stripe invoice.

## Why an InvoiceItem and not metered billing

Stripe's own docs now say Billing Meters are "appropriate only if you are
already billing customers via Billing Meters today" and steer new
usage-based integrations to Metronome. Metronome is a whole third-party
ingest platform; adding one to bill a 25-cent overage on one add-on would
be absurd. And we already hold the authoritative minute count in our own
database, because the portal gauge and the admin detail page both need
it — so there is nothing for a meter to tell us that we do not know.

So: a one-off ``InvoiceItem`` tied to the subscription.

## The timing, which is the only subtle part

Stripe bills the flat $97 **in advance** and we bill minutes **in
arrears**. At a period boundary Stripe creates the next invoice and holds
it as a draft for roughly an hour before finalising. We hook
``invoice.created``, compute the overage for the period that just ENDED,
and attach the item with an explicit ``invoice=`` so it lands on that
draft.

Passing ``subscription=`` without ``invoice=`` would make it a *pending*
item, which Stripe would sweep onto the FOLLOWING invoice — a month late,
and silently. The explicit invoice id is what keeps the charge on the
same bill as the period it belongs to.

## Never billing twice

``VoiceUsagePeriod.stripe_invoice_item_id`` is the guard. Once set, the
period is done, whatever redeliveries or re-runs happen afterwards.
"""

import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _stripe():
    if not settings.STRIPE_SECRET_KEY:
        return None
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def period_to_bill(program, invoice_period_start):
    """The usage period an invoice created at `invoice_period_start` closes.

    The invoice covers the upcoming month; the minutes it should carry
    are the ones used in the month that just ended, so we look for the
    period whose end lands at or just before this boundary. A one-hour
    tolerance absorbs the gap between Stripe's cycle timestamp and the
    period bounds we stored from it.
    """
    from datetime import timedelta
    window_start = invoice_period_start - timedelta(hours=1)
    return (program.usage_periods
            .filter(period_end__lte=invoice_period_start + timedelta(hours=1),
                    period_end__gte=window_start - timedelta(days=45))
            .filter(stripe_invoice_item_id='')
            .order_by('-period_end')
            .first())


def attach_overage(period, *, invoice_id=None, subscription_id=''):
    """Put this period's overage on an invoice. Returns the item id or ''.

    Idempotent and safe to call on a period with nothing owed — both of
    those cases return '' rather than raising, because this is driven by
    a webhook and the common case is genuinely "nothing to do".
    """
    from voice.usage import close_period

    if period is None:
        return ''
    if period.stripe_invoice_item_id:
        logger.info('voice.billing: period %s already billed as %s',
                    period.pk, period.stripe_invoice_item_id)
        return period.stripe_invoice_item_id

    # Final recount. The nightly sweep may have added calls the webhook
    # missed, and this is the last moment the number can still change.
    close_period(period)

    if period.overage_minutes <= 0:
        # Stamp nothing: a period with no overage must stay eligible for
        # a recount if a late call arrives before the invoice finalises.
        return ''

    stripe = _stripe()
    if stripe is None:
        logger.warning(
            'voice.billing: Stripe not configured; %s overage minutes on '
            'period %s not billed', period.overage_minutes, period.pk)
        return ''

    program = period.program
    customer_id = (program.account.stripe_customer_id or '').strip()
    if not customer_id:
        logger.warning(
            'voice.billing: account %s has no Stripe customer; %s overage '
            'minutes not billed', program.account_id, period.overage_minutes)
        return ''

    rate_cents = int(period.overage_rate * 100)
    label = (f'AI Receptionist — {period.overage_minutes} extra talk '
             f'minutes at {rate_cents}c '
             f'({period.period_start:%b %d} to {period.period_end:%b %d})')

    kwargs = {
        'customer': customer_id,
        'currency': 'usd',
        # unit_amount_decimal keeps a fractional rate exact. At 25c it is
        # equivalent to unit_amount, but a future rate of 12.5c would
        # silently truncate to 12c through the integer field.
        'unit_amount_decimal': str(period.overage_rate * 100),
        'quantity': period.overage_minutes,
        'description': label,
        'metadata': {
            'voice_usage_period': str(period.pk),
            'voice_program': str(program.pk),
            'overage_minutes': str(period.overage_minutes),
        },
    }
    # The explicit invoice is what keeps this on the bill that closes the
    # period. Without it Stripe defers the item to the next invoice.
    if invoice_id:
        kwargs['invoice'] = invoice_id
    elif subscription_id:
        kwargs['subscription'] = subscription_id

    try:
        item = stripe.InvoiceItem.create(**kwargs)
    except Exception:
        logger.exception(
            'voice.billing: InvoiceItem failed for period %s', period.pk)
        return ''

    period.stripe_invoice_item_id = item.id
    period.stripe_invoice_id = invoice_id or ''
    period.billed_at = timezone.now()
    period.save(update_fields=[
        'stripe_invoice_item_id', 'stripe_invoice_id', 'billed_at',
        'updated_at',
    ])
    logger.info('voice.billing: billed %s min ($%s) on period %s as %s',
                period.overage_minutes, period.overage_amount, period.pk,
                item.id)
    return item.id


def handle_invoice_created(invoice):
    """``invoice.created`` for a receptionist subscription.

    Returns the InvoiceItem id, or '' when this invoice is not ours or
    there is nothing owed. Called from ``billing/webhooks.py``; must never
    raise, because a failure here would make Stripe redeliver an event
    whose side effect is a charge.
    """
    from clients.contract_options import VOICE_RECEPTIONIST_SLUG
    from clients.service_models import MaintenancePlan
    from voice.entitlement import program_for

    subscription_id = (invoice.get('subscription') or '')
    if not subscription_id:
        return ''

    plan = (MaintenancePlan.objects
            .filter(stripe_subscription_id=subscription_id,
                    tier_slug=VOICE_RECEPTIONIST_SLUG)
            .select_related('account')
            .first())
    if plan is None:
        return ''

    program = program_for(plan.account)
    if program is None:
        logger.warning(
            'voice.billing: subscription %s has no program; cannot bill '
            'overage', subscription_id)
        return ''

    period_start = invoice.get('period_start')
    if not period_start:
        return ''
    from datetime import datetime, timezone as dt_timezone
    boundary = datetime.fromtimestamp(period_start, tz=dt_timezone.utc)

    period = period_to_bill(program, boundary)
    if period is None:
        return ''

    return attach_overage(
        period, invoice_id=invoice.get('id'),
        subscription_id=subscription_id)
