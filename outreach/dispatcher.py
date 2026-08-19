"""
Approved-email dispatcher.

The Celery task ``send_approved_emails_task`` (outreach/tasks.py) calls
``dispatch_approved_batch`` every 30 minutes during business hours.
Picks every EmailSent row with ``status='approved'`` and actually
delivers it via SMTP (SendGrid). On success the row flips to
``status='sent'`` with ``sent_at`` set and a unique ``message_id_header``
populated so inbound reply ingestion can thread responses back to the
right outbound message.

This module is also the ONLY place that advances a cold lead's
``sequence_step`` / ``next_followup_at``. The generator deliberately
leaves both alone so an unapproved draft never starts the follow-up
clock — see the module docstring in ``outreach/sender.py``.

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

from outreach.copy_guard import (
    describe_copy_problems,
    describe_pricing_problems,
)
from outreach.models import EmailSent, Lead, OutreachSettings, SuppressionList
# sender.py owns the cadence table; it does not import dispatcher, so this
# direction is cycle-free.
from outreach.sender import _next_followup_at
from outreach.variant_rotation import record_send

logger = logging.getLogger(__name__)


def dispatch_approved_batch():
    """
    Send every status='approved' EmailSent row. Returns a counts dict:

        {
            'sent':              int,  # handed to SendGrid
            'failed':            int,  # TRANSIENT; row stays 'approved'
            'permanent_failure': int,  # undeliverable; rejected + suppressed
            'suppressed':        int,  # lead already on suppression list
            'blocked':           int,  # copy failed validation
        }

    Idempotent across runs: only picks ``approved`` rows; a SendGrid
    accept flips status to ``sent`` atomically so a concurrent run
    can't double-dispatch.
    """
    counts = {'sent': 0, 'failed': 0, 'suppressed': 0, 'blocked': 0,
              'permanent_failure': 0}

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

        # Last line of defense before SMTP. The generator validates copy
        # too, but this catches anything approved before that validation
        # existed, hand-edited in the admin, or produced by a future code
        # path that forgets to check. Ten refusal messages reached real
        # prospects because nothing looked at the body on the way out.
        problems = describe_copy_problems(email.subject, email.body)
        # §1.1 pricing guardrail. Separate call because it reads the DB
        # (active ServiceTier rows) while describe_copy_problems stays
        # pure. Both feed the same rejection path — an invented price is
        # a quote we would be on the hook for.
        problems += describe_pricing_problems(email.body, email.subject)
        if problems:
            email.status = 'rejected'
            email.rejected_reason = (
                'Blocked by copy validation: ' + '; '.join(problems))[:500]
            email.save(update_fields=['status', 'rejected_reason'])
            logger.error(
                'dispatch BLOCKED EmailSent %s to %s — %s',
                email.pk, email.lead.email, '; '.join(problems))
            counts['blocked'] += 1
            continue

        message_id = _generate_message_id()
        try:
            _send_one(email, message_id)
        except Exception as exc:  # noqa: BLE001
            # This module's docstring has always claimed a permanent
            # failure should stop rather than hammer SendGrid every tick.
            # It never actually did: EVERY failure left the row on
            # 'approved', so a genuinely undeliverable address was retried
            # every 30 minutes forever — burning sender reputation on a
            # payload that cannot succeed. Split the two cases.
            if _is_permanent_failure(exc):
                email.status = 'rejected'
                email.rejected_reason = (
                    f'Permanent delivery failure: {exc}')[:500]
                email.save(update_fields=['status', 'rejected_reason'])
                _suppress_bad_address(email.lead, exc)
                logger.error(
                    'dispatch PERMANENT failure for EmailSent %s (lead %s, '
                    '%s) — rejected, address suppressed: %s',
                    email.pk, email.lead.pk, email.lead.email, exc)
                counts['permanent_failure'] += 1
            else:
                logger.exception(
                    'dispatch failed (transient) for EmailSent %s (lead %s): '
                    '%s — will retry next tick', email.pk, email.lead.pk, exc)
                counts['failed'] += 1
            continue

        now = timezone.now()
        email.status = 'sent'
        email.sent_at = now
        email.message_id_header = message_id
        email.save(update_fields=[
            'status', 'sent_at', 'message_id_header'])

        # Mirror to the lead so the dashboard reflects activity right
        # away — last_contacted_at is what the "stale leads" filters use.
        lead_updates = {'last_contacted_at': now}

        # Advance the sequence pointer HERE, not at generation time.
        # This is the only place that knows the prospect actually
        # received something. Guards:
        #   * kind == 'cold'  — replies carry sequence_step=0 and must
        #     never move the cold-outreach pointer.
        #   * monotonic       — a row approved out of order (or an
        #     operator re-queueing an older step) must not walk the
        #     lead backwards.
        # TODO(§4 / Instantly): once dispatcher.py is retired in favour
        # of pushing approved rows to Instantly, this advance has no
        # synchronous send moment to hang off. Re-anchor it to the
        # Instantly "email sent" webhook — see COLD_OUTREACH_AGENT.md §4
        # step 6. Do not let it fall back to generation time.
        if email.kind == 'cold' and email.sequence_step > email.lead.sequence_step:
            lead_updates['sequence_step'] = email.sequence_step
            lead_updates['next_followup_at'] = _next_followup_at(
                email.sequence_step, now)

        Lead.objects.filter(pk=email.lead.pk).update(**lead_updates)

        # Variant stats (§2/§6). Denormalised counters on
        # EmailTemplateVariant are what the rotation reads on every run —
        # they must move when the mail actually goes out, not when it was
        # drafted, or a variant would get credit for rejected drafts.
        record_send(email.template_variant_id)

        # Atomic counter bump so two concurrent drainer ticks can't
        # race. The midnight reset task zeroes this; the cap math in
        # outreach.sender reads EmailSent rows directly so the field
        # is purely informational, but inaccurate informational fields
        # erode trust in the dashboard — keep it honest.
        OutreachSettings.objects.filter(pk=1).update(
            emails_sent_today=F('emails_sent_today') + 1)

        counts['sent'] += 1

    return counts


def _is_permanent_failure(exc):
    """True when retrying this exception can never succeed.

    Permanent = the recipient or the payload is the problem (SMTP 5xx,
    refused recipient, malformed address). Transient = the connection or
    the server is the problem (timeouts, disconnects, 4xx greylisting),
    which the next tick may well get through.

    Defaults to TRANSIENT on anything unrecognised: a wrongly-transient
    call costs one retry, a wrongly-permanent call silently drops a real
    prospect. Retrying is the cheaper mistake.
    """
    import smtplib

    # Recipient/sender outright refused — the address is the problem.
    if isinstance(exc, (smtplib.SMTPRecipientsRefused,
                        smtplib.SMTPSenderRefused,
                        smtplib.SMTPNotSupportedError)):
        return True

    # Django raises ValueError for addresses that fail header validation
    # before SMTP is even reached (newlines, empty local part, …).
    if isinstance(exc, (UnicodeEncodeError, ValueError)):
        return True

    # Any SMTP error carrying a 5xx code is permanent by definition.
    code = getattr(exc, 'smtp_code', None)
    if isinstance(code, int) and 500 <= code < 600:
        return True

    return False


def _suppress_bad_address(lead, exc):
    """Add an undeliverable address to the suppression list.

    Deliberately does NOT set ``lead.unsubscribed``. Suppression is keyed
    on the ADDRESS, so parking a bad one still lets re-enrichment find a
    working address for the same firm later. Marking the lead
    unsubscribed would throw the prospect away over a typo.

    Never raises — a bookkeeping failure must not break the drain loop.
    """
    if not lead.email:
        return
    try:
        SuppressionList.objects.update_or_create(
            email=lead.email.lower(),
            defaults={
                'domain': lead.email.split('@', 1)[-1].lower(),
                'reason': f'Undeliverable: {exc}'[:100],
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            'Failed to suppress undeliverable address for lead %s', lead.pk)


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

    # BCC the From address so the operator has a Gmail-side audit
    # trail. SendGrid relay bypasses Gmail entirely; without the BCC
    # the operator's Sent folder stays permanently empty even though
    # the dashboard knows the email went out. Toggle off via
    # OUTREACH_BCC_FROM_ADDRESS=False in .env if the inbox volume
    # gets annoying. We strip a BCC that would equal the recipient to
    # avoid double-sending in the (vanishingly rare) case the operator
    # ever ends up on their own lead list.
    bcc = []
    if getattr(settings, 'OUTREACH_BCC_FROM_ADDRESS', True):
        from_addr = email.from_email.lower()
        if email.lead.email.lower() != from_addr:
            bcc = [email.from_email]

    msg = EmailMessage(
        subject=email.subject,
        body=email.body,
        from_email=email.from_email,
        to=[email.lead.email],
        bcc=bcc,
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
