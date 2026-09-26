"""
30-day guarantee with 25% retention (owner decision, Sept 2026).

Within 30 days of signing, a client may cancel their agreement. Aspired
Websites KEEPS 25% of everything the client has paid under that agreement
and REFUNDS the other 75%, and every subscription / installment schedule
the agreement created is cancelled immediately — no further charges.

    guarantee_status(website)            read-only panel data
    execute_guarantee_refund(website, …) does it (Stripe refunds + cancels)

"Paid under the agreement" = PaymentRecord ledger rows for this website
(or the contract's account when the contract has no website) with a
build / plan kind, paid on or after the contract's signing time.

The refund is split proportionally: each payment is refunded 75% of
itself (plus at most one cent of rounding), so no single charge is ever
refunded more than it was. Idempotency: GuaranteeRefund is one-to-one
with the contract; a completed or in-flight row refuses a second run, and
a failed run retries only the payments not yet refunded (each Stripe
refund also carries an idempotency key).

Only current-terms contracts (Contract.payment_option set) qualify: a
legacy 50/50 contract promised a different guarantee in its own text.
"""

import logging
import math
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from clients.services import GuardError

logger = logging.getLogger(__name__)

REFUND_PERCENT = 75
GUARANTEE_KINDS = ('deposit', 'final', 'build', 'installment',
                   'maintenance', 'hosting')


def _cents(amount):
    return int((Decimal(amount) * 100).to_integral_value(ROUND_HALF_UP))


def _dollars(cents):
    return (Decimal(cents) / 100).quantize(Decimal('0.01'))


def guarantee_contract(website):
    """The agreement the guarantee applies to: the site's most recent
    signed, current-terms contract."""
    from clients.models import Contract

    return (Contract.objects
            .filter(website_new=website, signed=True,
                    signed_at__isnull=False)
            .exclude(payment_option='')
            .order_by('-signed_at').first())


def payments_under(contract):
    """Ledger rows paid under this agreement (positive, since signing)."""
    from clients.models import PaymentRecord

    qs = PaymentRecord.objects.filter(
        kind__in=GUARANTEE_KINDS, amount__gt=0, status='paid',
        paid_at__gte=contract.signed_at)
    if contract.website_new_id:
        qs = qs.filter(website_id=contract.website_new_id)
    elif contract.account_id:
        qs = qs.filter(account_id=contract.account_id, website__isnull=True)
    else:
        return PaymentRecord.objects.none()
    return qs.order_by('paid_at', 'created_at')


