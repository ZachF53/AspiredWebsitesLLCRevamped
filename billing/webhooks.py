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
        if event_type == 'payment_intent.succeeded':
            _handle_payment_intent_succeeded(event)
        elif event_type == 'invoice.upcoming':
            _handle_invoice_upcoming(event)
        elif event_type == 'invoice.paid':
            _handle_invoice_paid(event)
        elif event_type == 'invoice.payment_failed':
            _handle_invoice_payment_failed(event)
        elif event_type == 'customer.subscription.deleted':
            _handle_subscription_deleted(event)
        elif event_type == 'customer.subscription.updated':
            _handle_subscription_updated(event)
        elif event_type == 'customer.subscription.created':
            # Phase 6 — self-checkout subscriptions trigger account
            # creation + magic-link email.
            _handle_self_checkout_subscription_created(event)
        else:
            logger.info('Stripe webhook: unhandled event type %s', event_type)
    except Exception:
        logger.exception('Stripe webhook handler error for %s', event_type)
    return HttpResponse(status=200)


# ── payment_intent.succeeded ────────────────────────────────────────────────

def _handle_payment_intent_succeeded(event):
    """
    Fires when the client successfully pays the onboarding invoice via
    Stripe Elements on our /pay/<token>/ page.

    Finds the OnboardingInvoice by `metadata.invoice_id`, marks it paid,
    runs the same downstream onboarding flow that the legacy Stripe
    Invoice path used (activate user, create Project, send setup link),
    and queues the branded PDF receipt.
    """
    from clients.models import OnboardingInvoice

    pi = event['data']['object']
    metadata = pi.get('metadata') or {}
    if metadata.get('kind') != 'onboarding':
        # Some other PaymentIntent — ignore. Subscription PIs, etc.
        logger.info(
            'payment_intent.succeeded: skipped (kind=%s, pi=%s)',
            metadata.get('kind'), pi.get('id'))
        return

    invoice_id = metadata.get('invoice_id')
    invoice = OnboardingInvoice.objects.filter(id=invoice_id).first()
    if invoice is None:
        logger.warning(
            'payment_intent.succeeded: no OnboardingInvoice for pi=%s '
            '(invoice_id=%s)',
            pi.get('id'), invoice_id)
        return

    if invoice.status == 'paid':
        # Already processed — idempotent re-delivery from Stripe.
        return

    # 0.4b — security check: refuse to mark paid unless the amount
    # actually charged matches the invoice total. Defends against a
    # tampered PaymentIntent where the metadata.invoice_id was kept
    # but the amount was reduced client-side.
    from billing.stripe_helpers import _cents
    try:
        expected_cents = _cents(invoice.total_amount)
    except Exception:
        expected_cents = 0
    charged_cents = pi.get('amount_received') or pi.get('amount') or 0
    if expected_cents and int(charged_cents) != int(expected_cents):
        logger.error(
            'payment_intent.succeeded amount mismatch — REFUSING to '
            'mark paid. pi=%s invoice=%s expected_cents=%s '
            'charged_cents=%s',
            pi.get('id'), invoice.pk, expected_cents, charged_cents)
        try:
            from core.system_alerts import record_alert
            record_alert(
                severity='critical',
                source='billing.webhooks.amount_mismatch',
                message=(f'PI amount mismatch on invoice {invoice.pk} — '
                         f'expected {expected_cents}c, got {charged_cents}c'),
                detail=f'pi={pi.get("id")} customer={pi.get("customer")}',
            )
        except Exception:
            pass
        return

    invoice.status = 'paid'
    invoice.paid_at = timezone.now()
    if not invoice.stripe_payment_intent_id:
        invoice.stripe_payment_intent_id = pi.get('id', '')
    invoice.save(update_fields=[
        'status', 'paid_at', 'stripe_payment_intent_id', 'updated_at',
    ])

    client = invoice.client

    # Attach the card to the customer + set as default so future
    # subscriptions (hosting, domain) charge it automatically. PI had
    # setup_future_usage='off_session' set, so Stripe has already
    # attached the PaymentMethod — we just need to mark it default.
    pm_id = pi.get('payment_method') or ''
    if pm_id and client.stripe_customer_id:
        try:
            from billing.stripe_helpers import (
                attach_payment_method_to_customer,
            )
            attach_payment_method_to_customer(
                client.stripe_customer_id, pm_id,
                set_as_default=True)
        except Exception:
            logger.exception(
                'PM attach/default-set failed for client %s',
                client.pk)

    # Create the annual hosting subscription if hosting was on the
    # invoice. Hosting line items are recognised by their description
    # containing "hosting" (case-insensitive); we stored the line
    # items verbatim on OnboardingInvoice.line_items.
    if _invoice_has_hosting(invoice) and pm_id:
        try:
            from billing.stripe_helpers import (
                StripeNotConfigured, create_hosting_subscription,
            )
            create_hosting_subscription(
                client, default_payment_method_id=pm_id)
        except StripeNotConfigured:
            logger.warning(
                'Hosting subscription skipped for client %s — '
                'STRIPE_PRICE_HOSTING_YEARLY not configured',
                client.pk)
        except Exception:
            logger.exception(
                'Hosting subscription creation failed for client %s',
                client.pk)

    _on_onboarding_invoice_paid(client)

    # Generate the branded receipt PDF + email it. Best-effort; the
    # invoice row already records the payment, so receipt issues don't
    # block onboarding.
    try:
        from billing.receipt_pdf import generate_invoice_receipt_pdf
        generate_invoice_receipt_pdf(invoice)
    except Exception:
        logger.exception(
            'receipt PDF generation failed for invoice %s', invoice.pk)

    try:
        from clients.emails import send_invoice_receipt_email
        send_invoice_receipt_email(invoice)
        invoice.receipt_sent_at = timezone.now()
        invoice.save(update_fields=['receipt_sent_at', 'updated_at'])
    except Exception:
        logger.exception(
            'receipt email failed for invoice %s', invoice.pk)

    logger.info(
        'payment_intent.succeeded: OnboardingInvoice %s paid '
        '(client=%s)', invoice.pk, client.pk)


