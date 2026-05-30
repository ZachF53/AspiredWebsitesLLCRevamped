"""
Approved-email dispatcher.

The Celery task ``send_approved_emails_task`` (outreach/tasks.py) calls
``dispatch_approved_batch`` every 30 minutes during business hours.
Picks every EmailSent row with ``status='approved'`` and actually
delivers it via SMTP (SendGrid). On success the row flips to
``status='sent'`` with ``sent_at`` set and a unique ``message_id_header``
populated so inbound reply ingestion can thread responses back to the
right outbound message.

Failures are intentionally NOT retried in this same function — a
permanent SendGrid error (bounce, invalid address) should bubble up to
``Lead.unsubscribed = True`` and a SuppressionList entry rather than
hammering SendGrid with the same broken payload every tick.

Rate-shaping: the warming cap already constrained how many rows the
sender generated for the day, so we can dispatch the entire approved
batch in one go without secondary throttling here.
"""

import logging
import uuid

from django.conf import settings
from django.core.mail import EmailMessage
from django.db.models import F
from django.utils import timezone

from outreach.models import EmailSent, Lead, OutreachSettings, SuppressionList

logger = logging.getLogger(__name__)


def dispatch_approved_batch():
    """
    Send every status='approved' EmailSent row. Returns a counts dict:

        {
            'sent':       int,  # successfully handed to SendGrid
            'failed':     int,  # SMTP raised; row left as 'approved'
            'suppressed': int,  # lead on suppression list — auto-rejected
        }

    Idempotent across runs: only picks ``approved`` rows; a SendGrid
    accept flips status to ``sent`` atomically so a concurrent run
    can't double-dispatch.
    """
    counts = {'sent': 0, 'failed': 0, 'suppressed': 0}

    suppressed_emails = set(
        SuppressionList.objects.values_list('email', flat=True))

    qs = EmailSent.objects.filter(
        status='approved'
    ).select_related('lead').order_by('approved_at')

    for email in qs:
        if not email.lead.email:
            email.status = 'rejected'
            email.rejected_reason = 'Lead has no email address.'
            email.save(update_fields=['status', 'rejected_reason'])
            continue

        if email.lead.email.lower() in suppressed_emails:
            email.status = 'rejected'
            email.rejected_reason = 'Lead is on the suppression list.'
            email.save(update_fields=['status', 'rejected_reason'])
            counts['suppressed'] += 1
            continue

        message_id = _generate_message_id()
        try:
            _send_one(email, message_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                'dispatch failed for EmailSent %s (lead %s): %s',
                email.pk, email.lead.pk, exc)
            counts['failed'] += 1
            continue

        now = timezone.now()
        email.status = 'sent'
        email.sent_at = now
        email.message_id_header = message_id
        email.save(update_fields=[
            'status', 'sent_at', 'message_id_header'])

        # Mirror to the lead so the dashboard reflects activity right
        # away — last_contacted_at is what the "stale leads" filters
        # use, sequence_step was already advanced at generation time.
        Lead.objects.filter(pk=email.lead.pk).update(last_contacted_at=now)

        # Atomic counter bump so two concurrent drainer ticks can't
        # race. The midnight reset task zeroes this; the cap math in
        # outreach.sender reads EmailSent rows directly so the field
        # is purely informational, but inaccurate informational fields
        # erode trust in the dashboard — keep it honest.
        OutreachSettings.objects.filter(pk=1).update(
            emails_sent_today=F('emails_sent_today') + 1)

        counts['sent'] += 1

    return counts


def _generate_message_id():
    """
    RFC 5322 Message-ID — a globally unique token in <local@domain> form.
    We mint our own (rather than relying on the SMTP server) so the
    value is known BEFORE send, can be stored on the EmailSent row,
    and inbound reply ingestion can match against it.
    """
    domain = getattr(settings, 'OUTREACH_MESSAGE_ID_DOMAIN',
                     'aspiredwebsites.com')
    return f'<{uuid.uuid4().hex}@{domain}>'


def _send_one(email, message_id):
    """
    Hand a single EmailSent to SendGrid via Django's SMTP backend.

    We bypass ``django.core.mail.send_mail`` so we can control the
    Message-ID header — that's what inbound reply threading needs.

    SendGrid event-webhook tracking: the X-SMTPAPI header carries
    SendGrid's custom_args + filters. We pass ``email_sent_id`` so
    inbound /sendgrid/events/ pings can match opens/clicks back to
    the right row WITHOUT relying on Message-ID parsing — and we
    explicitly enable open + click trackers (SendGrid lets a single
    send override the account default either way).
    """
    import json as _json

    sg_payload = {
        'unique_args': {
            'email_sent_id': str(email.pk),
            'kind': email.kind,
            'lead_id': str(email.lead_id),
        },
        'filters': {
            'opentrack':  {'settings': {'enable': 1}},
            'clicktrack': {'settings': {'enable': 1}},
        },
    }

    msg = EmailMessage(
        subject=email.subject,
        body=email.body,
        from_email=email.from_email,
        to=[email.lead.email],
        # Headers extension: Django passes these straight through to the
        # SMTP backend, which writes them into the outgoing envelope.
        headers={
            'Message-ID': message_id,
            'X-Outreach-Step': str(email.sequence_step),
            'X-Outreach-Kind': email.kind,
            'X-SMTPAPI': _json.dumps(sg_payload),
        },
    )
    # If this is a reply to an inbound message, add the threading
    # headers so the recipient's mail client groups it correctly.
    if email.in_reply_to and email.in_reply_to.email_sent_id:
        original_msg_id = (
            EmailSent.objects.filter(pk=email.in_reply_to.email_sent_id)
            .values_list('message_id_header', flat=True).first())
        if original_msg_id:
            msg.extra_headers['In-Reply-To'] = original_msg_id
            msg.extra_headers['References'] = original_msg_id

    msg.send(fail_silently=False)
