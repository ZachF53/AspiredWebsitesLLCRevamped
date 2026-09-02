"""Celery tasks for billing + onboarding automation."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)

DAY_SECONDS = 24 * 60 * 60


@shared_task
def provision_droplet_task(client_id):
    """Provision a site's DigitalOcean Droplet after deposit payment.

    One droplet per site (CLAUDE.md), so this takes a website id.
    """
    from clients.account_models import Website
    from billing.do_helpers import provision_client_droplet

    client = (Website.objects
              .select_related('account')
              .filter(id=client_id)
              .first())
    if client is None:
        logger.warning('provision_droplet_task: no website %s', client_id)
        return
    provision_client_droplet(client)


@shared_task
def send_intake_reminder_task(client_id, day):
    """Send a Day-2 / Day-4 intake reminder if intake is still incomplete.

    The intake describes a build, so this is per site: a client with two
    builds owes two intakes and is reminded about each.

    Backwards-compat note: this signature has moved twice, from
    `(project_id, day)` to `(client_id, day)` to a website id. An
    in-flight task queued under an older signature resolves to nothing
    and no-ops, which is the safe direction — a missed reminder, not a
    reminder sent to the wrong client.
    """
    from clients.account_models import Website
    from clients.emails import send_intake_reminder_email

    client = (Website.objects
              .select_related('account')
              .filter(id=client_id)
              .first())
    if client is None:
        return
    intake = getattr(client, 'intake_new', None)
    if intake is not None and intake.completed:
        return  # Intake already done — no reminder needed.
    send_intake_reminder_email(client, day)


@shared_task
def send_payment_failed_email_task(client_id, day):
    """Send a Day-7 / Day-14 payment-failure follow-up email.

    Account-level: the card that failed belongs to the customer, not to
    one of their sites.

    Two things this has to check before sending, both learned the hard
    way on 2026-09-02 when a client who had paid a week earlier was told
    her payment had failed:

    1. `payment_failure_started_at` — the same reinstatement guard the
       14/21/30/60-day escalation tasks read. Nulling it on payment is
       what cancels the whole in-flight chain, but this task never
       looked at it, so the emails kept going out to a paid-up customer
       while her site was (correctly) left alone. A dunning email is a
       claim about the account's state, so it has to re-read that state
       at send time, not trust the state from when it was queued.

    2. A send-once marker — see `_claim_dunning_send`. Belt and braces
       against duplicate delivery of the same message.
    """
    from clients.account_models import Account
    from clients.emails import send_payment_failed_email

    client = Account.objects.filter(id=client_id).first()
    if client is None:
        return

    if client.payment_failure_started_at is None:
        # Paid / reinstated in the meantime — cancel by no-op, exactly
        # as the escalation tasks do.
        logger.info(
            'send_payment_failed_email_task: account %s is paid up — '
            'Day-%s dunning email suppressed', client_id, day)
        return

    if not _claim_dunning_send(client_id, day):
        logger.warning(
            'send_payment_failed_email_task: Day-%s email for account %s '
            'already sent — duplicate delivery suppressed', day, client_id)
        return

    send_payment_failed_email(client, day)


def _claim_dunning_send(client_id, day):
    """Claim the right to send this (account, day) dunning email once.

    True on the first call, False for every later one within the window.

    The guard above stops the common case, but it can't stop duplicates
    of the *same* fire: when the broker redelivers a parked ETA message,
    every copy sees an identical, still-delinquent account and all of
    them send. This makes the send itself idempotent so a genuinely
    delinquent client gets one Day-7 email rather than one per copy.

    `cache.add` is atomic on Redis, so the first caller wins. The window
    is 30 days: comfortably longer than the Day-7 → Day-14 gap, so the
    two stages never collide with each other (the key includes `day`)
    and a redelivery arriving days late is still recognised.

    Fails OPEN — if the cache is down, send. A duplicate dunning email is
    an annoyance; silently swallowing the real one lets an account slide
    toward the droplet-destroy chain with no warning to the client.
    """
    from django.core.cache import cache

    try:
        return bool(cache.add(f'dunning-email:{client_id}:{day}', 1,
                              timeout=30 * DAY_SECONDS))
    except Exception:
        logger.exception(
            '_claim_dunning_send: cache unavailable for %s day %s — '
            'sending anyway', client_id, day)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — payment-failure dunning escalation tasks
# ─────────────────────────────────────────────────────────────────────────────
# Each task takes a WEBSITE id. The droplet, its 503 vhost and its
# retention snapshot all belong to a site; the escalation guard and the
# offence counter belong to the Account, because billing does. So the
# task resolves the site and reads the guard through `site.account`.
#
# When the guard is None, payment was received in the meantime
# (reinstatement) and the task no-ops. That is how the chain self-cancels
# without tracking and revoking individual Celery task IDs.
#
# Order:
#   Day 14 → set_site_maintenance_mode_task    (503 page, droplet up)
#   Day 21 → set_site_offline_task             (droplet powered down)
#   Day 30 → destroy_client_droplet_task       (snapshot, then destroy)
#   Day 60 → delete_client_snapshot_task       (no recovery after this)


def _site_still_in_failure_window(website_id, task_name):
    """Resolve the site, or return None if the chain should stop.

    Two different "None"s are deliberately distinguished. A site whose
    account has no `payment_failure_started_at` is a *cancellation* -- the
    normal, expected path when a client pays -- and is silent. A site id
    that resolves to nothing is a *fault*: the chain was scheduled against
    something that no longer exists, so an escalation step will not happen
    and nobody would otherwise know. Historically this returned silently
    for both, which meant a stale id looked exactly like a successful
    reinstatement.
    """
    from clients.account_models import Website

    site = (Website.objects
            .select_related('account')
            .filter(id=website_id)
            .first())
    if site is None:
        logger.error(
            '%s: no Website %s — escalation step skipped',
            task_name, website_id)
        try:
            from core.system_alerts import record_alert
            record_alert(
                severity='error',
                source=f'billing.escalation.{task_name}',
                message=(f'Dunning step {task_name} could not resolve '
                         f'website {website_id}'),
                detail=('The escalation chain was scheduled against a site '
                        'that no longer exists, so this step did not run. '
                        'Check whether the site was deleted mid-chain or '
                        'the task was queued under the pre-cutover '
                        'signature, which passed a ClientProfile id.'),
            )
        except Exception:
            logger.exception('%s: could not record the alert', task_name)
        return None

    account = site.account
    if account is None or account.payment_failure_started_at is None:
        # Paid up / reinstated — cancel the chain by no-op.
        return None
    return site


@shared_task
def set_site_maintenance_mode_task(website_id):
    """Day 14 — flip site to maintenance (503) if still in failure window."""
    from billing.do_helpers import set_site_maintenance_mode
    site = _site_still_in_failure_window(
        website_id, 'set_site_maintenance_mode_task')
    if site is None:
        return
    try:
        set_site_maintenance_mode(site)
    except Exception:
        logger.exception(
            'set_site_maintenance_mode_task failed for site %s', website_id)


@shared_task
def set_site_offline_task(website_id):
    """Day 21 — power the Droplet off if still in failure window."""
    from billing.do_helpers import set_site_offline
    site = _site_still_in_failure_window(
        website_id, 'set_site_offline_task')
    if site is None:
        return
    try:
        set_site_offline(site)
    except Exception:
        logger.exception(
            'set_site_offline_task failed for site %s', website_id)


@shared_task
def destroy_client_droplet_task(website_id):
    """Day 30 — snapshot for 60-day retention, then destroy the Droplet."""
    from billing.do_helpers import destroy_client_droplet
    site = _site_still_in_failure_window(
        website_id, 'destroy_client_droplet_task')
    if site is None:
        return
    try:
        destroy_client_droplet(site)
    except Exception:
        logger.exception(
            'destroy_client_droplet_task failed for site %s', website_id)


@shared_task
def delete_client_snapshot_task(website_id):
    """Day 60 — delete the retention snapshot. Last call."""
    from billing.do_helpers import delete_client_snapshot
    site = _site_still_in_failure_window(
        website_id, 'delete_client_snapshot_task')
    if site is None:
        return
    try:
        delete_client_snapshot(site)
    except Exception:
        logger.exception(
            'delete_client_snapshot_task failed for site %s', website_id)


@shared_task
def provision_manual_droplet_task(name, region, size, snapshot_id,
                                  client_id=None, tags=None):
    """
    Spin up a manual / linked Droplet from the admin Droplet dashboard.

    Runs out of process so the form POST doesn't sit through DO's 1–3
    minute provisioning poll. `create_droplet` handles the cloud-init
    temp password, polling to active, and the (non-blocking) vault key
    install when a client is linked.
    """
    from billing.do_helpers import create_droplet
    from clients.account_models import Website

    client = None
    if client_id:
        client = (Website.objects
                  .select_related('account')
                  .filter(id=client_id)
                  .first())
        if client is None:
            logger.warning(
                'provision_manual_droplet_task: website %s not found '
                '— continuing as a manual (unlinked) Droplet.', client_id)

    try:
        droplet = create_droplet(
            name=name,
            region=region,
            size=size,
            snapshot_id=snapshot_id,
            tags=tags,
            client=client,
        )
        logger.info(
            'manual provision: %s ready at %s', name, droplet.get('ip'))
        return droplet
    except Exception:
        logger.exception('manual provision failed for %s', name)
        raise


@shared_task
def reconcile_subscriptions_task():
    """
    Daily safety net — Celery wrapper around the
    `reconcile_subscriptions` management command. Confirms every
    active hosting subscription still has a live Droplet; cancels
    any drift before the next billing cycle.
    """
    from io import StringIO

    from django.core.management import call_command

    buf = StringIO()
    call_command('reconcile_subscriptions', stdout=buf)
    summary = buf.getvalue().splitlines()
    last_line = summary[-1] if summary else ''
    logger.info('reconcile_subscriptions: %s', last_line)
    return last_line


@shared_task
def reconcile_domains_task():
    """
    Daily — pull every active DomainRegistration's state from
    Namecheap, mirror locally, and send 7-day pre-renewal heads-ups
    to clients whose subs renew this week.
    """
    from io import StringIO

    from django.core.management import call_command

    buf = StringIO()
    call_command('reconcile_domains', stdout=buf)
    summary = buf.getvalue().splitlines()
    last_line = summary[-1] if summary else ''
    logger.info('reconcile_domains: %s', last_line)
    return last_line


@shared_task
def send_maintenance_upsell_nudges_task():
    """
    Daily — drains the 30-day / 60-day post-launch maintenance upsell
    nudge queue. Wraps the
    `send_maintenance_upsell_nudges` management command and logs the
    summary line so it shows in flower/journal.

    Idempotent — the command tracks each touchpoint per-client in
    `ClientProfile.maintenance_upsell_log` so re-running on the same
    day is a no-op once everyone in range is nudged.
    """
    from io import StringIO

    from django.core.management import call_command

    buf = StringIO()
    call_command('send_maintenance_upsell_nudges', stdout=buf)
    summary = buf.getvalue().splitlines()
    last_line = summary[-1] if summary else ''
    logger.info('send_maintenance_upsell_nudges: %s', last_line)
    return last_line
