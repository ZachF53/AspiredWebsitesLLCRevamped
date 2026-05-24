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

from clients.emails import (
    send_onboarding_setup_email,
    send_payment_failed_email,
    send_welcome_email,
)
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


def _client_for_invoice(invoice_id):
    if not invoice_id:
        return None
    return ClientProfile.objects.filter(stripe_invoice_id=invoice_id).first()


# ── invoice.paid ────────────────────────────────────────────────────────────

def _handle_invoice_paid(event):
    invoice = event['data']['object']
    # Look up by invoice ID first (set on admin-created onboarding invoices),
    # fall back to customer ID for contract-flow invoices that pre-date the
    # stripe_invoice_id field.
    client = (_client_for_invoice(invoice.get('id'))
              or _client_for_customer(invoice.get('customer')))
    if client is None:
        logger.warning('invoice.paid: no client for invoice %s / customer %s',
                       invoice.get('id'), invoice.get('customer'))
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

    kind = (invoice.get('metadata') or {}).get('kind')

    # ── NEW admin onboarding-invoice flow (Part 3) ──
    # Triggered by the admin invoice-creation form. Creates the Project
    # the intake form needs, activates the Django user so they can log
    # in, and emails the setup link. Droplet provisioning is deferred
    # until intake completion (Part 6).
    if kind == 'onboarding_setup' or not client.projects.exists():
        _on_onboarding_invoice_paid(client)
        return

    project = client.projects.order_by('-created_at').first()
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


def _on_onboarding_invoice_paid(client):
    """
    First-touch handler for the admin onboarding-invoice flow.

    The client paid the single one-off invoice that covers their build
    (and optionally first-month maintenance + hosting). We:
      1. Activate the Django user so they can use the setup link
      2. Mark the profile active + pending_setup
      3. Create the Project + IntakeResponse so the intake page works
         once they log in
      4. Email the setup link (only if it wasn't already sent at invoice
         creation time)

    Note: Droplet provisioning is intentionally deferred to intake
    completion — we don't want to spin up a $6 Droplet for someone who
    paid but never submits intake. See `clients/views.intake` (Part 6).
    """
    from clients.models import Project
    from vault.models import ClientVault

    if client.user and not client.user.is_active:
        client.user.is_active = True
        client.user.save(update_fields=['is_active'])

    fields_to_save = ['status', 'updated_at']
    client.status = 'active'
    if client.onboarding_status not in (
            'pending_intake', 'onboarding_complete'):
        client.onboarding_status = 'pending_setup'
        fields_to_save.append('onboarding_status')
    client.save(update_fields=fields_to_save)

    project, _ = Project.objects.get_or_create(
        client=client,
        defaults={
            # `payment_status='fully_paid'` because the admin onboarding
            # invoice is a single one-off — not split into deposit/final.
            'payment_status': 'fully_paid',
            'package': (
                client.package
                if client.package in ('essential_build', 'premium_build')
                else ''),
            'stage': 'intake',
            'final_paid_at': timezone.now(),
        },
    )
    IntakeResponse.objects.get_or_create(project=project)
    ClientVault.objects.get_or_create(client=client)

    # Resend the setup link unless the token has already been used. The
    # admin view sent it once at invoice creation; this catches the
    # case where the prior send failed (e.g. SendGrid was down).
    token = getattr(client, 'onboarding_token', None)
    if token and not token.used:
        try:
            send_onboarding_setup_email(client, token)
        except Exception:
            logger.exception(
                'onboarding setup email failed for %s', client.pk)

    logger.info(
        'invoice.paid (onboarding_setup): client %s activated, '
        'project %s pending intake', client.pk, project.pk)


def _on_deposit_paid(client, project):
    """
    Legacy contract-flow handler — kept for the existing contract-signing
    path. Sends the welcome email + schedules Day-2/4 intake reminders.

    Droplet provisioning used to happen here; it's been moved to intake
    completion (Part 6) so a paid-but-never-submitted client doesn't
    waste a Droplet.
    """
    IntakeResponse.objects.get_or_create(project=project)
    from vault.models import ClientVault
    ClientVault.objects.get_or_create(client=client)
    send_welcome_email(client, project)
    _schedule_intake_reminders(project)


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