def _verify_event(payload, sig_header):
    """
    Return the parsed Stripe event AS A PLAIN DICT, or None if it can't
    be trusted.

    With STRIPE_WEBHOOK_SECRET set the signature is verified (production).
    Without it, an unverified payload is accepted ONLY when DEBUG is True
    so the flow can be exercised locally.

    Returned as a plain dict (not StripeObject) so the rest of the
    webhook code can use `.get(...)` safely. Stripe Python v8+ removed
    `.get()` from StripeObject — every previous webhook handler that
    used it was silently 500ing.

    We verify the signature with construct_event, then parse the ORIGINAL
    raw payload — which is already exactly the JSON Stripe signed — into a
    plain nested dict. This is robust across stripe-python versions:
    stripe 15's Event object isn't a dict subclass and has no
    `to_dict_recursive`, so converting the StripeObject is fragile (it
    silently degraded to a `str` and 500'd every handler).
    """
    if settings.STRIPE_WEBHOOK_SECRET:
        try:
            stripe.Webhook.construct_event(
                payload, sig_header, settings.STRIPE_WEBHOOK_SECRET,
            )
        except Exception:
            logger.warning('Stripe webhook signature verification failed.')
            return None
        try:
            return json.loads(payload)
        except (ValueError, TypeError):
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

    # Phase 1.6 — reinstatement gate. If this client is currently in
    # the payment-failure window (escalation chain scheduled), this
    # payment ends it. We run this BEFORE the subscription / kind
    # branches below so a paid maintenance renewal triggers
    # reinstatement (not just clears the failure email queue).
    #
    # Policy (CLAUDE.md): first offense free; 2nd+ offense incurs a
    # $75 fee charged off-session BEFORE restoration. If the fee
    # charge fails, we DO NOT restore — the admin gets a SystemAlert
    # and chases the client for a new card.
    if client.payment_failure_started_at is not None:
        _handle_reinstatement(client)
        # Don't return — fall through to the normal subscription /
        # onboarding branches so the underlying invoice still gets
        # the right local-state updates (maintenance_active=True etc).

    # An invoice tied to a subscription is a recurring charge. Two
    # subscription types live on the client right now (hosting +
    # maintenance) — disambiguate via the subscription ID before
    # flipping any local flags. Hosting invoices must NOT touch the
    # maintenance fields, and vice versa.
    sub_id = invoice.get('subscription') or ''
    if sub_id:
        if sub_id == client.stripe_hosting_subscription_id:
            # Hosting renewal cleared — nothing extra to record locally
            # beyond what Stripe already retains. Renewal gating happens
            # earlier in `invoice.upcoming`.
            logger.info(
                'invoice.paid (hosting sub): client %s hosting renewed',
                client.pk)
            return
        # Domain subscription renewal — call Namecheap renew + extend
        # local expires_at. The Stripe charge has already cleared, so
        # we owe the renewal regardless of Namecheap outcome.
        if _maybe_handle_domain_renewal(client, sub_id, invoice):
            return
        if sub_id == client.stripe_subscription_id:
            client.maintenance_active = True
            if not client.maintenance_started_at:
                client.maintenance_started_at = timezone.now()
            client.save(update_fields=[
                'maintenance_active', 'maintenance_started_at',
                'updated_at',
            ])
            logger.info(
                'invoice.paid (maintenance sub): maintenance active '
                'for %s', client.pk)
            return
        # Sub ID is unknown to us — likely a fresh subscription created
        # outside the portal flow. Be conservative and only activate
        # maintenance if the line item description hints at it.
        logger.warning(
            'invoice.paid (unknown sub %s) for client %s — checking '
            'line items', sub_id, client.pk)
        if _invoice_lines_mention_maintenance(invoice):
            client.maintenance_active = True
            client.stripe_subscription_id = sub_id
            if not client.maintenance_started_at:
                client.maintenance_started_at = timezone.now()
            client.save(update_fields=[
                'maintenance_active', 'stripe_subscription_id',
                'maintenance_started_at', 'updated_at',
            ])
        return

    kind = (invoice.get('metadata') or {}).get('kind')

    # ── Out-of-scope MiniInvoice flow (Phase 1.3) ──
    # Triggered when a previously-sent MiniInvoice is paid by the client.
    # We flip its status to 'paid' so portal work-blocking (Phase 1.4)
    # clears for them.
    if kind == 'mini_invoice':
        from billing.models import MiniInvoice
        mini_id = (invoice.get('metadata') or {}).get('mini_invoice_id')
        mini = None
        if mini_id:
            mini = MiniInvoice.objects.filter(id=mini_id).first()
        if mini is None and invoice.get('id'):
            mini = MiniInvoice.objects.filter(
                stripe_invoice_id=invoice.get('id')).first()
        if mini is None:
            logger.warning(
                'invoice.paid (mini_invoice): no MiniInvoice for '
                'invoice=%s metadata.mini_invoice_id=%s',
                invoice.get('id'), mini_id)
            return
        if mini.status == 'paid':
            return  # idempotent re-delivery
        mini.status = 'paid'
        mini.save(update_fields=['status', 'updated_at'])
        logger.info(
            'invoice.paid (mini_invoice): MiniInvoice %s paid', mini.pk)
        return

    # ── Admin onboarding-invoice flow ──
    # Triggered by the admin invoice-creation form. Activates the user,
    # bootstraps the IntakeResponse + ClientVault, and emails the setup
    # link. Droplet provisioning is deferred until intake completion.
    if kind == 'onboarding_setup' or client.stage in ('', 'intake'):
        _on_onboarding_invoice_paid(client)
        return

    if kind == 'final':
        client.payment_status = 'fully_paid'
        client.final_paid_at = timezone.now()
        client.save(update_fields=[
            'payment_status', 'final_paid_at', 'updated_at'])
        logger.info('invoice.paid (final): client %s fully paid', client.pk)
    else:
        # Anything not explicitly 'final' is treated as the deposit.
        client.payment_status = 'deposit_paid'
        client.deposit_paid_at = timezone.now()
        client.save(update_fields=[
            'payment_status', 'deposit_paid_at', 'updated_at'])
        _on_deposit_paid(client)


