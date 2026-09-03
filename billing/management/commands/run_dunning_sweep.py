"""
Run the payment-failure dunning sweep by hand.

The daily Celery beat job calls the same function. This exists so the
sweep can be inspected without waiting for 7:30am, and so `--dry-run`
can answer "what would this do right now" — worth checking before a
deploy that touches billing.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Advance every open payment-failure window by whatever is due.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what each open window would do, and change nothing.')

    def handle(self, *args, **options):
        from billing.dunning import SCHEDULE, check_delinquency
        from clients.account_models import Account, Website

        if not options['dry_run']:
            from billing.dunning import run_dunning_sweep
            summary = run_dunning_sweep()
            self.stdout.write(self.style.SUCCESS(f'dunning sweep: {summary}'))
            return

        now = timezone.now()
        accounts = Account.objects.filter(
            payment_failure_started_at__isnull=False)
        if not accounts:
            self.stdout.write('No open payment-failure windows.')
            return

        for account in accounts:
            window = account.payment_failure_started_at
            days = (now - window).days
            state = check_delinquency(account)
            label = {True: 'DELINQUENT', False: 'CURRENT',
                     None: 'UNKNOWN (Stripe unreachable)'}[state]
            self.stdout.write(
                f'\n{account} — day {days} of window opened {window:%Y-%m-%d} '
                f'— Stripe says {label}')

            if state is None:
                self.stdout.write('  → would do nothing and alert')
                continue
            if state is False:
                self.stdout.write(
                    '  → would CLOSE the window and restore any suspended site')
                continue

            sites = list(Website.objects.filter(account=account))
            for stage, threshold, scope, needs_approval in SCHEDULE:
                if days < threshold:
                    continue
                targets = [None] if scope == 'account' else sites
                for site in targets:
                    where = f' site={site.pk}' if site else ''
                    verb = ('would HOLD for approval' if needs_approval
                            else 'would RUN')
                    self.stdout.write(
                        f'  → {stage} (day {threshold}){where}: {verb} '
                        f'(if not already claimed)')
