"""
ingest_dmarc_imap — poll a mailbox via IMAP, find DMARC aggregate
reports (XML attachments), and ingest them.

Auto-ingestion path. Opt-in via four env vars; runs as a no-op
otherwise so the project keeps working on environments where the
mailbox creds aren't set up.

Required env vars:
    DMARC_IMAP_HOST       = imap.gmail.com
    DMARC_IMAP_USER       = zacherylong@aspiredwebsites.com
    DMARC_IMAP_PASS       = (Gmail App Password — NOT the account password)
    DMARC_IMAP_FOLDER     = [Gmail]/All Mail

Gmail App Password setup:
    Google Account → Security → 2-Step Verification → App passwords
    → Generate one for "Mail / Other" and paste into DMARC_IMAP_PASS.

On DMARC_IMAP_FOLDER:
    Prefer ``[Gmail]/All Mail``. It sees every message regardless of
    label, archive state, or how you filed it, so the poller can't be
    starved by a filter that was never created or a label that was
    renamed. Re-ingest is free — ingest_dmarc_xml() de-dups on
    report_id — so the wider scan costs nothing but a few IMAP bytes.

    A label (e.g. ``dmarc-reports``) also works, but ONLY if a Gmail
    filter actually applies it. A label that exists but is empty makes
    this command a silent no-op — that exact misconfiguration kept the
    dashboard blank for two months. Note that Gmail filters are not
    retroactive; mail that arrived before the filter existed keeps
    whatever labels it had.

    Mailbox names containing spaces are handled — see _imap_quote().

Usage:
    python manage.py ingest_dmarc_imap                 # process unread, mark seen
    python manage.py ingest_dmarc_imap --dry-run       # report what'd happen
    python manage.py ingest_dmarc_imap --days 30       # re-scan last 30 days
    python manage.py ingest_dmarc_imap --leave-unread  # don't mark seen

Cron suggestion (Celery beat):
    Daily at 06:00 — picks up the previous day's reports as they
    accumulate.
"""