def _on_onboarding_invoice_paid(client):
    """
    First-touch handler for the admin onboarding-invoice flow.

    The client paid the single one-off invoice that covers their build
    (and optionally first-month maintenance + hosting). We:
      1. Activate the Django user so they can use the setup link
      2. Mark the profile active + pending_setup
      3. Bootstrap the IntakeResponse + ClientVault
      4. Mark payment_status='fully_paid' (admin invoice = single one-off)
      5. Email the setup link

    Note: Droplet provisioning is intentionally deferred to intake
    completion — we don't want to spin up a $6 Droplet for someone who
    paid but never submits intake. See `clients/views.intake` (Part 6).
    """
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

    # Admin onboarding invoice is single-pay (not split deposit/final),
    # so mark it fully paid up front. Stage stays 'intake' (the default).
    if client.payment_status != 'fully_paid':
        client.payment_status = 'fully_paid'
        client.final_paid_at = timezone.now()
        fields_to_save += ['payment_status', 'final_paid_at']
    if not client.stage:
        client.stage = 'intake'
        fields_to_save.append('stage')

    client.save(update_fields=fields_to_save)

    IntakeResponse.objects.get_or_create(client=client)
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
        'pending intake', client.pk)


def _on_deposit_paid(client):
    """
    Legacy contract-flow handler — kept for the existing contract-signing
    path. Sends the welcome email + schedules Day-2/4 intake reminders.

    Droplet provisioning used to happen here; it's been moved to intake
    completion (Part 6) so a paid-but-never-submitted client doesn't
    waste a Droplet.
    """
    IntakeResponse.objects.get_or_create(client=client)
    from vault.models import ClientVault
    ClientVault.objects.get_or_create(client=client)
    send_welcome_email(client)
    _schedule_intake_reminders(client)


def _schedule_intake_reminders(client):
    """Schedule Day-2 + Day-4 intake reminders. Best effort."""
    try:
        from billing.tasks import send_intake_reminder_task
        send_intake_reminder_task.apply_async(
            (str(client.id), 2), countdown=2 * DAY)
        send_intake_reminder_task.apply_async(
            (str(client.id), 4), countdown=4 * DAY)
    except Exception:
        logger.exception(
            'Could not schedule intake reminders for %s', client.pk)


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

    # Stamp the failure window on first failure (idempotent — only on
    # the first miss; subsequent failures within the same window leave
    # the timestamp alone). This is the GUARD field every scheduled
    # escalation task checks before acting; setting it to None
    # (reinstatement) is what self-cancels the chain.
    if client.payment_failure_started_at is None:
        client.payment_failure_started_at = timezone.now()
        client.save(update_fields=[
            'payment_failure_started_at', 'updated_at'])

    # Day 3 email now, then schedule Day 7 + Day 14 follow-ups.
    send_payment_failed_email(client, day=3)
    try:
        from billing.tasks import send_payment_failed_email_task
        send_payment_failed_email_task.apply_async((str(client.id), 7), countdown=7 * DAY)
        send_payment_failed_email_task.apply_async((str(client.id), 14), countdown=14 * DAY)
    except Exception:
        logger.exception('Could not schedule payment-failure follow-ups for %s',
                         client.pk)

    # Phase 1.1 — schedule the full 14/21/30/60 escalation chain.
    # Each task self-checks payment_failure_started_at; if reinstatement
    # nulls it, all remaining tasks no-op without us tracking IDs.
    try:
        from billing.tasks import (
            set_site_maintenance_mode_task,
            set_site_offline_task,
            destroy_client_droplet_task,
            delete_client_snapshot_task,
        )
        cid = str(client.id)
        set_site_maintenance_mode_task.apply_async(
            (cid,), countdown=14 * DAY)
        set_site_offline_task.apply_async(
            (cid,), countdown=21 * DAY)
        destroy_client_droplet_task.apply_async(
            (cid,), countdown=30 * DAY)
        delete_client_snapshot_task.apply_async(
            (cid,), countdown=60 * DAY)
    except Exception:
        logger.exception(
            'Could not schedule dunning escalation chain for %s',
            client.pk)


