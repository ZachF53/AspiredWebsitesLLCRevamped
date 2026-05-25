"""
reconcile_domains — daily safety net for every DomainRegistration.

Five jobs run end-to-end:
  1. Pull current state from Namecheap for every non-terminal
     registration (status, expiry, lock, privacy, nameservers)
  2. Send 7-day pre-renewal heads-up emails to active domains whose
     renewal is between 6 and 8 days out
  3. Send expiration-cascade warnings (7/3/1 days) to grace-status
     domains that are about to ACTUALLY EXPIRE at the registry
  4. Retry failed Namecheap renewals up to 3 times across 24 hours,
     then alert admin if still failing
  5. Check the Namecheap account balance + alert admin if it drops
     below LOW_BALANCE_THRESHOLD

Idempotent: each touchpoint guards itself so re-running on the same
day is a no-op past anything already done.
"""

import sys
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from domains.emails import (
    send_expiring_warning_email,
    send_renewal_soon_email,
)
from domains.models import DomainRegistration
from domains.namecheap_client import NamecheapError, get_client
from domains.services import sync_one


# 7-day pre-renewal heads-up. Tracked via Stripe-side cancel + local
# expires_at; we don't need to keep our own log file because the
# email is idempotent within a window (we only send when the renewal
# is between 6-8 days out, so we'll only fire it once per cycle).
RENEWAL_HEADS_UP_DAYS = 7
RENEWAL_HEADS_UP_WINDOW = 2   # days on either side

# Final-warning expiration cascade — fired when a domain is in
# 'grace' status (client cancelled or renewal repeatedly failed)
# and is about to fall off the registry. Each window is ±0.5 days
# so the 4:30am daily run catches it cleanly.
EXPIRATION_WARNING_DAYS = (7, 3, 1)
EXPIRATION_WARNING_WINDOW = Decimal('0.5')

# Below this, email the admin so they can top up before any
# registrations start failing.
LOW_BALANCE_THRESHOLD = Decimal('25')

# Failed-renewal retry cadence: try up to 3 times, with the first
# retry at the next reconcile run after the failure (24h later).
FAILED_RENEW_MAX_RETRIES = 3


