"""Celery tasks for billing + onboarding automation."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def provision_droplet_task(client_id):
    """Provision a client's DigitalOcean Droplet after deposit payment."""
    from clients.models import ClientProfile
    from billing.do_helpers import provision_client_droplet

    client = ClientProfile.objects.filter(id=client_id).first()
    if client is None:
        logger.warning('provision_droplet_task: no client %s', client_id)
        return
    provision_client_droplet(client)


@shared_task
def send_intake_reminder_task(client_id, day):
    """Send a Day-2 / Day-4 intake reminder if intake is still incomplete.

    Backwards-compat note: the task signature changed from
    `(project_id, day)` to `(client_id, day)` in the Project→Client
    consolidation. Old in-flight tasks queued under the previous
    signature would still resolve a valid UUID (the project IDs and
    client IDs are both UUIDs, but distinct namespaces) and silently
    no-op — the ClientProfile.objects.filter call would return None.
    """
    from clients.emails import send_intake_reminder_email
    from clients.models import ClientProfile

    client = ClientProfile.objects.filter(id=client_id).first()
    if client is None:
        return
    intake = getattr(client, 'intake', None)
    if intake is not None and intake.completed:
        return  # Intake already done — no reminder needed.
    send_intake_reminder_email(client, day)


@shared_task
def send_payment_failed_email_task(client_id, day):
    """Send a Day-7 / Day-14 payment-failure follow-up email."""
    from clients.emails import send_payment_failed_email
    from clients.models import ClientProfile

    client = ClientProfile.objects.filter(id=client_id).first()
    if client is None:
        return
    send_payment_failed_email(client, day)


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
    from clients.models import ClientProfile

    client = None
    if client_id:
        client = ClientProfile.objects.filter(id=client_id).first()
        if client is None:
            logger.warning(
                'provision_manual_droplet_task: client %s not found '
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
