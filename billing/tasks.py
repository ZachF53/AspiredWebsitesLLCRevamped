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
def send_intake_reminder_task(project_id, day):
    """Send a Day-2 / Day-4 intake reminder if intake is still incomplete."""
    from clients.emails import send_intake_reminder_email
    from clients.models import Project

    project = Project.objects.filter(id=project_id).first()
    if project is None:
        return
    intake = getattr(project, 'intake', None)
    if intake is not None and intake.completed:
        return  # Intake already done — no reminder needed.
    send_intake_reminder_email(project, day)


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
