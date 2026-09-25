"""
run_sync — drain the pending outbound SyncJob queue.

Scheduled every minute via cron (NOT Celery — per CLAUDE.md). Each attempt
computes a FRESH HMAC + timestamp; only the payload_snapshot is frozen.
Failed jobs back off 1 / 5 / 15 / 60 minutes and are marked failed after
five attempts.
"""

from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from sync import transport
from sync.models import SyncJob

# Minutes to wait before the next attempt, keyed by attempts-so-far.
BACKOFF_MINUTES = {1: 1, 2: 5, 3: 15, 4: 60}
MAX_ATTEMPTS = 5


class Command(BaseCommand):
    help = 'Send pending outbound SyncJobs to Moonieful (cron: every minute).'

    def handle(self, *args, **options):
        target_url = settings.MOONIEFUL_SYNC_URL
        if not target_url:
            self.stdout.write(
                'run_sync: MOONIEFUL_SYNC_URL not configured — queue left pending.'
            )
            return
        if not settings.MOONIEFUL_SYNC_SECRET:
            self.stdout.write(
                'run_sync: MOONIEFUL_SYNC_SECRET not configured — aborting.')
            return

        # Only required for document_added jobs (see sync/transport.py) —
        # left unset, everything else still sends normally; document_added
        # jobs are simply held pending rather than burning an attempt
        # against a var that just isn't configured yet.
        file_url = settings.MOONIEFUL_SYNC_FILE_URL

        now = timezone.now()
        sent = failed = not_due = skipped = 0
        for job in SyncJob.objects.filter(status='pending', target='moonieful'):
            if not self._is_due(job, now):
                not_due += 1
                continue
            if job.event_type == 'document_added' and not file_url:
                skipped += 1
                continue

            if job.event_type == 'document_added':
                ok, error = transport.deliver_document(job, target_url, file_url)
            else:
                ok, error = transport.deliver_json(job, target_url)

            job.attempts += 1
            job.last_attempt_at = timezone.now()
            if ok:
                job.status = 'sent'
                job.sent_at = job.last_attempt_at
                sent += 1
            else:
                job.last_error = error
                if job.attempts >= MAX_ATTEMPTS:
                    job.status = 'failed'
                    failed += 1
                    self._alert_admin(job)
            job.save()

        self.stdout.write(
            f'run_sync: sent={sent} failed={failed} not-due={not_due} '
            f'skipped={skipped}'
        )

    def _is_due(self, job, now):
        """A job is due if it has never been tried, or its backoff has elapsed."""
        if job.attempts == 0 or job.last_attempt_at is None:
            return True
        wait = BACKOFF_MINUTES.get(job.attempts, 60)
        return now >= job.last_attempt_at + timedelta(minutes=wait)

    def _alert_admin(self, job):
        send_mail(
            subject=f'Sync job permanently failed — {job.event_type}',
            message=(
                f'SyncJob {job.pk} ({job.event_type}) failed after '
                f'{job.attempts} attempts and will not be retried.\n\n'
                f'Last error: {job.last_error}'
            ),
            from_email=settings.EMAIL_FROM_NO_REPLY,
            recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
            fail_silently=True,
        )