# ── customer.subscription.deleted ───────────────────────────────────────────

def _handle_subscription_deleted(event):
    """
    Fires when a subscription is fully ended (either cancel_at_period_end
    elapsed, or an immediate cancel). Clears our reference to it so the
    portal stops showing a "subscribed" state.
    """
    subscription = event['data']['object']
    sub_id = subscription.get('id', '')
    client = _client_for_customer(subscription.get('customer'))
    if client is None:
        return

    # Disambiguate which of our refs this was.
    fields = []
    if client.stripe_hosting_subscription_id == sub_id:
        client.stripe_hosting_subscription_id = ''
        fields.append('stripe_hosting_subscription_id')
        # Hosting cancelled → park every domain still pointing at
        # this client's now-defunct Droplet. Best-effort: failure
        # here doesn't block the rest of subscription teardown.
        _park_client_domains_on_hosting_cancel(client)
    if client.stripe_subscription_id == sub_id:
        client.maintenance_active = False
        client.stripe_subscription_id = ''
        fields.extend(['maintenance_active', 'stripe_subscription_id'])

    if fields:
        fields.append('updated_at')
        client.save(update_fields=fields)
        logger.info(
            'subscription.deleted: cleared %s for client %s',
            ', '.join(fields), client.pk)
    else:
        logger.info(
            'subscription.deleted: no matching ref for client %s '
            '(sub %s)', client.pk, sub_id)

    # Domain subscription deletion — flip the matching DomainRegistration
    # row to 'expired' so the portal stops showing it as active.
    try:
        from domains.models import DomainRegistration
        reg = DomainRegistration.objects.filter(
            stripe_subscription_id=sub_id).first()
        if reg:
            reg.status = 'expired' if reg.status == 'grace' else reg.status
            reg.stripe_subscription_id = ''
            reg.save(update_fields=[
                'status', 'stripe_subscription_id', 'updated_at'])
            logger.info(
                'subscription.deleted: domain %s now %s',
                reg.domain_name, reg.status)
    except Exception:
        logger.exception(
            'subscription.deleted: domain cleanup failed for sub %s',
            sub_id)


# ── invoice.upcoming ────────────────────────────────────────────────────────

def _handle_invoice_upcoming(event):
    """
    Stripe fires this ~3 days before each renewal invoice is created.
    For HOSTING subscriptions, we check that the client's Droplet is
    still alive on our DO account. For DOMAIN subscriptions, we check
    that the domain is still on the Namecheap account (it might have
    been transferred out by the client manually). For maintenance,
    there's no gate.

    If a gate fails we cancel the subscription before Stripe creates
    the renewal invoice, so the client never sees a charge for a
    resource they no longer have.
    """
    invoice = event['data']['object']
    sub_id = invoice.get('subscription') or ''
    if not sub_id:
        return

    # Domain gate fires first — different cancel helper, different
    # alert recipient.
    if _domain_renewal_gate(sub_id):
        return

    client = ClientProfile.objects.filter(
        stripe_hosting_subscription_id=sub_id).first()
    if client is None:
        # Not one of our hosting subs — maintenance subs etc. have
        # their own gate (or none).
        return

    if _droplet_alive(client):
        logger.info(
            'invoice.upcoming: hosting renewal cleared for client %s '
            '(droplet %s alive)', client.pk, client.do_droplet_id)
        return

    # Droplet is gone — cancel the subscription so the renewal
    # invoice doesn't generate a charge.
    try:
        from billing.stripe_helpers import cancel_hosting_subscription
        cancel_hosting_subscription(
            client,
            reason=(
                'invoice.upcoming gate: droplet '
                f'{client.do_droplet_id or "(unknown)"} not active '
                'on DO account at renewal time'))
    except Exception:
        logger.exception(
            'auto-cancel of hosting sub %s failed', sub_id)
        return

    # Alert the admin so the offboarding isn't silent.
    try:
        from django.conf import settings as _s
        from django.core.mail import send_mail
        send_mail(
            subject=(f'[Hosting auto-cancelled] {client.firm_name} — '
                     f'droplet missing at renewal'),
            message=(
                f'The annual hosting subscription for {client.firm_name} '
                f'has been cancelled because their Droplet is no longer '
                f'on our DigitalOcean account.\n\n'
                f'Subscription ID: {sub_id}\n'
                f'Client ID:       {client.id}\n'
                f'Last known droplet ID: {client.do_droplet_id or "(none)"}\n\n'
                f'Confirm this was intentional. If the Droplet should '
                f'still exist, recreate it + re-subscribe via the '
                f'admin client detail page.\n'),
            from_email=getattr(
                _s, 'EMAIL_FROM_NO_REPLY', _s.DEFAULT_FROM_EMAIL),
            recipient_list=[_s.LEAD_NOTIFICATION_EMAIL],
            fail_silently=True,
        )
    except Exception:
        logger.exception('admin alert for hosting auto-cancel failed')


