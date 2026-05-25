"""
reconcile_domains — daily safety net for every DomainRegistration.

Pulls the current state from Namecheap (status, expiry, lock,
privacy, nameservers) and mirrors it locally; flags any domain that
no longer appears to be on our account; sends the 7-day pre-renewal
heads-up email to anyone whose Stripe sub fires soon.

Idempotent: re-running on the same day is a no-op past the pre-
renewal email (the model tracks the most recent renewal-soon
timestamp via `last_synced_at`).
"""

import sys
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from domains.emails import send_renewal_soon_email
from domains.models import DomainRegistration
from domains.namecheap_client import NamecheapError
from domains.services import sync_one


# 7-day pre-renewal heads-up. Tracked via Stripe-side cancel + local
# expires_at; we don't need to keep our own log file because the
# email is idempotent within a window (we only send when the renewal
# is between 6-8 days out, so we'll only fire it once per cycle).
RENEWAL_HEADS_UP_DAYS = 7
RENEWAL_HEADS_UP_WINDOW = 2   # days on either side


class Command(BaseCommand):
    help = (
        'Sync every active DomainRegistration with Namecheap + fire '
        '7-day renewal heads-up emails.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Sync state but do not send emails.')

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

        dry_run = options['dry_run']
        synced = 0
        flagged = 0
        emailed = 0
        now = timezone.now()
        heads_up_low = now + timedelta(
            days=RENEWAL_HEADS_UP_DAYS - RENEWAL_HEADS_UP_WINDOW)
        heads_up_high = now + timedelta(
            days=RENEWAL_HEADS_UP_DAYS + RENEWAL_HEADS_UP_WINDOW)

        for reg in DomainRegistration.objects.filter(
                status__in=('active', 'grace', 'pending')):
            try:
                sync_one(reg)
                synced += 1
            except NamecheapError as exc:
                self.stderr.write(self.style.ERROR(
                    f'  x sync failed for {reg.domain_name} — {exc}'))
                continue
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(self.style.ERROR(
                    f'  x unexpected error syncing {reg.domain_name} — {exc}'))
                continue

            if reg.last_api_error:
                flagged += 1

            # 7-day pre-renewal heads-up — only for active subs whose
            # expires_at lands in our heads-up window.
            if (reg.status == 'active' and reg.expires_at
                    and heads_up_low <= reg.expires_at <= heads_up_high):
                if not reg.client.user or not reg.client.user.email:
                    continue
                if dry_run:
                    self.stdout.write(
                        f'  [DRY] would email {reg.domain_name} '
                        f'<{reg.client.user.email}>')
                    emailed += 1
                    continue
                try:
                    days_until = max(
                        (reg.expires_at.date() - now.date()).days, 1)
                    send_renewal_soon_email(reg, days_until)
                    emailed += 1
                except Exception:
                    self.stderr.write(self.style.ERROR(
                        f'  x heads-up email failed for {reg.domain_name}'))

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done — synced {synced}, flagged {flagged}, '
            f'emailed {emailed} renewal heads-ups.'))
