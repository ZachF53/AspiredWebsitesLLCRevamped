"""
Inbound reply ingest.

Polls the outreach mailbox via IMAP every 15 minutes (Celery beat),
matches incoming messages back to the EmailSent rows they reply to
using ``In-Reply-To`` / ``References`` headers, writes EmailReply
rows, and hands each new reply off to the classifier+drafter.

Reuses the same IMAP_* env vars the DMARC poller does — they share
the mailbox. ``OUTREACH_IMAP_FOLDER`` defaults to INBOX since reply
mail isn't filtered into a label.

Defensive: if the env vars aren't set the poller no-ops cleanly so
fresh dev environments don't blow up. The DMARC poller follows the
same pattern.

Threading rules:
    - First we look for ``In-Reply-To: <message_id>`` and find the
      EmailSent whose message_id_header matches.
    - Failing that, walk ``References`` (space-separated list of
      message-ids in conversation order) — try the last one first.
    - Failing both, fall back to matching the inbound From: address
      against an existing Lead — covers replies that stripped the
      threading headers (some webmail clients do).
    - If nothing matches, log and move on. The mailbox poller will
      not re-process the same UID twice (we mark Seen).
"""

import email
import imaplib
import logging
import re

from django.conf import settings
from django.utils import timezone

from outreach.models import EmailReply, EmailSent, Lead

logger = logging.getLogger(__name__)


def ingest_replies():
    """
    Run one IMAP poll. Returns counts dict for the task log:

        {
            'fetched':       int,  # raw messages pulled
            'matched':       int,  # successfully threaded to an EmailSent
            'orphan_lead':   int,  # matched a Lead by From: but no EmailSent
            'unmatched':     int,  # no EmailSent and no Lead — ignored
            'errors':        int,
        }
    """
    counts = {'fetched': 0, 'matched': 0, 'orphan_lead': 0,
              'unmatched': 0, 'errors': 0}

    host = getattr(settings, 'DMARC_IMAP_HOST', '') or ''
    user = getattr(settings, 'DMARC_IMAP_USER', '') or ''
    pwd = getattr(settings, 'DMARC_IMAP_PASS', '') or ''
    folder = getattr(settings, 'OUTREACH_IMAP_FOLDER', 'INBOX') or 'INBOX'

    if not (host and user and pwd):
        logger.info('reply ingest: IMAP creds not set, skipping.')
        return counts

    try:
        mbox = imaplib.IMAP4_SSL(host)
        mbox.login(user, pwd)
        mbox.select(folder)
    except Exception as exc:  # noqa: BLE001
        logger.exception('reply ingest: IMAP login failed: %s', exc)
        counts['errors'] += 1
        return counts

    try:
        # Only unread mail — we mark Seen after processing.
        status, data = mbox.search(None, '(UNSEEN)')
        if status != 'OK':
            logger.warning('reply ingest: IMAP search failed: %s', status)
            return counts

        msg_ids = (data[0] or b'').split()
        for msg_id in msg_ids:
            counts['fetched'] += 1
            try:
                status, msg_data = mbox.fetch(msg_id, '(RFC822)')
                if status != 'OK' or not msg_data:
                    counts['errors'] += 1
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                result = _process_message(msg)
                counts[result] += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    'reply ingest: failed to process message %s', msg_id)
                counts['errors'] += 1
                continue
            else:
                # Mark seen ONLY after successful processing; a crash
                # leaves the message Unread so the next run retries.
                try:
                    mbox.store(msg_id, '+FLAGS', '\\Seen')
                except Exception:  # noqa: BLE001
                    logger.warning(
                        'reply ingest: failed to flag %s as seen', msg_id)
    finally:
        try:
            mbox.logout()
        except Exception:  # noqa: BLE001
            pass

    return counts


