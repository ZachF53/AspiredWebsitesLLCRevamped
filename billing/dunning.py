"""
Payment-failure dunning, driven by a daily sweep over current state.

WHY THIS IS A SWEEP AND NOT A CHAIN OF SCHEDULED TASKS
------------------------------------------------------
The previous design queued nine `apply_async(countdown=...)` messages the
instant a payment failed — Day 3/7/14 emails and the Day 14/21/30/60
droplet escalation. Two things went wrong with that, both on prod:

  1. The decision was made at queue time and executed days later against
     state that had changed. A client who paid still got told her payment
     had failed, because the email task never re-read the account.
  2. Redis has no delayed delivery, so those messages sat "in flight" for
     weeks and the broker kept redelivering them. Prod accumulated 42
     copies of every task; all 42 run when the ETA arrives.

Both are properties of scheduling far ahead. So nothing is scheduled far
ahead any more. `run_dunning_sweep()` runs once a day, asks what is true
right now, and does whatever is due. Nothing sits in the broker, and
every action re-reads state milliseconds before it acts.

STATE, NOT EVENTS
-----------------
The sweep does not trust the webhook that opened the window. Each day it
asks Stripe whether the account actually owes money. If it does not, the
window closes and any suspended site is restored. That makes a wrongly
opened window self-healing: it costs one wasted day instead of a 60-day
march toward droplet destruction. It is also why the old
`billing_reason == 'subscription_create'` special case is gone — a card
declining at checkout opens a window that closes itself the next day,
while a first invoice that genuinely never gets paid now escalates
properly instead of being skipped forever.

Failing to reach Stripe is NOT treated as "not delinquent" — the sweep
does nothing for that account and alerts, so an API outage can neither
destroy a droplet nor silently cancel enforcement.

DESTRUCTIVE STAGES HOLD FOR A HUMAN
-----------------------------------
Day 14 (maintenance page) and Day 21 (power off) are reversible and run
automatically. Day 30 (destroy droplet) and Day 60 (delete snapshot) are
not, so they claim their ledger row as `awaiting_approval`, raise a
critical SystemAlert, and wait for an operator. Claiming the row is what
stops the alert firing again every single day.
"""

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from billing.dunning_models import DunningEvent

logger = logging.getLogger(__name__)


# ── Schedule ────────────────────────────────────────────────────────────────
# (stage, day_threshold, scope, needs_approval)
#
# `scope` is 'account' for the emails (the card belongs to the customer)
# and 'site' for everything that touches a droplet (one droplet per site,
# so a two-site account escalates both).
#
# Day thresholds match CLAUDE.md's payment-failure chain. Note the Day-3
# email is now genuinely sent on day 3: the old code sent it immediately
# on the webhook while labelling it "day 3", which meant a card that
# declined once and cleared on retry still generated an instant alarm.
SCHEDULE = [
    (DunningEvent.STAGE_EMAIL_3, 3, 'account', False),
    (DunningEvent.STAGE_EMAIL_7, 7, 'account', False),
    (DunningEvent.STAGE_EMAIL_14, 14, 'account', False),
    (DunningEvent.STAGE_MAINTENANCE, 14, 'site', False),
    (DunningEvent.STAGE_OFFLINE, 21, 'site', False),
    (DunningEvent.STAGE_DESTROY, 30, 'site', True),
    (DunningEvent.STAGE_SNAPSHOT_DELETE, 60, 'site', True),
]

# Stages whose effect must be undone if the window turns out to be a
# false positive, in the order they need undoing.
_SUSPENDING_STAGES = (
    DunningEvent.STAGE_MAINTENANCE,
    DunningEvent.STAGE_OFFLINE,
)


# ── Stripe: does this account actually owe us money right now? ──────────────

def check_delinquency(account):
    """True / False / None for "does this account owe money right now".

    None means we could not tell — Stripe unreachable, no customer id,
    misconfigured keys. The caller must treat None as "do nothing", never
    as either answer: reading it as False silently cancels enforcement
    for a real deadbeat, and reading it as True can destroy a paying
    client's droplet.
    """
    customer_id = (getattr(account, 'stripe_customer_id', '') or '').strip()
    if not customer_id:
        # Nothing to check against. Not an error worth alerting on every
        # day, but we cannot claim they are current either.
        logger.warning(
            'dunning: account %s has no stripe_customer_id — cannot '
            'verify delinquency', account.pk)
        return None

    try:
        import stripe

        from billing.stripe_helpers import _init
        _init()

        # A subscription Stripe itself considers behind is the clearest
        # signal, and it is what the retry/dunning settings in the Stripe
        # dashboard drive.
        for sub in stripe.Subscription.list(
                customer=customer_id, status='all', limit=100).data:
            if sub.status in ('past_due', 'unpaid'):
                return True

        # An invoice that is finalised, unpaid, and past its due date.
        # `open` invoices inside their payment window (the send_invoice
        # path gives 7 days) are NOT delinquent yet.
        now_ts = timezone.now().timestamp()
        for inv in stripe.Invoice.list(
                customer=customer_id, status='open', limit=100).data:
            due = getattr(inv, 'due_date', None)
            if due is None or due <= now_ts:
                return True

        return False
    except Exception:
        logger.exception(
            'dunning: Stripe delinquency check failed for account %s',
            account.pk)
        return None