def _droplet_alive(client):
    """
    True if the client's Droplet is recorded AND DigitalOcean still
    reports it as active. False if the Droplet ID is missing OR the
    DO API can't find it.

    Best-effort: a transient DO API outage returns True (assume alive)
    so we don't accidentally cancel a real client's subscription over
    a temporary network blip. The daily reconcile cron catches any
    drift the next morning.
    """
    if not client.do_droplet_id:
        return False
    try:
        import requests
        from django.conf import settings as _s
        resp = requests.get(
            f'https://api.digitalocean.com/v2/droplets/{client.do_droplet_id}',
            headers={
                'Authorization': f'Bearer {_s.DO_API_TOKEN}',
            },
            timeout=10,
        )
        if resp.status_code == 404:
            return False                # explicitly gone
        if resp.status_code >= 500:
            # DO is having a moment — assume alive (safer than a
            # false-cancel during an outage).
            logger.warning(
                'DO API %d on droplet %s status check — assuming alive',
                resp.status_code, client.do_droplet_id)
            return True
        resp.raise_for_status()
        status = (resp.json().get('droplet') or {}).get('status', '')
        return status in ('active', 'new')
    except Exception:
        logger.exception(
            'DO droplet status check failed for client %s — '
            'assuming alive', client.pk)
        return True


# ── _invoice_has_hosting helper (used by payment_intent.succeeded) ──────────

def _invoice_has_hosting(invoice):
    """
    Whether the OnboardingInvoice included a hosting line item.

    Line items are stored as a list of {'description': ..., 'amount': ...}
    on the OnboardingInvoice row. A hosting line is anything whose
    description contains the word 'hosting' (case-insensitive). Robust
    enough for the current product set; if we add new line types we
    can switch to an explicit `kind` field on each line item.
    """
    for item in (invoice.line_items or []):
        if 'hosting' in (item.get('description') or '').lower():
            return True
    return False


# ── Domain subscription helpers ─────────────────────────────────────────────

def _maybe_handle_domain_renewal(client, sub_id, invoice):
    """
    Handle the `invoice.paid` event for a domain subscription:
      1. Look up the matching DomainRegistration
      2. Call Namecheap renew (we already charged the client, so we
         owe the registration regardless)
      3. Push expires_at forward 365 days on success

    Returns True if this WAS a domain-sub invoice and we handled it
    (or attempted to), False if it isn't a domain sub at all.
    """
    try:
        from domains.models import DomainRegistration
        reg = DomainRegistration.objects.filter(
            stripe_subscription_id=sub_id, client=client).first()
    except Exception:
        logger.exception(
            'invoice.paid: domain lookup failed for sub %s', sub_id)
        return False
    if reg is None:
        return False

    from datetime import timedelta

    from domains.namecheap_client import NamecheapError, get_client

    nc = get_client()
    try:
        result = nc.renew_domain(reg.domain_name, years=1)
        if result.get('renewed'):
            reg.expires_at = (
                (reg.expires_at or timezone.now())
                + timedelta(days=365))
            reg.last_api_error = ''
            reg.last_api_call_at = timezone.now()
            reg.save(update_fields=[
                'expires_at', 'last_api_error',
                'last_api_call_at', 'updated_at',
            ])
            logger.info(
                'invoice.paid (domain): %s renewed for client %s',
                reg.domain_name, client.pk)
        else:
            reg.last_api_error = (
                'Namecheap reported renewal not completed'
            )[:2000]
            reg.save(update_fields=['last_api_error', 'updated_at'])
            _alert_admin_domain_renewal_failed(reg)
    except NamecheapError as exc:
        reg.last_api_error = f'renew: {exc}'[:2000]
        reg.save(update_fields=['last_api_error', 'updated_at'])
        _alert_admin_domain_renewal_failed(reg)
    except Exception:
        logger.exception(
            'invoice.paid (domain): renew call failed for %s',
            reg.domain_name)

    return True


def _domain_renewal_gate(sub_id):
    """
    Pre-renewal gate for domain subscriptions. If the client has
    cancelled or the domain is no longer on our Namecheap account
    (transferred out, etc.), cancel the Stripe sub at period end so
    the renewal invoice never generates.

    Returns True if `sub_id` IS a domain sub and we've handled it,
    False if it isn't.
    """
    try:
        from domains.models import DomainRegistration
        reg = DomainRegistration.objects.filter(
            stripe_subscription_id=sub_id).first()
    except Exception:
        logger.exception(
            'invoice.upcoming: domain lookup failed for sub %s',
            sub_id)
        return False
    if reg is None:
        return False

    # If client already cancelled it (status=grace, expired, etc.),
    # cancel the sub so no renewal fires.
    if reg.status != 'active':
        try:
            stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
            logger.info(
                'invoice.upcoming (domain): cancelling %s renewal '
                '(local status=%s)', reg.domain_name, reg.status)
        except Exception:
            logger.exception(
                'invoice.upcoming (domain): cancel failed for %s',
                sub_id)
        return True

    # Status is active locally — confirm the domain is still on our
    # NC account before we let the renewal charge fire.
    try:
        from domains.namecheap_client import NamecheapError, get_client
        nc = get_client()
        info = nc.get_info(reg.domain_name)
        if not info.get('is_owner', False):
            stripe.Subscription.modify(
                sub_id, cancel_at_period_end=True,
                metadata={
                    'cancel_reason':
                        f'invoice.upcoming gate: {reg.domain_name} '
                        f'no longer on Namecheap account',
                },
            )
            reg.status = 'transferred_out'
            reg.save(update_fields=['status', 'updated_at'])
            _alert_admin_domain_gate_cancel(reg, 'transferred out')
    except NamecheapError as exc:
        # Transient NC error → assume domain still ours (safer than
        # accidentally cancelling a real renewal). Daily reconcile
        # cron catches actual drift.
        logger.warning(
            'invoice.upcoming (domain): NC check failed for %s '
            '(%s) — letting renewal proceed', reg.domain_name, exc)
    except Exception:
        logger.exception(
            'invoice.upcoming (domain): gate failed for %s',
            reg.domain_name)
    return True


