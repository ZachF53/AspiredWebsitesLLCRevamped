"""
run_sync — drain the pending outbound SyncJob queue.

Scheduled every minute via cron (NOT Celery — per CLAUDE.md). Each attempt
computes a FRESH HMAC + timestamp; only the payload_snapshot is frozen.
Failed jobs back off 1 / 5 / 15 / 60 minutes and are marked failed after
five attempts.
"""

import hashlib
import hmac
import json
import time
from datetime import timedelta

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

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

        now = timezone.now()
        sent = failed = not_due = 0
        for job in SyncJob.objects.filter(status='pending', target='moonieful'):
            if not self._is_due(job, now):
                not_due += 1
                continue

            ok, error = self._deliver(job, target_url)
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
            f'run_sync: sent={sent} failed={failed} not-due={not_due}'
        )

    def _is_due(self, job, now):
        """A job is due if it has never been tried, or its backoff has elapsed."""
        if job.attempts == 0 or job.last_attempt_at is None:
            return True
        wait = BACKOFF_MINUTES.get(job.attempts, 60)
        return now >= job.last_attempt_at + timedelta(minutes=wait)

    def _deliver(self, job, url):
        """POST one job with a freshly computed signature + timestamp."""
        envelope = {
            'schema_version': 1,
            'event_type': job.event_type,
            **(job.payload_snapshot or {}),
        }
        body = json.dumps(envelope, sort_keys=True).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            settings.MOONIEFUL_SYNC_SECRET.encode(), body, hashlib.sha256,
        ).hexdigest()
        headers = {
            'Content-Type': 'application/json',
            'X-Sync-Timestamp': timestamp,
            'X-Sync-Signature': signature,
        }
        try:
            resp = requests.post(url, data=body, headers=headers, timeout=20)
        except requests.RequestException as exc:
            return False, str(exc)
        if resp.status_code == 200:
            return True, ''
        return False, f'HTTP {resp.status_code}: {resp.text[:200]}'

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