import email
import imaplib
import logging
import re
import sys
from datetime import datetime, timedelta
from email.header import decode_header, make_header

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        'Poll an IMAP mailbox for DMARC aggregate reports and ingest '
        'every attachment found. Opt-in via DMARC_IMAP_* env vars.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='List what would be ingested without writing.')
        parser.add_argument(
            '--leave-unread', action='store_true',
            help='Do not flag processed messages as seen.')
        parser.add_argument(
            '--days', type=int, default=7,
            help='Look back this many days (default 7).')

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

        # Read via Django settings (loaded from .env by django-environ
        # at app boot) — NOT os.getenv, which only sees vars already
        # exported into the process env.
        host = getattr(settings, 'DMARC_IMAP_HOST', '') or ''
        user = getattr(settings, 'DMARC_IMAP_USER', '') or ''
        pwd = getattr(settings, 'DMARC_IMAP_PASS', '') or ''
        folder = (getattr(settings, 'DMARC_IMAP_FOLDER', '') or 'INBOX')

        if not (host and user and pwd):
            self.stdout.write(self.style.WARNING(
                'DMARC_IMAP_* env vars not set — no-op.\n'
                'See reporting/management/commands/ingest_dmarc_imap.py '
                'header for setup.'))
            return

        dry_run = options['dry_run']
        leave_unread = options['leave_unread']
        since_date = datetime.utcnow() - timedelta(days=options['days'])
        since_imap = since_date.strftime('%d-%b-%Y')

        from reporting.dmarc import (
            ingest_dmarc_xml, parse_dmarc_attachment,
        )

        self.stdout.write(
            f'Connecting to {host} as {user} (folder={folder}, '
            f'since={since_imap})…')

        # SSL is the only sane default for Gmail IMAP.
        try:
            mbox = imaplib.IMAP4_SSL(host)
            mbox.login(user, pwd)
            mbox.select(_imap_quote(folder))
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(
                f'IMAP login / select failed: {exc}'))
            return

        # Search for unseen messages in the window.
        criteria = f'(SINCE {since_imap})'
        status, data = mbox.search(None, criteria)
        if status != 'OK':
            self.stderr.write(self.style.ERROR(
                f'IMAP search failed: status={status}'))
            mbox.logout()
            return

        msg_ids = (data[0] or b'').split()
        self.stdout.write(
            f'  → {len(msg_ids)} messages match window.')

        # An empty folder is the failure mode that hid this job's
        # breakage for two months: creds valid, folder selectable,
        # 0 messages, exit 0, "task succeeded". Say it loudly instead.
        if not msg_ids:
            warning = (
                f'0 messages in folder "{folder}" since {since_imap}. '
                f'If DMARC reports ARE arriving in this mailbox, the '
                f'folder is wrong — a Gmail label only receives mail '
                f'when a filter applies it, and filters are not '
                f'retroactive. Set DMARC_IMAP_FOLDER="[Gmail]/All Mail" '
                f'to scan everything.')
            logger.warning('DMARC ingest: %s', warning)
            self.stderr.write(self.style.WARNING(f'WARNING: {warning}'))

        ingested = duplicates = skipped = failures = 0
        for msg_id in msg_ids:
            status, msg_data = mbox.fetch(msg_id, '(RFC822)')
            if status != 'OK' or not msg_data:
                failures += 1
                continue
            raw_email = msg_data[0][1]
            try:
                msg = email.message_from_bytes(raw_email)
            except Exception:
                failures += 1
                continue

            # RFC 2047-decode before matching. Microsoft sends the
            # subject as a base64 encoded-word
            # (``=?utf-8?B?UmVwb3J0IERvbWFpbjog…``), which the raw
            # header value never matches — those reports survived only
            # because "dmarc" happens to appear in their From address.
            subject = _decode_mime_header(msg.get('Subject', ''))
            sender = _decode_mime_header(msg.get('From', ''))
            # Quick heuristic — DMARC reports usually have one of
            # these subject signals. Skip everything else without
            # walking the attachments (saves IMAP bytes).
            if not _looks_like_dmarc(subject, sender):
                skipped += 1
                continue

            handled_any = False
            for part in msg.walk():
                if part.get_content_maintype() == 'multipart':
                    continue
                fname = part.get_filename() or ''
                lname = fname.lower()
                if not lname.endswith(('.xml', '.zip', '.gz')):
                    continue
                payload = part.get_payload(decode=True) or b''
                if not payload:
                    continue
                xml = parse_dmarc_attachment(payload, filename=fname)
                if not xml:
                    self.stdout.write(self.style.WARNING(
                        f'    ! Could not unwrap {fname}'))
                    failures += 1
                    continue
                if dry_run:
                    self.stdout.write(
                        f'    [DRY] would ingest {fname} ({len(xml)} bytes)')
                    handled_any = True
                    continue
                report = ingest_dmarc_xml(xml)
                if report is None:
                    failures += 1
                    self.stderr.write(self.style.ERROR(
                        f'    x ingest failed for {fname}'))
                    continue
                # Detect duplicate (existing row returned vs fresh).
                from django.utils import timezone as _tz
                from datetime import timedelta as _td
                if (_tz.now() - report.received_at) < _td(seconds=10):
                    ingested += 1
                    self.stdout.write(self.style.SUCCESS(
                        f'    ✓ {report.org_name} '
                        f'{report.period_start:%Y-%m-%d}→'
                        f'{report.period_end:%Y-%m-%d} '
                        f'({report.total_messages} msgs)'))
                else:
                    duplicates += 1
                handled_any = True

            # Mark seen so we don't re-fetch it next run.
            if handled_any and not leave_unread and not dry_run:
                try:
                    mbox.store(msg_id, '+FLAGS', '\\Seen')
                except Exception:
                    logger.exception('failed to flag %s', msg_id)

        mbox.logout()

        # Messages were present but none of them yielded a report —
        # every one was filtered out by the subject/sender heuristic or
        # failed to unwrap. Also worth a warning: it means the mailbox
        # is being read but the matching logic has drifted.
        if msg_ids and not (ingested + duplicates):
            logger.warning(
                'DMARC ingest: scanned %d message(s) in "%s" but ingested '
                'nothing (skipped=%d failures=%d). Sender/subject matching '
                'or attachment unwrapping may have drifted.',
                len(msg_ids), folder, skipped, failures)
            self.stderr.write(self.style.WARNING(
                f'WARNING: scanned {len(msg_ids)} message(s) but ingested '
                f'nothing (skipped={skipped} failures={failures}).'))

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. ingested={ingested} duplicates={duplicates} '
            f'skipped={skipped} failures={failures}'))


def _decode_mime_header(raw):
    """Decode an RFC 2047 encoded-word header to plain text.

    ``email.message.Message.get()`` returns the header verbatim, so a
    base64-encoded Subject arrives as ``=?utf-8?B?…?=`` and no
    plain-text regex will ever match it. Falls back to the raw value
    if the header is malformed — matching on something is better than
    dropping the message.
    """
    if not raw:
        return ''
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001
        return raw


def _imap_quote(folder):
    """Quote a mailbox name for SELECT.

    imaplib does NOT quote arguments — it joins them with spaces and
    sends them raw. So a mailbox whose name contains a space (Gmail's
    ``[Gmail]/All Mail``, ``[Gmail]/Sent Mail``) is parsed by the
    server as two arguments and the command fails with
    ``BAD Could not parse command``.

    Quoting unconditionally is valid IMAP for every name, including
    plain ``INBOX``, so there's no need to special-case.
    """
    escaped = folder.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


# DMARC aggregate-report senders all use one of these signals in the
# subject or from-address. Worth being generous because the From: of
# reports varies a lot (postmaster@google.com, dmarc-noreply@...).
_DMARC_SUBJECT_RE = re.compile(
    r'(report\s+domain|dmarc|aggregate report)', re.IGNORECASE)
_DMARC_SENDER_RE = re.compile(
    r'(dmarc|postmaster|noreply.*report)', re.IGNORECASE)


def _looks_like_dmarc(subject, sender):
    return bool(
        _DMARC_SUBJECT_RE.search(subject or '')
        or _DMARC_SENDER_RE.search(sender or '')
    )