def _park_client_domains_on_hosting_cancel(client):
    """
    Re-point every active domain the client owns at our parking page
    when hosting is cancelled. Without this, visitors hit a dead
    Droplet IP. Best-effort: never raise — just log.
    """
    try:
        from domains.models import DomainRegistration
        from domains.services import park_domain
        regs = list(DomainRegistration.objects.filter(
            client=client, status='active'))
        for reg in regs:
            try:
                park_domain(reg)
                logger.info(
                    'hosting cancel: parked domain %s',
                    reg.domain_name)
            except Exception:
                logger.exception(
                    'hosting cancel: park failed for %s — admin '
                    'will need to manually park',
                    reg.domain_name)
    except Exception:
        logger.exception(
            'hosting cancel: bulk domain park step failed '
            'for client %s', client.pk)


def _alert_admin_domain_renewal_failed(registration):
    """Email admin so a failed renewal doesn't go silent."""
    try:
        from django.conf import settings as _s
        from django.core.mail import send_mail
        send_mail(
            subject=(
                f'[Domain renewal failed] {registration.domain_name} '
                f'({registration.client.firm_name})'),
            message=(
                f'Stripe charged the client for the renewal of '
                f'{registration.domain_name}, but the Namecheap renew '
                f'call failed.\n\n'
                f'Last API error: {registration.last_api_error}\n\n'
                f'Client: {registration.client.firm_name} '
                f'<{registration.client.user.email if registration.client.user else "(no user)"}>\n'
                f'Registration ID: {registration.id}\n\n'
                f'Manually renew on Namecheap, or refund the client.\n'),
            from_email=getattr(
                _s, 'EMAIL_FROM_NO_REPLY', _s.DEFAULT_FROM_EMAIL),
            recipient_list=[_s.LEAD_NOTIFICATION_EMAIL],
            fail_silently=True,
        )
    except Exception:
        logger.exception('admin alert for domain renewal failed')


def _alert_admin_domain_gate_cancel(registration, reason):
    """Email admin when invoice.upcoming gate auto-cancels a domain."""
    try:
        from django.conf import settings as _s
        from django.core.mail import send_mail
        send_mail(
            subject=(
                f'[Domain auto-cancelled] {registration.domain_name} '
                f'({reason})'),
            message=(
                f'The Stripe subscription for '
                f'{registration.domain_name} has been cancelled '
                f'because the domain is {reason} from our Namecheap '
                f'account.\n\n'
                f'Client: {registration.client.firm_name}\n'
                f'Registration ID: {registration.id}\n'),
            from_email=getattr(
                _s, 'EMAIL_FROM_NO_REPLY', _s.DEFAULT_FROM_EMAIL),
            recipient_list=[_s.LEAD_NOTIFICATION_EMAIL],
            fail_silently=True,
        )
    except Exception:
        logger.exception('admin alert for domain gate cancel failed')


def _invoice_lines_mention_maintenance(stripe_invoice_dict):
    """
    Last-resort hint: does any line item on the (Stripe-shaped)
    invoice dict contain the word "maintenance"? Used by the
    `invoice.paid` handler when we receive a subscription invoice
    whose ID doesn't match either of our recorded references.
    """
    lines = ((stripe_invoice_dict.get('lines') or {}).get('data') or [])
    for line in lines:
        desc = (line.get('description') or '').lower()
        price_nick = ((line.get('price') or {}).get('nickname') or '').lower()
        meta_plan = ((line.get('metadata') or {}).get('plan') or '').lower()
        haystack = f'{desc} {price_nick} {meta_plan}'
        if 'maintenance' in haystack:
            return True
    return False


# ── customer.subscription.updated ───────────────────────────────────────────

# Map the Stripe Price ID back to the local PACKAGE_CHOICES code so
# tier changes initiated in the Stripe Dashboard (or via the portal)
# keep ClientProfile.package + maintenance_active in sync.
def _price_id_to_local_package(price_id):
    if not price_id:
        return ''
    from billing.pricing_models import ServiceTier
    from billing.stripe_helpers import MAINTENANCE_TIER_TO_PACKAGE
    tier = ServiceTier.objects.filter(
        stripe_price_id=price_id, category='maintenance').first()
    if tier is None:
        return ''
    return MAINTENANCE_TIER_TO_PACKAGE.get(tier.slug, '')