# ── Ledger ──────────────────────────────────────────────────────────────────

def claim_stage(account, stage, window_started_at, website=None,
                status=DunningEvent.STATUS_DONE):
    """Claim the right to run `stage` once for this failure window.

    Returns the DunningEvent on success, or None if it was already
    claimed. The uniqueness is a database constraint, so two workers
    racing on the same stage cannot both win.
    """
    try:
        with transaction.atomic():
            return DunningEvent.objects.create(
                account=account,
                website=website,
                window_started_at=window_started_at,
                stage=stage,
                status=status,
            )
    except IntegrityError:
        return None


# ── Stage handlers ──────────────────────────────────────────────────────────

def _send_dunning_email(account, day):
    from clients.emails import send_payment_failed_email
    send_payment_failed_email(account, day)


_EMAIL_DAY = {
    DunningEvent.STAGE_EMAIL_3: 3,
    DunningEvent.STAGE_EMAIL_7: 7,
    DunningEvent.STAGE_EMAIL_14: 14,
}


def _run_stage(stage, account, website):
    """Perform one stage's side effect. Raises on failure."""
    from billing import do_helpers

    if stage in _EMAIL_DAY:
        _send_dunning_email(account, _EMAIL_DAY[stage])
    elif stage == DunningEvent.STAGE_MAINTENANCE:
        do_helpers.set_site_maintenance_mode(website)
    elif stage == DunningEvent.STAGE_OFFLINE:
        do_helpers.set_site_offline(website)
    elif stage == DunningEvent.STAGE_DESTROY:
        do_helpers.destroy_client_droplet(website)
    elif stage == DunningEvent.STAGE_SNAPSHOT_DELETE:
        do_helpers.delete_client_snapshot(website)
    else:
        raise ValueError(f'unknown dunning stage {stage!r}')


def _alert_awaiting_approval(account, website, stage, days):
    from core.system_alerts import record_alert

    label = dict(DunningEvent.STAGE_CHOICES).get(stage, stage)
    record_alert(
        severity='critical',
        source='billing.dunning.approval',
        message=(f'Dunning step needs your approval: {label} for '
                 f'{account} (day {days})'),
        detail=(
            f'Account {account.pk} has been in a payment-failure window '
            f'for {days} days and reached "{label}". This step is '
            f'irreversible, so it has NOT run.\n\n'
            f'Site: {getattr(website, "pk", None)}\n\n'
            f'Approve or cancel it under Dunning in the admin dashboard. '
            f'If the client has since paid, do nothing — the next sweep '
            f'closes the window and cancels this step automatically.'
        ),
    )


# ── Closing a window ────────────────────────────────────────────────────────

def close_window(account, reason):
    """Clear the guard and undo anything the window already did.

    Called when Stripe says the account is current. Deliberately does NOT
    charge the $75 second-offence reinstatement fee and does NOT touch
    `payment_failure_offenses`: that fee belongs to the `invoice.paid`
    reinstatement path, where we know a real recovery happened. A window
    that should never have opened must not cost the client anything, and
    an inflated offence counter is exactly what would have auto-charged
    Burgland Technology $75 on their next hiccup.
    """
    from billing import do_helpers

    window = account.payment_failure_started_at
    if window is None:
        return

    # Undo suspensions this window caused, and only this window's — a
    # site that was already off for another reason is left alone.
    suspended = (DunningEvent.objects
                 .filter(account=account,
                         window_started_at=window,
                         stage__in=_SUSPENDING_STAGES,
                         status__in=(DunningEvent.STATUS_DONE,
                                     DunningEvent.STATUS_APPROVED))
                 .exclude(website=None)
                 .values_list('website_id', flat=True)
                 .distinct())
    from clients.account_models import Website
    for site in Website.objects.filter(id__in=list(suspended)):
        try:
            do_helpers.restore_client_site(site)
            logger.info('dunning: restored site %s on window close', site.pk)
        except Exception:
            logger.exception(
                'dunning: restore failed for site %s during window close',
                site.pk)

    # Any destructive step still waiting on a human is moot now.
    cancelled = (DunningEvent.objects
                 .filter(account=account,
                         window_started_at=window,
                         status=DunningEvent.STATUS_AWAITING_APPROVAL)
                 .update(status=DunningEvent.STATUS_CANCELLED,
                         detail=f'Window closed: {reason}'))

    account.payment_failure_started_at = None
    account.save(update_fields=['payment_failure_started_at', 'updated_at'])
    logger.info(
        'dunning: closed window for account %s (%s); %s pending step(s) '
        'cancelled', account.pk, reason, cancelled)


# ── The sweep ───────────────────────────────────────────────────────────────