def split_refund(records):
    """(total_cents, refund_cents, [(record, cents)]) — 75% of the total,
    rounded half-up, allocated proportionally and never more than any
    single payment."""
    amounts = [(r, _cents(r.amount)) for r in records]
    total = sum(c for _, c in amounts)
    refund_total = (total * REFUND_PERCENT + 50) // 100
    alloc = [(r, c * REFUND_PERCENT // 100, c) for r, c in amounts]
    remainder = refund_total - sum(a for _, a, _ in alloc)
    # Hand out the rounding cents, largest payment first, where there is
    # room left on that charge.
    order = sorted(range(len(alloc)), key=lambda i: -alloc[i][2])
    for i in order:
        if remainder <= 0:
            break
        r, a, c = alloc[i]
        if a < c:
            alloc[i] = (r, a + 1, c)
            remainder -= 1
    return total, refund_total, [(r, a) for r, a, _ in alloc]


def guarantee_status(website, now=None):
    """Everything the admin panel shows. Never touches Stripe."""
    from clients.contract_options import GUARANTEE_DAYS

    now = now or timezone.now()
    contract = guarantee_contract(website)
    status = {
        'contract': contract, 'eligible': False, 'reason': '',
        'deadline': None, 'days_remaining': 0,
        'total_paid': Decimal('0.00'), 'refund_amount': Decimal('0.00'),
        'retained_amount': Decimal('0.00'), 'payments': [],
        'existing': None,
    }
    if contract is None:
        status['reason'] = ('No signed agreement on current terms — the '
                            '30-day guarantee does not apply.')
        return status
    try:
        existing = contract.guarantee_refund
    except Exception:  # noqa: BLE001 — reverse 1:1 raises when absent
        existing = None
    status['existing'] = existing

    deadline = contract.signed_at + timedelta(days=GUARANTEE_DAYS)
    status['deadline'] = deadline
    seconds_left = (deadline - now).total_seconds()
    status['days_remaining'] = (
        max(0, math.ceil(seconds_left / 86400)) if seconds_left > 0 else 0)

    records = list(payments_under(contract))
    total, refund, _ = split_refund(records)
    status.update({
        'payments': records,
        'total_paid': _dollars(total),
        'refund_amount': _dollars(refund),
        'retained_amount': _dollars(total - refund),
    })

    if existing is not None and existing.status == 'completed':
        status['reason'] = (
            f'Already refunded ${existing.refund_amount:,.2f} on '
            f'{existing.completed_at:%b %d, %Y}.')
    elif existing is not None and existing.status == 'processing':
        status['reason'] = 'A guarantee refund is already in progress.'
    elif now > deadline:
        status['reason'] = (f'Outside the {GUARANTEE_DAYS}-day window — '
                            f'it closed {deadline:%b %d, %Y}.')
    else:
        status['eligible'] = True
    return status


# ── Stripe ──────────────────────────────────────────────────────────────────

def _g(obj, key):
    if obj is None or isinstance(obj, str):
        return None
    try:
        return obj[key] if key in obj else None
    except Exception:  # noqa: BLE001
        return getattr(obj, key, None)


def _refund_target(stripe, stripe_id):
    """{'payment_intent': ...} or {'charge': ...} for a ledger stripe_id
    (a PaymentIntent id for one-time payments, an Invoice id for
    subscription charges)."""
    if stripe_id.startswith('pi_'):
        return {'payment_intent': stripe_id}
    if stripe_id.startswith('ch_'):
        return {'charge': stripe_id}
    if stripe_id.startswith('in_'):
        # dahlia API: invoices no longer carry payment_intent; the
        # payment lives on InvoicePayment.
        payments = stripe.InvoicePayment.list(
            invoice=stripe_id, status='paid', limit=10)
        for p in _g(payments, 'data') or []:
            pay = _g(p, 'payment')
            pi = _g(pay, 'payment_intent')
            if pi:
                return {'payment_intent': pi if isinstance(pi, str)
                        else _g(pi, 'id')}
            ch = _g(pay, 'charge')
            if ch:
                return {'charge': ch if isinstance(ch, str)
                        else _g(ch, 'id')}
    raise GuardError(f'Could not find the Stripe charge behind {stripe_id} '
                     '— refund it by hand in the Stripe dashboard.')


def _subscriptions_to_cancel(contract, website):
    objs = contract.stripe_objects or {}
    schedules = [objs.get('installment_schedule')]
    subs = [objs.get('installment_subscription'),
            objs.get('plan_subscription'),
            objs.get('hosting_subscription')]
    if website is not None:
        schedules.append(website.stripe_build_installment_schedule_id)
        subs.append(website.stripe_build_installment_subscription_id)
    return ([s for s in dict.fromkeys(schedules) if s],
            [s for s in dict.fromkeys(subs) if s])


def _cancel_everything(stripe, contract, website):
    """Cancel the agreement's schedules and subscriptions NOW (not at
    period end). Returns (cancelled_ids, errors)."""
    schedules, subs = _subscriptions_to_cancel(contract, website)
    cancelled, errors = [], []
    for sched_id in schedules:
        try:
            sched = stripe.SubscriptionSchedule.retrieve(sched_id)
            if _g(sched, 'status') in ('not_started', 'active'):
                stripe.SubscriptionSchedule.cancel(sched_id)
            cancelled.append(sched_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception('guarantee: schedule %s cancel failed',
                             sched_id)
            errors.append(f'schedule {sched_id}: {exc}')
    for sub_id in subs:
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            if _g(sub, 'status') not in ('canceled', 'incomplete_expired'):
                stripe.Subscription.cancel(sub_id)
            cancelled.append(sub_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception('guarantee: subscription %s cancel failed',
                             sub_id)
            errors.append(f'subscription {sub_id}: {exc}')
    return cancelled, errors


def _clear_local_subscriptions(contract, website, cancelled):
    from clients.service_models import MaintenancePlan

    now = timezone.now()
    ids = set(cancelled)
    for plan in MaintenancePlan.objects.filter(
            stripe_subscription_id__in=ids).exclude(status='ended'):
        plan.status = 'ended'
        plan.ended_at = now
        plan.save(update_fields=['status', 'ended_at', 'updated_at'])
    if website is None:
        return
    fields = []
    for field in ('stripe_build_installment_subscription_id',
                  'stripe_build_installment_schedule_id',
                  'stripe_maintenance_subscription_id',
                  'stripe_hosting_subscription_id'):
        if getattr(website, field) and getattr(website, field) in ids:
            setattr(website, field, '')
            fields.append(field)
    if 'stripe_maintenance_subscription_id' in fields:
        website.maintenance_active = False
        website.maintenance_cancelled_at = now
        fields += ['maintenance_active', 'maintenance_cancelled_at']
    if fields:
        website.save(update_fields=fields + ['updated_at'])


def execute_guarantee_refund(website, *, requested_by, reason=''):
    """Refund 75% of what was paid under the agreement, keep 25%, and
    cancel every remaining payment. Returns the GuaranteeRefund.

    Raises GuardError (operator-facing) when not eligible, already done,
    or when Stripe fails — in which case the row is left 'failed' with
    what did succeed recorded, and a retry picks up where it stopped.
    """
    from billing.stripe_helpers import StripeNotConfigured, _init
    from clients.models import Contract, GuaranteeRefund, PaymentRecord

    status = guarantee_status(website)
    if not status['eligible']:
        raise GuardError(status['reason'] or 'Not eligible.')
    contract = status['contract']

    with transaction.atomic():
        Contract.objects.select_for_update().get(pk=contract.pk)
        row, created = GuaranteeRefund.objects.get_or_create(
            contract=contract,
            defaults={
                'account': contract.account, 'website': website,
                'total_paid': status['total_paid'],
                'refund_amount': status['refund_amount'],
                'retained_amount': status['retained_amount'],
                'requested_by': requested_by or '', 'reason': reason or '',
                'status': 'processing'})
        if not created:
            if row.status != 'failed':
                raise GuardError('This agreement has already been refunded '
                                 'under the guarantee (or a refund is in '
                                 'progress).')
            row.status = 'processing'
            row.error = ''
            row.save(update_fields=['status', 'error', 'updated_at'])

    try:
        _init()
        import stripe
    except StripeNotConfigured:
        row.status = 'failed'
        row.error = 'Stripe is not configured.'
        row.save(update_fields=['status', 'error', 'updated_at'])
        raise GuardError('Stripe is not configured — nothing was refunded.')

    done = {str(r.get('payment_record_id')) for r in (row.refunds or [])}
    _, _, allocations = split_refund(status['payments'])
    errors = []
    for record, cents in allocations:
        if cents <= 0 or str(record.id) in done:
            continue
        try:
            target = _refund_target(stripe, record.stripe_id)
            refund = stripe.Refund.create(
                amount=cents,
                metadata={'reason': '30-day guarantee (75% refund)',
                          'contract_id': str(contract.id),
                          'payment_record_id': str(record.id)},
                idempotency_key=f'guarantee-{row.id}-{record.id}',
                **target)
        except Exception as exc:  # noqa: BLE001
            logger.exception('guarantee: refund of %s failed',
                             record.stripe_id)
            errors.append(f'{record.stripe_id}: {exc}')
            continue
        refund_id = _g(refund, 'id') or getattr(refund, 'id', '')
        row.refunds = list(row.refunds or []) + [{
            'payment_record_id': str(record.id),
            'stripe_id': record.stripe_id,
            'amount': str(_dollars(cents)),
            'refund_id': refund_id,
        }]
        row.save(update_fields=['refunds', 'updated_at'])
        PaymentRecord.objects.get_or_create(
            stripe_id=refund_id,
            defaults={
                'client': record.client, 'account': record.account,
                'website': record.website, 'kind': 'refund',
                'amount': -_dollars(cents), 'status': 'refunded',
                'description': (f'30-day guarantee refund (75%) of '
                                f'{record.get_kind_display().lower()}'),
                'paid_at': timezone.now()})

    cancelled, cancel_errors = _cancel_everything(stripe, contract, website)
    _clear_local_subscriptions(contract, website, cancelled)
    row.cancelled_subscriptions = sorted(
        set(row.cancelled_subscriptions or []) | set(cancelled))
    errors += cancel_errors

    if errors:
        row.status = 'failed'
        row.error = '\n'.join(errors)[:5000]
        row.save(update_fields=['status', 'error', 'cancelled_subscriptions',
                                'updated_at'])
        raise GuardError(
            'The guarantee refund only partly completed — see the error on '
            'the record and retry. What succeeded is recorded and will not '
            'be repeated.')

    row.status = 'completed'
    row.completed_at = timezone.now()
    row.save(update_fields=['status', 'completed_at',
                            'cancelled_subscriptions', 'updated_at'])
    return row