def _handle_subscription_updated(event):
    """
    Reconcile local maintenance fields whenever the subscription is
    modified — covers tier upgrades/downgrades, status changes
    (active ↔ past_due), and cancel-at-period-end toggles. Hosting
    subs are skipped (their lifecycle is fully handled elsewhere).
    """
    subscription = event['data']['object']
    sub_id = subscription.get('id', '')
    client = _client_for_customer(subscription.get('customer'))
    if client is None or sub_id != client.stripe_subscription_id:
        # Only react to the client's MAINTENANCE subscription. Hosting +
        # any third-party subs the customer might have are ignored.
        return

    status = subscription.get('status', '')
    items = ((subscription.get('items') or {}).get('data') or [])
    price_id = ''
    if items:
        price_id = ((items[0].get('price') or {}).get('id') or '')

    fields = ['updated_at']
    if status in ('active', 'trialing'):
        if not client.maintenance_active:
            client.maintenance_active = True
            fields.append('maintenance_active')
        if not client.maintenance_started_at:
            client.maintenance_started_at = timezone.now()
            fields.append('maintenance_started_at')
    elif status in ('past_due', 'unpaid'):
        # Keep maintenance_active True for the grace window — Stripe
        # retries automatically. We'll flip on `subscription.deleted`
        # if it ultimately ends.
        pass

    new_package = _price_id_to_local_package(price_id)
    if new_package and new_package != client.package:
        client.package = new_package
        fields.append('package')

    if fields != ['updated_at']:
        client.save(update_fields=fields)
        logger.info(
            'subscription.updated: client %s status=%s package=%s',
            client.pk, status, client.package)


# ── Phase 6 — self-checkout magic-link bootstrap ─────────────────────

def _handle_self_checkout_subscription_created(event):
    """
    Triggered by every customer.subscription.created. For subscriptions
    that came from our custom checkout flow (metadata.tier_slug present),
    we:
      1. Find or create a User by email
      2. Create an Onboarding for the (user, product_type, tier_slug)
      3. Mint a PasswordSetupToken and email the magic link

    Idempotent — re-running the same event is a no-op after the first
    pass because Onboarding has unique_together(user, product_type,
    tier_slug) and PasswordSetupToken creation is allowed (a fresh
    token just supersedes the prior one until used).
    """
    from django.contrib.auth import get_user_model

    sub = event.get('data', {}).get('object', {})
    metadata = sub.get('metadata') or {}
    tier_slug = metadata.get('tier_slug') or ''
    product_type = metadata.get('product_type') or ''
    customer_id = sub.get('customer')
    if not (tier_slug and product_type and customer_id):
        return  # not a self-checkout subscription

    # Look up email from the Stripe customer
    import stripe as _stripe
    _stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        cust = _stripe.Customer.retrieve(customer_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            'self-checkout webhook: customer retrieve failed for %s',
            customer_id)
        return
    email = (cust.get('email') or '').lower()
    if not email:
        return

    User = get_user_model()
    user, created = User.objects.get_or_create(
        email__iexact=email,
        defaults={
            'username': email,
            'email': email,
            'is_active': True,
        },
    )
    if created:
        user.set_unusable_password()
        user.save(update_fields=['password'])

    # ── BC1 — auto-create ClientProfile + Account ────────────────
    # Without these the portal sidebar + many views 500 because
    # they expect ``request.user.client_profile`` and
    # ``request.user.account`` to exist. This is a stop-gap until
    # Phase D lands a proper service-model split.
    try:
        from clients.models import ClientProfile
        from clients.account_models import Account

        # Derive a firm_name + display name. Stripe Customer often has
        # a `name` set during checkout; fall back to the email local part.
        derived_name = (cust.get('name') or '').strip()
        if not derived_name:
            local = email.split('@', 1)[0]
            derived_name = local.replace('.', ' ').replace('-', ' ').title()
        # ClientProfile is the legacy "everything" model; we only
        # populate the fields that genuinely identify the person.
        cp, cp_created = ClientProfile.objects.get_or_create(
            user=user,
            defaults={
                'firm_name': derived_name,
                'contact_name': derived_name,
                'status': 'active',
                'stripe_customer_id': customer_id,
                # No `package` set — Phase D's MaintenancePlan /
                # SocialMediaPlan rows will encode what they bought.
                # The legacy package field stays blank for self-
                # checkout customers so we don't lie about their
                # status with the old single-product enum.
            },
        )
        # Backfill stripe_customer_id even if the profile already
        # existed (e.g. they re-purchased a different product).
        if not cp_created and not cp.stripe_customer_id:
            cp.stripe_customer_id = customer_id
            cp.save(update_fields=['stripe_customer_id'])

        Account.objects.get_or_create(
            user=user,
            defaults={
                'name': derived_name,
                'status': 'active',
                'stripe_customer_id': customer_id,
                # legacy_client_profile mirror so existing admin
                # tools that resolve via ClientProfile still work.
                'legacy_client_profile': cp,
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            'self-checkout webhook: ClientProfile/Account create failed '
            'for user %s', user.pk)
        try:
            from core.system_alerts import record_alert
            record_alert(
                severity='error',
                source='billing.webhook.profile_create',
                message=f'ClientProfile/Account auto-create failed for {email}',
                detail='Customer paid but portal will 500 — investigate.',
            )
        except Exception:
            pass

    # Create Onboarding (idempotent on unique_together)
    try:
        from onboarding.models import Onboarding
        Onboarding.objects.get_or_create(
            user=user, product_type=product_type, tier_slug=tier_slug,
        )
    except Exception:
        logger.exception('self-checkout webhook: Onboarding create failed')

    # ── D6 — write the per-service plan row ─────────────────────────
    # Phase D's MaintenancePlan / SocialMediaPlan replaces the legacy
    # ClientProfile.package field. We write both for now (D7 strips
    # the legacy field).
    try:
        from clients.account_models import Account
        from clients.service_models import (
            MaintenancePlan, SocialMediaPlan)
        from billing.pricing_models import ServiceTier
        from django.utils import timezone

        account = Account.objects.filter(user=user).first()
        if account is not None:
            sub_id = sub.get('id') or ''
            if product_type == 'maintenance':
                MaintenancePlan.objects.update_or_create(
                    account=account,
                    stripe_subscription_id=sub_id,
                    defaults={
                        'tier_slug': tier_slug,
                        'status': 'active',
                        'started_at': timezone.now(),
                        'hosting_move_over': metadata.get(
                            'hosting_upsell') == '1',
                        'hosting_move_over_at': timezone.now() if
                            metadata.get('hosting_upsell') == '1' else None,
                    },
                )
            elif product_type == 'social_media':
                tier = ServiceTier.objects.filter(slug=tier_slug).first()
                max_ch = (tier.max_channels if tier and tier.max_channels
                          else 2)
                SocialMediaPlan.objects.update_or_create(
                    account=account,
                    stripe_subscription_id=sub_id,
                    defaults={
                        'tier_slug': tier_slug,
                        'status': 'active',
                        'started_at': timezone.now(),
                        'max_channels': max_ch,
                    },
                )
    except Exception:  # noqa: BLE001
        logger.exception(
            'self-checkout webhook: service-model write failed '
            'for user %s', user.pk)

    # Mint a password setup token + email it (only if newly created).
    # Failure is recorded but NOT swallowed silently — exception
    # propagates to the outer handler, which logs it AND records a
    # SystemAlert so the operator sees it on the dashboard banner.
    if created:
        from onboarding.password_views import send_password_setup_email
        try:
            send_password_setup_email(user)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                'self-checkout webhook: password email failed for %s', email)
            try:
                from core.system_alerts import record_alert
                record_alert(
                    severity='error',
                    source='billing.webhook.password_email',
                    message=f'Password-setup email failed for {email}',
                    detail=str(exc)[:2000],
                )
            except Exception:
                pass
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1.6 — Reinstatement helper
# ─────────────────────────────────────────────────────────────────────────────