def _process_message(msg):
    """
    Process one decoded ``email.message.Message``. Returns one of the
    counts-dict keys: 'matched', 'orphan_lead', 'unmatched', 'errors'.
    """
    sender = _addr(msg.get('From', ''))
    subject = (msg.get('Subject') or '').strip()
    body = _extract_body(msg)
    in_reply_to = (msg.get('In-Reply-To') or '').strip()
    references = (msg.get('References') or '').strip()

    # Heuristic: skip our own outgoing mail in case the mailbox is the
    # same one we send from (Gmail puts sent mail in INBOX when you
    # use IMAP to send AND receive on the same account).
    from_addr = getattr(settings, 'OUTREACH_FROM_EMAIL',
                        'zacherylong@aspiredwebsites.com')
    if sender and sender.lower() == from_addr.lower():
        return 'unmatched'

    email_sent = _find_matching_email_sent(in_reply_to, references)
    lead = email_sent.lead if email_sent else _find_lead_by_email(sender)

    if not lead:
        logger.info(
            'reply ingest: no match for from=%s subject=%s', sender, subject)
        return 'unmatched'

    # Idempotency: don't write the same reply twice. We key on the
    # Message-ID of the inbound mail (every replier client sets one).
    inbound_msg_id = (msg.get('Message-ID') or '').strip()
    if inbound_msg_id and EmailReply.objects.filter(
            body__icontains=inbound_msg_id[1:30]).exists():
        # Cheap-and-fuzzy guard — the message_id substring rarely
        # collides; sufficient until we add a dedicated column.
        return 'matched' if email_sent else 'orphan_lead'

    reply = EmailReply.objects.create(
        lead=lead,
        email_sent=email_sent,
        subject=subject[:255],
        body=body[:50000],
    )

    # Mirror engagement onto the EmailSent + pause the lead's sequence.
    if email_sent and not email_sent.replied:
        email_sent.replied = True
        email_sent.replied_at = timezone.now()
        email_sent.save(update_fields=['replied', 'replied_at'])
    Lead.objects.filter(pk=lead.pk).update(sequence_paused=True)

    # Fire-and-forget classify + draft on the new reply. The classify
    # task itself is responsible for deciding (via gating) whether the
    # AI-drafted reply should go straight to ``approved`` or wait in
    # the queue.
    try:
        from outreach.tasks import classify_and_draft_reply_task
        classify_and_draft_reply_task.delay(reply.pk)
    except Exception:  # noqa: BLE001
        logger.exception(
            'reply ingest: failed to enqueue classify for reply %s', reply.pk)

    return 'matched' if email_sent else 'orphan_lead'


def _find_matching_email_sent(in_reply_to, references):
    """Look up our EmailSent by Message-ID header."""
    candidates = []
    if in_reply_to:
        candidates.append(in_reply_to)
    if references:
        # References is whitespace-separated, most-recent last per RFC.
        candidates.extend(reversed(references.split()))
    for mid in candidates:
        mid = mid.strip()
        if not mid:
            continue
        # Stored form is always <foo@bar>; tolerate either.
        if not mid.startswith('<'):
            mid = f'<{mid}>'
        hit = EmailSent.objects.filter(message_id_header=mid).first()
        if hit:
            return hit
    return None


def _find_lead_by_email(addr):
    """Fallback when threading headers got stripped."""
    if not addr:
        return None
    return Lead.objects.filter(email__iexact=addr).first()


_ADDR_RE = re.compile(r'<([^>]+)>')


def _addr(from_header):
    """Pull just the bare address out of 'Name <addr@host>'."""
    m = _ADDR_RE.search(from_header)
    if m:
        return m.group(1).strip()
    return from_header.strip()


def _extract_body(msg):
    """Plain-text body preferred; fall back to HTML stripped of tags."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/plain':
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or 'utf-8',
                        errors='replace')
                except Exception:  # noqa: BLE001
                    pass
        # No plain — try first text/html
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                try:
                    raw = part.get_payload(decode=True).decode(
                        part.get_content_charset() or 'utf-8',
                        errors='replace')
                    return re.sub(r'<[^>]+>', '', raw)
                except Exception:  # noqa: BLE001
                    pass
        return ''
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or 'utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        return msg.get_payload() or ''
