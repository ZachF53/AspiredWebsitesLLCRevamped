"""
Minutes: accrual, periods, rounding, and the 80%/100% warnings.

Every number a client is ever charged for passes through this module, so
the rules are stated once here rather than inferred at each call site.

## Rounding

GHL sends ``duration`` in SECONDS. Seconds are summed across the whole
billing period and converted to minutes exactly once, at the end:

    overage_minutes = ceil(max(0, total_seconds / 60 - included))

Rounding each call up to a whole minute instead would add roughly half a
minute of phantom usage per call — about 20 minutes on a 40-call month,
or $5 of overage the client did not use. That is the kind of error that
is individually small, systematically in our favour, and impossible to
defend in a dispute.

## Trial calls never bill

``trialCall=true`` marks GHL's own test calls. The operator makes these
while building a client's agent. They are stored (so the operator can see
them) and excluded from ``billable_seconds`` everywhere.

## Period boundaries

A period mirrors the Stripe subscription's billing cycle, so the overage
we attach to an invoice covers exactly the span that invoice closes. When
there is no subscription to read a cycle from — a comped account — the
period falls back to calendar months, because a comped client still needs
a usage gauge even though nothing will be invoiced.
"""

import logging
import math
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

logger = logging.getLogger(__name__)


def included_minutes():
    return int(getattr(settings, 'VOICE_INCLUDED_MINUTES', 300) or 300)


def overage_rate():
    raw = getattr(settings, 'VOICE_OVERAGE_RATE', Decimal('0.25'))
    return raw if isinstance(raw, Decimal) else Decimal(str(raw))


def _calendar_period(moment):
    """First of the month to first of the next, in the project timezone."""
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0, day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def period_bounds_for(program, moment=None):
    """The billing window `moment` falls in for this program.

    Prefers the Stripe subscription's current period so the overage we
    bill lines up with the invoice that closes it. Falls back to calendar
    months for comped accounts and for any case where Stripe is
    unreachable — a usage gauge that is a few days out of phase is much
    better than no gauge.
    """
    from clients.contract_options import VOICE_RECEPTIONIST_SLUG

    moment = moment or timezone.now()
    plan = (program.account.maintenance_plans
            .filter(tier_slug=VOICE_RECEPTIONIST_SLUG)
            .exclude(stripe_subscription_id='')
            .order_by('-started_at')
            .first())
    if plan is None or not plan.stripe_subscription_id:
        return _calendar_period(moment)
    if not settings.STRIPE_SECRET_KEY:
        return _calendar_period(moment)
    try:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        sub = stripe.Subscription.retrieve(plan.stripe_subscription_id)
        # django.utils.timezone.utc was removed in Django 5 — use the
        # stdlib UTC directly rather than the old Django alias.
        start = datetime.fromtimestamp(
            sub['current_period_start'], tz=dt_timezone.utc)
        end = datetime.fromtimestamp(
            sub['current_period_end'], tz=dt_timezone.utc)
        return start, end
    except Exception:
        logger.warning(
            'voice.usage: could not read Stripe cycle for program %s; '
            'falling back to calendar month', program.pk, exc_info=True)
        return _calendar_period(moment)


def period_for(program, moment=None, *, create=True):
    """The VoiceUsagePeriod covering `moment`, created on demand."""
    from voice.models import VoiceUsagePeriod

    moment = moment or timezone.now()
    existing = (program.usage_periods
                .filter(period_start__lte=moment, period_end__gt=moment)
                .first())
    if existing is not None or not create:
        return existing

    start, end = period_bounds_for(program, moment)
    period, _ = VoiceUsagePeriod.objects.get_or_create(
        program=program, period_start=start,
        defaults={
            'period_end': end,
            'included_minutes': included_minutes(),
            'overage_rate': overage_rate(),
        },
    )
    return period


def recompute(period):
    """Re-derive this period's minutes from its call records.

    Authoritative, and deliberately a full recount rather than an
    increment: the nightly reconciliation inserts calls the webhook
    missed, and an increment-only design would never pick those up.
    Cheap enough to run on every call because of the
    (program, trial_call, started_at) index.
    """
    total = (period.program.calls
             .filter(trial_call=False,
                     started_at__gte=period.period_start,
                     started_at__lt=period.period_end)
             .aggregate(s=Sum('duration_seconds'))['s'] or 0)

    period.billable_seconds = total
    minutes = math.ceil(total / 60) if total else 0
    over = max(0, minutes - period.included_minutes)
    period.overage_minutes = over
    period.overage_amount = (period.overage_rate * over).quantize(
        Decimal('0.01'))
    period.save(update_fields=[
        'billable_seconds', 'overage_minutes', 'overage_amount',
        'updated_at',
    ])
    return period


def accrue(call):
    """Fold one call into its period and fire any crossed threshold.

    Returns the period, or None when the call cannot be attributed (no
    program) — an unattributed call is still stored, it just has no
    allowance to count against.
    """
    if call.program_id is None:
        return None
    period = period_for(call.program, call.started_at)
    if period is None:
        return None
    recompute(period)
    check_thresholds(period)
    return period


def check_thresholds(period):
    """Warn once at 80% and once at 100%.

    Idempotent via the stamped timestamps, which matters more than it
    looks: a busy afternoon can push a client from 70% to 130% across a
    dozen calls, and each one runs this.
    """
    if period.program is None:
        return
    pct = period.percent_used
    now = timezone.now()

    if pct >= 100 and period.warned_100_at is None:
        period.warned_100_at = now
        period.save(update_fields=['warned_100_at', 'updated_at'])
        _notify(period, level=100)
    elif pct >= 80 and period.warned_80_at is None:
        period.warned_80_at = now
        period.save(update_fields=['warned_80_at', 'updated_at'])
        _notify(period, level=80)


def _notify(period, *, level):
    """Email the client. Never raises — a mail failure must not roll back
    a stored call or re-fire the warning on the next one."""
    try:
        from voice.emails import send_usage_warning
        send_usage_warning(period, level=level)
    except Exception:
        logger.exception(
            'voice.usage: %s%% warning email failed for period %s',
            level, period.pk)


def close_period(period):
    """Final recount before invoicing. Returns the period."""
    return recompute(period)


def usage_summary(program):
    """What the portal gauge and the admin detail page both need."""
    period = period_for(program, create=False) if program else None
    if period is None:
        return {
            'has_period': False,
            'minutes_used': 0,
            'included_minutes': included_minutes(),
            'minutes_remaining': included_minutes(),
            'percent_used': 0,
            'is_over': False,
            'overage_minutes': 0,
            'overage_amount': Decimal('0.00'),
            'overage_rate': overage_rate(),
            'period': None,
        }
    return {
        'has_period': True,
        'minutes_used': period.minutes_used,
        'included_minutes': period.included_minutes,
        'minutes_remaining': period.minutes_remaining,
        'percent_used': period.percent_used,
        'is_over': period.is_over,
        'overage_minutes': period.overage_minutes,
        'overage_amount': period.overage_amount,
        'overage_rate': period.overage_rate,
        'period': period,
    }
