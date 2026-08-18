"""
send_maintenance_upsell_nudges — finds SITES that have been live for
~30 / ~60 days, don't yet have an active maintenance subscription, and
haven't already been nudged at this touchpoint. Sends the branded
maintenance upsell email + records the timestamp in
`Website.maintenance_upsell_log` so the next run won't resend.

Run daily via Celery beat (billing.tasks.send_maintenance_upsell_nudges_task).
"""

import sys
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from clients.account_models import Website
from clients.emails import _recipient, send_maintenance_upsell_email

# Touchpoint code -> minimum age in days. Add new entries to extend the
# cadence — the email function reads its copy from `_UPSELL_COPY` in
# clients/emails.py, so keep the keys aligned across both places.
TOUCHPOINTS = (
    ('day_30', 30),
    ('day_60', 60),
)


class Command(BaseCommand):
    help = (
        'Send 30/60-day post-launch maintenance upsell emails to clients '
        'who don\'t have an active maintenance subscription. Idempotent.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Identify recipients and print what would be sent, '
                 'without actually mailing anyone.')
        parser.add_argument(
            '--touchpoint', default='',
            help='Only run a specific touchpoint code (e.g. day_30).')

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

        dry_run = options['dry_run']
        only_touchpoint = options['touchpoint']
        now = timezone.now()

        sent_count = 0
        skipped_count = 0
        for code, min_days in TOUCHPOINTS:
            if only_touchpoint and only_touchpoint != code:
                continue
            day_value = int(code.split('_')[1])
            cutoff = now - timedelta(days=min_days)

            # Candidate SITES: this build went live >=N days ago and has
            # no active maintenance sub.
            #
            # Per site, and off the canonical table. Iterating
            # ClientProfile meant a client created since the cutover was
            # not in the queryset at all, so they were never nudged -- a
            # revenue path that fails by staying quiet. Per site also
            # matters on its own: an account with two builds, one on a
            # plan and one not, is one upsell, not zero.
            candidates = (
                Website.objects
                .filter(maintenance_active=False,
                        status='active',
                        account__status='active',
                        launch_date__lte=cutoff.date(),
                        stage='live')
                .select_related('account', 'account__user')
            )

            for client in candidates:
                if not _recipient(client):
                    skipped_count += 1
                    continue
                log = client.maintenance_upsell_log or {}
                if log.get(code):
                    # Already sent — skip silently.
                    continue
                if dry_run:
                    self.stdout.write(
                        f'  [DRY] would send {code} to {client.name} '
                        f'<{_recipient(client)[0]}>')
                    sent_count += 1
                    continue
                try:
                    send_maintenance_upsell_email(client, day=day_value)
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(self.style.ERROR(
                        f'  x failed {code} for {client.pk} — {exc}'))
                    continue
                log[code] = now.isoformat()
                client.maintenance_upsell_log = log
                client.save(update_fields=[
                    'maintenance_upsell_log', 'updated_at'])
                sent_count += 1
                self.stdout.write(self.style.SUCCESS(
                    f'  ✓ {code} -> {client.name} <{_recipient(client)[0]}>'))

        verb = 'would send' if dry_run else 'sent'
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done — {verb} {sent_count} maintenance upsell email(s); '
            f'{skipped_count} skipped.'))