def _handle_reinstatement(client):
    """Called from _handle_invoice_paid when the paying client has a
    non-null payment_failure_started_at. Steps:

      1. Increment payment_failure_offenses (lifetime counter).
      2. If offenses >= 2: charge $75 fee off-session.
            - Fee charge SUCCESS → continue to restore.
            - Fee charge FAILURE → SystemAlert (critical), DO NOT
              restore. Admin must chase the client for a new card.
      3. restore_client_site(client) — power on / rebuild from
         snapshot / clear maintenance vhost.
      4. Null payment_failure_started_at — this is what makes every
         already-scheduled escalation task no-op when they fire.

    Idempotent on re-delivery: if started_at is already None we
    bail early at the caller, so this helper isn't entered twice
    for the same reinstatement.
    """
    from billing.do_helpers import restore_client_site
    from billing.stripe_helpers import (
        StripeNotConfigured,
        charge_reinstatement_fee,
    )

    client.payment_failure_offenses = (
        (client.payment_failure_offenses or 0) + 1)

    if client.payment_failure_offenses >= 2:
        try:
            pi = charge_reinstatement_fee(client)
        except StripeNotConfigured:
            pi = None
        if pi is None:
            logger.error(
                '_handle_reinstatement: reinstatement-fee charge failed '
                'for client %s (offense %s) — NOT restoring',
                client.pk, client.payment_failure_offenses)
            try:
                from core.system_alerts import record_alert
                record_alert(
                    severity='critical',
                    source='billing.webhook.reinstatement_fee',
                    message=(f'Reinstatement fee charge failed for '
                             f'client {client.pk} — manual recovery '
                             f'needed'),
                    detail=(f'offense={client.payment_failure_offenses}; '
                            f'check Stripe dashboard for the failed '
                            f'PaymentIntent'),
                )
            except Exception:
                pass
            # Save the offense increment even if restoration is
            # blocked — we don't want a free 2nd offense if the
            # admin manually fixes things later.
            client.save(update_fields=[
                'payment_failure_offenses', 'updated_at'])
            return

    # Restore the site state. restore_client_site logs + alerts on
    # its own failures.
    try:
        restore_client_site(client)
    except Exception:
        logger.exception(
            '_handle_reinstatement: restore_client_site raised for %s',
            client.pk)

    # Cancel the in-flight escalation chain by nulling the guard.
    # The 14/21/30/60-day tasks check this field on fire and no-op
    # when it's None.
    client.payment_failure_started_at = None
    client.save(update_fields=[
        'payment_failure_started_at',
        'payment_failure_offenses',
        'updated_at',
    ])
    logger.info(
        '_handle_reinstatement: client %s reinstated (offense %s)',
        client.pk, client.payment_failure_offenses)
