"""
Report every Website / Account whose ``onboarding_status`` holds a value
outside its own model's valid choices.

Built alongside the ``_on_intake_submitted`` fix in ``clients/views.py``,
which used to write ``'onboarding_complete'`` onto both
``Website.onboarding_status`` (valid: pending_intake / intake_complete /
complete) and ``Account.onboarding_status`` (valid: pending_setup /
complete) whenever intake ran through the aliased profile/project call
shape (the new portal flow, where both arguments are the same Website
instance). Any row written before that fix is stuck showing an invalid
state on the admin Website page and needs a human decision about what it
should become.

This command finds those rows. It does not fix them — read-only, never
writes. A stuck Website is almost always meant to become
'intake_complete' (the client did submit); a stuck Account, 'complete'.
But only a human who can see the client's actual state should make that
call, since the invalid value could also mean the write never landed for
a legitimate reason (e.g. the intake failed for something unrelated).
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Report Website/Account rows whose onboarding_status is not one '
        'of that model\'s valid choices. Read-only — reports only, never '
        'modifies data.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true', default=True,
            help=(
                'No-op flag, kept for interface parity with other repair '
                'commands in this app. This command never writes, '
                'regardless of this flag.'
            ),
        )

    def handle(self, *args, **options):
        from clients.account_models import Account, Website

        self.stdout.write('Scanning Website.onboarding_status …')
        website_valid = {c for c, _ in Website.ONBOARDING_STATUS_CHOICES}
        bad_websites = list(
            Website.objects.exclude(onboarding_status__in=website_valid)
            .select_related('account'))
        if bad_websites:
            for w in bad_websites:
                self.stdout.write(self.style.WARNING(
                    f'  Website {w.pk} ({w.name!r}, account={w.account_id}) '
                    f'has invalid onboarding_status={w.onboarding_status!r}'
                ))
        else:
            self.stdout.write('  none found.')

        self.stdout.write('')
        self.stdout.write('Scanning Account.onboarding_status …')
        account_valid = {c for c, _ in Account.ONBOARDING_STATUS_CHOICES}
        bad_accounts = list(
            Account.objects.exclude(onboarding_status__in=account_valid))
        if bad_accounts:
            for a in bad_accounts:
                self.stdout.write(self.style.WARNING(
                    f'  Account {a.pk} ({a.name!r}) has invalid '
                    f'onboarding_status={a.onboarding_status!r}'
                ))
        else:
            self.stdout.write('  none found.')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. {len(bad_websites)} Website(s), {len(bad_accounts)} '
            f'Account(s) with an invalid onboarding_status. This command '
            f'is read-only and changed nothing.'
        ))
