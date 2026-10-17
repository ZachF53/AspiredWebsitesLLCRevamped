"""
Nightly reconciliation of billable minutes against GHL's call logs.

The webhook is the fast path. This is the authority.

## Why both

The webhook has to be fast and has to return 200, so a transient failure
on our side (deploy, DB blip, a mapping bug) loses the event — GHL retries
non-2xx, but we answer 200 as soon as the row is stored, and anything that
fails before that is simply gone. For a stats page that is a slightly
wrong chart. For an invoice it is revenue we never bill, or minutes a
client is charged for twice if someone "fixes" it by replaying.

So every night we ask GHL what it thinks happened over the last two days,
insert anything missing, and recompute the affected periods from scratch.
``recompute`` is a full recount rather than an increment precisely so this
can work.

## Why a daily beat sweep and not a scheduled task per call

Hard-won project rule: never schedule Celery work days ahead. The broker's
visibility timeout makes long countdowns unreliable and a redelivery storm
is the failure mode. ``billing/dunning.py`` is the reference pattern — a
daily sweep that re-derives what is due from the database instead of
trusting a timer set in the past.

## Two days, not one

Overlap is deliberate. A call that lands near midnight, a GHL log that
appears late, or a sweep that fails once and runs the next night all get
picked up. Re-inserting is harmless because ``ghl_call_id`` is unique and
the recount is idempotent.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 2


def _upsert_from_log(program, log):
    """Insert one GHL call-log row if we do not already have it.

    Returns True when a row was created. Mapping lives here rather than
    in ``ghl_client`` so the webhook's field mapping and this one sit in
    the same module and cannot drift apart unnoticed.
    """
    from voice.ghl_webhook import (
        ACTION_BOOKING, ACTION_TRANSFER, _action_names, _as_bool, _first,
    )
    from voice.models import VoiceCallRecord

    call_id = _first(log, 'id', 'callId', 'call_id', 'messageId')
    if not call_id:
        return False
    call_id = str(call_id)[:255]
    if VoiceCallRecord.objects.filter(ghl_call_id=call_id).exists():
        return False

    started_at = parse_datetime(
        str(_first(log, 'createdAt', 'created_at', 'startedAt') or ''))
    if started_at is None:
        return False
    if timezone.is_naive(started_at):
        started_at = timezone.make_aware(started_at)

    try:
        duration = max(0, int(float(_first(log, 'duration') or 0)))
    except (TypeError, ValueError):
        duration = 0

    actions = _action_names(log)
    after_hours = False
    try:
        from voice.schedule import is_after_hours
        after_hours = is_after_hours(program, started_at)
    except Exception:
        logger.exception('voice.tasks: after-hours check failed')

    VoiceCallRecord.objects.create(
        program=program,
        ghl_call_id=call_id,
        ghl_location_id=(program.ghl_location_id or '')[:64],
        ghl_agent_id=str(_first(log, 'agentId', 'agent_id') or '')[:64],
        started_at=started_at,
        duration_seconds=duration,
        from_number=str(
            _first(log, 'fromNumber', 'from_number', 'from') or '')[:20],
        # callType=LIVE already excludes trial rows, but trust the field
        # over the query parameter if it is present and says otherwise.
        trial_call=_as_bool(_first(log, 'trialCall', 'trial_call')),
        transferred=ACTION_TRANSFER in actions,
        booked_appointment=ACTION_BOOKING in actions,
        after_hours=after_hours,
        summary=str(_first(log, 'summary') or ''),
        transcript=str(_first(log, 'transcript') or ''),
        extracted_data=log.get('extractedData') or {},
        executed_actions=log.get('executedCallActions') or [],
        payload=log,
    )
    return True


def reconcile_program(program, *, days=LOOKBACK_DAYS):
    """Pull recent logs for one program and recount its periods.

    Returns ``(inserted, periods_touched)``. Swallows per-program errors
    so one misconfigured client cannot stop the sweep for everyone.
    """
    from voice.ghl_client import (
        VoiceGHLError, VoiceGHLNotConfigured, fetch_call_logs,
    )
    from voice.usage import period_for, recompute

    end = timezone.now()
    start = end - timedelta(days=days)

    try:
        logs = fetch_call_logs(program, start=start, end=end)
    except VoiceGHLNotConfigured:
        # Expected for a program the operator has not finished wiring.
        return 0, 0
    except VoiceGHLError:
        logger.warning('voice.tasks: call-log fetch failed for program %s',
                       program.pk, exc_info=True)
        return 0, 0

    inserted = 0
    for log in logs:
        try:
            if _upsert_from_log(program, log):
                inserted += 1
        except Exception:
            logger.exception(
                'voice.tasks: could not store a log row for program %s',
                program.pk)

    # Recount every period the window overlaps — a window spanning a
    # period boundary affects two, and recounting only the current one
    # would leave the closed period understated right when it is about
    # to be invoiced.
    touched = 0
    periods = set()
    for moment in (start, end):
        period = period_for(program, moment, create=False)
        if period is not None:
            periods.add(period.pk)
    for pk in periods:
        from voice.models import VoiceUsagePeriod
        period = VoiceUsagePeriod.objects.filter(pk=pk).first()
        if period is None:
            continue
        recompute(period)
        period.reconciled_at = timezone.now()
        period.save(update_fields=['reconciled_at', 'updated_at'])
        touched += 1

    return inserted, touched


@shared_task
def reconcile_voice_usage(days=LOOKBACK_DAYS):
    """Daily: reconcile every live program's minutes against GHL."""
    from voice.models import VoiceProgram

    programs = VoiceProgram.objects.filter(
        setup_status='live').select_related('account')

    total_inserted = total_touched = 0
    for program in programs:
        inserted, touched = reconcile_program(program, days=days)
        total_inserted += inserted
        total_touched += touched

    if total_inserted:
        logger.info(
            'voice.tasks: reconciliation inserted %s calls the webhook '
            'missed across %s periods', total_inserted, total_touched)
    return {'programs': programs.count(), 'inserted': total_inserted,
            'periods': total_touched}


@shared_task
def sweep_unbilled_overage():
    """Daily backstop for overage that ``invoice.created`` did not catch.

    The webhook is the primary path and bills on the right invoice. This
    catches periods that ended, have overage, were never billed, and are
    now old enough that no invoice is going to arrive for them — a missed
    webhook, or a client whose subscription ended mid-period still owing
    minutes.

    Deliberately conservative: only periods that ended more than a day
    ago, so it can never race the webhook for the same period.
    """
    from voice.billing import attach_overage
    from voice.models import VoiceUsagePeriod

    cutoff = timezone.now() - timedelta(days=1)
    stale = (VoiceUsagePeriod.objects
             .filter(period_end__lt=cutoff,
                     stripe_invoice_item_id='',
                     overage_minutes__gt=0)
             .select_related('program', 'program__account'))

    billed = 0
    for period in stale:
        plan = None
        try:
            from clients.contract_options import VOICE_RECEPTIONIST_SLUG
            plan = (period.program.account.maintenance_plans
                    .filter(tier_slug=VOICE_RECEPTIONIST_SLUG)
                    .exclude(stripe_subscription_id='')
                    .first())
        except Exception:
            logger.exception('voice.tasks: plan lookup failed for period %s',
                             period.pk)
        if attach_overage(
                period,
                subscription_id=(plan.stripe_subscription_id if plan else '')):
            billed += 1

    if billed:
        logger.info('voice.tasks: swept %s unbilled overage periods', billed)
    return {'billed': billed}
