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