def run_dunning_sweep(now=None):
    """Advance every open payment-failure window by whatever is due.

    Returns a summary dict — the management command and the Celery task
    both log it.
    """
    from clients.account_models import Account, Website

    now = now or timezone.now()
    summary = {'accounts': 0, 'closed': 0, 'ran': 0,
               'awaiting_approval': 0, 'skipped_unknown': 0, 'failed': 0}

    accounts = Account.objects.filter(
        payment_failure_started_at__isnull=False)

    for account in accounts:
        summary['accounts'] += 1
        window = account.payment_failure_started_at

        state = check_delinquency(account)
        if state is None:
            # Cannot tell — do nothing at all for this account today.
            summary['skipped_unknown'] += 1
            continue
        if state is False:
            close_window(account, 'Stripe reports no amount outstanding')
            summary['closed'] += 1
            continue

        days = (now - window).days
        sites = list(Website.objects.filter(account=account))

        for stage, threshold, scope, needs_approval in SCHEDULE:
            if days < threshold:
                continue

            targets = [None] if scope == 'account' else sites
            if scope == 'site' and not sites:
                # A delinquent account with nothing to take down is a
                # silent non-event otherwise.
                _alert_no_site(account, stage)
                continue

            for website in targets:
                if needs_approval:
                    event = claim_stage(
                        account, stage, window, website=website,
                        status=DunningEvent.STATUS_AWAITING_APPROVAL)
                    if event is None:
                        continue  # already claimed — do not re-alert
                    _alert_awaiting_approval(account, website, stage, days)
                    summary['awaiting_approval'] += 1
                    continue

                event = claim_stage(account, stage, window, website=website)
                if event is None:
                    continue  # already run for this window

                try:
                    _run_stage(stage, account, website)
                    summary['ran'] += 1
                except Exception as exc:  # noqa: BLE001
                    # The row stays claimed on purpose. A stage that
                    # raises every day would otherwise retry forever and
                    # re-alert forever; an operator gets one alert and
                    # can re-run it deliberately.
                    logger.exception(
                        'dunning: stage %s failed for account %s site %s',
                        stage, account.pk, getattr(website, 'pk', None))
                    event.status = DunningEvent.STATUS_FAILED
                    event.detail = str(exc)[:2000]
                    event.save(update_fields=['status', 'detail', 'updated_at'])
                    _alert_stage_failed(account, website, stage, exc)
                    summary['failed'] += 1

    logger.info('dunning sweep: %s', summary)
    return summary


def _alert_no_site(account, stage):
    from core.system_alerts import record_alert
    record_alert(
        severity='error',
        source='billing.dunning.no_site',
        message=(f'{account} is in dunning at stage {stage} but has no '
                 f'website to act on'),
        detail=('Nothing can be suspended or destroyed for this account, '
                'so non-payment has no enforcement path. Check whether '
                'the site was deleted or never linked.'),
    )


def _alert_stage_failed(account, website, stage, exc):
    from core.system_alerts import record_alert
    record_alert(
        severity='error',
        source='billing.dunning.stage_failed',
        message=f'Dunning stage {stage} failed for {account}',
        detail=(f'Site: {getattr(website, "pk", None)}\n'
                f'Error: {exc}\n\n'
                'The step is recorded as failed and will NOT retry on its '
                'own. Re-run it deliberately once the cause is fixed.'),
    )


# ── Operator approval ───────────────────────────────────────────────────────

def approve_stage(event, user):
    """Run a destructive stage an operator has confirmed.

    Re-checks delinquency first: the approval may be hours or days old,
    and the client may have paid in between. That re-check is the whole
    lesson of this module — never act on a decision made earlier than the
    action itself.
    """
    if event.status != DunningEvent.STATUS_AWAITING_APPROVAL:
        return False, f'Step is {event.get_status_display().lower()}, not pending.'

    account = event.account
    if account.payment_failure_started_at != event.window_started_at:
        return False, 'That failure window has closed — step no longer applies.'

    state = check_delinquency(account)
    if state is None:
        return False, 'Could not reach Stripe to confirm — try again shortly.'
    if state is False:
        close_window(account, 'confirmed current at approval time')
        return False, 'Stripe says this account is current. Window closed instead.'

    try:
        _run_stage(event.stage, account, event.website)
    except Exception as exc:  # noqa: BLE001
        logger.exception('dunning: approved stage %s failed', event.stage)
        event.status = DunningEvent.STATUS_FAILED
        event.detail = str(exc)[:2000]
        event.save(update_fields=['status', 'detail', 'updated_at'])
        return False, f'Step failed: {exc}'

    event.status = DunningEvent.STATUS_APPROVED
    event.approved_by = user
    event.approved_at = timezone.now()
    event.save(update_fields=[
        'status', 'approved_by', 'approved_at', 'updated_at'])
    return True, 'Step completed.'


def cancel_stage(event, user, reason=''):
    """Operator declines a pending destructive stage."""
    if event.status != DunningEvent.STATUS_AWAITING_APPROVAL:
        return False, f'Step is {event.get_status_display().lower()}, not pending.'
    event.status = DunningEvent.STATUS_CANCELLED
    event.approved_by = user
    event.approved_at = timezone.now()
    event.detail = reason or 'Cancelled by operator.'
    event.save(update_fields=[
        'status', 'approved_by', 'approved_at', 'detail', 'updated_at'])
    return True, 'Step cancelled.'
