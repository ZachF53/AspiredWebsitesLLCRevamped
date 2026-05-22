"""
Stripe webhook receiver.

Verifies the Stripe signature, then dispatches the handled events. Always
returns 200 once the payload is accepted so Stripe does not retry on our
internal errors (those are logged for follow-up).
"""

import json
import logging

import stripe
from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from clients.emails import send_payment_failed_email, send_welcome_email
from clients.models import ClientProfile, IntakeResponse

logger = logging.getLogger(__name__)

DAY = 86400  # seconds


@csrf_exempt
@require_POST
def stripe_webhook(request):
    """Stripe webhook endpoint — POST /billing/webhook/."""
    event = _verify_event(
        request.body, request.META.get('HTTP_STRIPE_SIGNATURE', ''),
    )
    if event is None:
        return HttpResponseBadRequest('Invalid Stripe webhook payload/signature')

    event_type = event.get('type', '')
    try:
        if event_type == 'invoice.paid':
            _handle_invoice_paid(event)
        elif event_type == 'invoice.payment_failed':
            _handle_invoice_payment_failed(event)
        elif event_type == 'customer.subscription.deleted':
            _handle_subscription_deleted(event)
        else:
            logger.info('Stripe webhook: unhandled event type %s', event_type)
    except Exception:
        logger.exception('Stripe webhook handler error for %s', event_type)
    return HttpResponse(status=200)


def _verify_event(payload, sig_header):
    """
    Return the parsed Stripe event, or None if it cannot be trusted.

    With STRIPE_WEBHOOK_SECRET set the signature is verified (production).
    Without it, an unverified payload is accepted ONLY when DEBUG is True so
    the flow can be exercised locally.
    """
    if settings.STRIPE_WEBHOOK_SECRET:
        try:
            return stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET,
            )
        except Exception:
            logger.warning('Stripe webhook signature verification failed.')
            return None
    if settings.DEBUG:
        try:
            return json.loads(payload)
        except ValueError:
            return None
    logger.error('Stripe webhook rejected: STRIPE_WEBHOOK_SECRET not configured.')
    return None


def _safe_payload(obj):
    """Coerce a Stripe object (or dict) into a JSON-serialisable dict."""
    try:
        return json.loads(json.dumps(obj, default=str))
    except Exception:
        return {}


def _client_for_customer(customer_id):
    if not customer_id:
        return None
    return ClientProfile.objects.filter(stripe_customer_id=customer_id).first()


# ── invoice.paid ────────────────────────────────────────────────────────────

def _handle_invoice_paid(event):
    invoice = event['data']['object']
    client = _client_for_customer(invoice.get('customer'))
    if client is None:
        logger.warning('invoice.paid: no client for customer %s',
                       invoice.get('customer'))
        return

    # An invoice tied to a subscription is a maintenance-plan payment —
    # activate maintenance and stop (no build-project bookkeeping).
    if invoice.get('subscription'):
        client.maintenance_active = True
        if not client.maintenance_started_at:
            client.maintenance_started_at = timezone.now()
        client.save(update_fields=[
            'maintenance_active', 'maintenance_started_at', 'updated_at',
        ])
        logger.info('invoice.paid (subscription): maintenance active for %s',
                    client.pk)
        return

    project = client.projects.order_by('-created_at').first()
    if project is None:
        logger.warning('invoice.paid: no project for client %s', client.pk)
        return

    kind = (invoice.get('metadata') or {}).get('kind')
    if kind == 'final':
        project.payment_status = 'fully_paid'
        project.final_paid_at = timezone.now()
        project.save(update_fields=['payment_status', 'final_paid_at', 'updated_at'])
        logger.info('invoice.paid (final): project %s fully paid', project.pk)
    else:
        # Anything not explicitly 'final' is treated as the deposit.
        project.payment_status = 'deposit_paid'
        project.deposit_paid_at = timezone.now()
        project.save(update_fields=['payment_status', 'deposit_paid_at', 'updated_at'])
        _on_deposit_paid(client, project)


def _on_deposit_paid(client, project):
    """Post-deposit onboarding: intake, vault, welcome email, Droplet, reminders."""
    # Activate the intake form (ensure the row exists for the portal).
    IntakeResponse.objects.get_or_create(project=project)
    # Ensure the client has a credential vault (the ClientProfile post_save
    # signal also does this — belt and suspenders).
    from vault.models import ClientVault
    ClientVault.objects.get_or_create(client=client)
    send_welcome_email(client, project)
    _provision_droplet(client)
    _schedule_intake_reminders(project)


def _provision_droplet(client):
    """Enqueue Droplet provisioning (Part 6). Best effort — never blocks 200."""
    try:
        from billing.tasks import provision_droplet_task
        provision_droplet_task.delay(str(client.id))
    except Exception:
        logger.exception('Could not enqueue Droplet provisioning for %s', client.pk)


def _schedule_intake_reminders(project):
    """Schedule Day-2 + Day-4 intake reminders. Best effort."""
    try:
        from billing.tasks import send_intake_reminder_task
        send_intake_reminder_task.apply_async((str(project.id), 2), countdown=2 * DAY)
        send_intake_reminder_task.apply_async((str(project.id), 4), countdown=4 * DAY)
    except Exception:
        logger.exception('Could not schedule intake reminders for %s', project.pk)


# ── invoice.payment_failed ──────────────────────────────────────────────────

def _handle_invoice_payment_failed(event):
    from sync.models import SyncLog

    invoice = event['data']['object']
    client = _client_for_customer(invoice.get('customer'))

    SyncLog.objects.create(
        source_site='stripe',
        event_type='invoice.payment_failed',
        payload_received=_safe_payload(invoice),
        status='processed' if client else 'skipped',
        error_message='' if client else 'No matching client for Stripe customer.',
    )
    if client is None:
        return

    # Day 3 email now, then schedule Day 7 + Day 14 follow-ups.
    send_payment_failed_email(client, day=3)
    try:
        from billing.tasks import send_payment_failed_email_task
        send_payment_failed_email_task.apply_async((str(client.id), 7), countdown=7 * DAY)
        send_payment_failed_email_task.apply_async((str(client.id), 14), countdown=14 * DAY)
    except Exception:
        logger.exception('Could not schedule payment-failure follow-ups for %s',
                         client.pk)


# ── customer.subscription.deleted ───────────────────────────────────────────

def _handle_subscription_deleted(event):
    subscription = event['data']['object']
    client = _client_for_customer(subscription.get('customer'))
    if client is None:
        return
    client.maintenance_active = False
    client.save(update_fields=['maintenance_active', 'updated_at'])
    logger.info('subscription.deleted: maintenance off for client %s', client.pk)