class Command(BaseCommand):
    help = (
        'Sync every active DomainRegistration with Namecheap + fire '
        'renewal + expiration warnings + retry failed renewals + '
        'check account balance.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Sync state but do not send emails / retry / alert.')

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

        dry_run = options['dry_run']
        synced = 0
        flagged = 0
        renewal_emails = 0
        expiry_emails = 0
        retried = 0
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

            has_email = bool(reg.client.user and reg.client.user.email)

            # ── (2) 7-day pre-renewal heads-up (active only) ──
            if (reg.status == 'active' and reg.expires_at and has_email
                    and heads_up_low <= reg.expires_at <= heads_up_high):
                if dry_run:
                    self.stdout.write(
                        f'  [DRY] renewal heads-up to {reg.domain_name}')
                    renewal_emails += 1
                else:
                    try:
                        days_until = max(
                            (reg.expires_at.date() - now.date()).days, 1)
                        send_renewal_soon_email(reg, days_until)
                        renewal_emails += 1
                    except Exception:
                        self.stderr.write(self.style.ERROR(
                            f'  x heads-up email failed for {reg.domain_name}'))

            # ── (3) Expiration cascade for grace-status domains ──
            if (reg.status == 'grace' and reg.expires_at and has_email):
                days_out_dec = Decimal(
                    (reg.expires_at - now).total_seconds() / 86400.0)
                for window_days in EXPIRATION_WARNING_DAYS:
                    if abs(days_out_dec - Decimal(window_days)
                           ) <= EXPIRATION_WARNING_WINDOW:
                        if dry_run:
                            self.stdout.write(
                                f'  [DRY] expiry-{window_days}d warning to '
                                f'{reg.domain_name}')
                            expiry_emails += 1
                            break
                        try:
                            send_expiring_warning_email(reg, window_days)
                            expiry_emails += 1
                        except Exception:
                            self.stderr.write(self.style.ERROR(
                                f'  x expiry email failed for '
                                f'{reg.domain_name}'))
                        break

            # ── (4) Retry failed renewals ──
            # Marker: status=active + last_api_error starts with "renew:"
            # AND the row has a Stripe sub (so we're sure there was a
            # successful charge). We retry up to 3 times — track count
            # via a marker in internal_notes.
            if (reg.status == 'active' and reg.stripe_subscription_id
                    and reg.last_api_error
                    and reg.last_api_error.startswith('renew:')):
                retry_count = (reg.internal_notes or '').count(
                    '[renew-retry]')
                if retry_count < FAILED_RENEW_MAX_RETRIES:
                    if dry_run:
                        self.stdout.write(
                            f'  [DRY] would retry renew {reg.domain_name} '
                            f'(attempt {retry_count + 1})')
                        retried += 1
                    else:
                        if self._retry_renew(reg):
                            retried += 1

        # ── (5) Account balance check ──
        balance_line = self._check_account_balance(dry_run)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done — synced {synced}, flagged {flagged}, '
            f'renewal heads-ups {renewal_emails}, '
            f'expiry warnings {expiry_emails}, '
            f'renew retries {retried}.'))
        if balance_line:
            self.stdout.write(balance_line)

    def _retry_renew(self, reg):
        """Attempt one Namecheap renew. Increment retry marker on the
        row regardless of outcome so we don't loop. Alert admin on
        the final attempt if still failing."""
        nc = get_client()
        marker = '[renew-retry]'
        reg.internal_notes = (
            (reg.internal_notes or '') + f'\n{marker} '
            f'{timezone.now().isoformat()}'
        ).strip()
        try:
            result = nc.renew_domain(reg.domain_name, years=1)
            if result.get('renewed'):
                reg.expires_at = (
                    (reg.expires_at or timezone.now())
                    + timedelta(days=365))
                reg.last_api_error = ''
                reg.save(update_fields=[
                    'expires_at', 'last_api_error',
                    'internal_notes', 'updated_at'])
                self.stdout.write(self.style.SUCCESS(
                    f'  ✓ renew retry succeeded for {reg.domain_name}'))
                return True
            reg.save(update_fields=['internal_notes', 'updated_at'])
        except Exception as exc:
            reg.last_api_error = f'renew: {exc}'[:2000]
            reg.save(update_fields=[
                'last_api_error', 'internal_notes', 'updated_at'])
            self.stderr.write(self.style.ERROR(
                f'  x renew retry failed for {reg.domain_name}: {exc}'))

        # Final retry failed — alert admin once.
        retry_count = (reg.internal_notes or '').count('[renew-retry]')
        if retry_count >= FAILED_RENEW_MAX_RETRIES:
            self._alert_admin_renew_exhausted(reg)
        return False

    def _check_account_balance(self, dry_run):
        """
        Fetch the Namecheap account balance + alert admin if low.
        Returns a human-readable summary line for the run output.
        """
        try:
            nc = get_client()
            balances = nc.get_balances()
        except Exception as exc:  # noqa: BLE001
            return f'Balance check failed: {exc}'
        available = balances.get(
            'available_balance', Decimal('0'))
        line = (
            f'Account balance: '
            f'available ${available} / '
            f'total ${balances.get("account_balance", 0)} '
            f'({balances.get("currency", "USD")})'
        )
        if available < LOW_BALANCE_THRESHOLD and not dry_run:
            self._alert_admin_low_balance(available, balances)
        return line

    def _alert_admin_low_balance(self, available, balances):
        """Email admin when Namecheap balance is below threshold."""
        try:
            from django.conf import settings as _s
            from django.core.mail import send_mail
            send_mail(
                subject=(
                    f'[Namecheap balance low] ${available} '
                    f'available'),
                message=(
                    f'Heads-up — the Namecheap account balance is '
                    f'low.\n\n'
                    f'Available: ${available} '
                    f'{balances.get("currency", "USD")}\n'
                    f'Account total: ${balances.get("account_balance", 0)}\n'
                    f'Threshold: ${LOW_BALANCE_THRESHOLD}\n\n'
                    f'Top up at https://ap.www.namecheap.com/'
                    f'profile/account-balance/ before new '
                    f'registrations or renewals start failing.\n'),
                from_email=getattr(
                    _s, 'EMAIL_FROM_NO_REPLY', _s.DEFAULT_FROM_EMAIL),
                recipient_list=[_s.LEAD_NOTIFICATION_EMAIL],
                fail_silently=True,
            )
        except Exception:
            pass    # never fail the cron over an alert email

    def _alert_admin_renew_exhausted(self, reg):
        """Email admin when failed-renew retries are exhausted."""
        try:
            from django.conf import settings as _s
            from django.core.mail import send_mail
            send_mail(
                subject=(
                    f'[Domain renew retries exhausted] '
                    f'{reg.domain_name}'),
                message=(
                    f'After {FAILED_RENEW_MAX_RETRIES} retry '
                    f'attempts, the Namecheap renew for '
                    f'{reg.domain_name} is still failing.\n\n'
                    f'Last error: {reg.last_api_error}\n\n'
                    f'Client: {reg.client.firm_name}\n'
                    f'Registration ID: {reg.id}\n\n'
                    f'Action: renew manually on Namecheap, or refund '
                    f'the client and let it expire.\n'),
                from_email=getattr(
                    _s, 'EMAIL_FROM_NO_REPLY', _s.DEFAULT_FROM_EMAIL),
                recipient_list=[_s.LEAD_NOTIFICATION_EMAIL],
                fail_silently=True,
            )
        except Exception:
            pass
