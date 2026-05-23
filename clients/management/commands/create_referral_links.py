"""
Create a ReferralLink for every ClientProfile that doesn't have one.

Idempotent — safe to run repeatedly. Used as a one-shot backfill after
migration 0014 and as the seed step in the Phase 7 Part 2 deploy.

Usage:
    python manage.py create_referral_links
"""

from django.core.management.base import BaseCommand

from clients.models import (
    ClientProfile, ReferralLink, generate_referral_code,
)


class Command(BaseCommand):
    help = ('Create a ReferralLink for every ClientProfile that does '
            'not already have one.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--include-testers', action='store_true',
            help=('Include is_tester=True clients (default skips them, '
                  'so seed data never gets a real referral code).'),
        )

    def handle(self, *args, **options):
        include_testers = options['include_testers']

        qs = ClientProfile.objects.filter(referral_link__isnull=True)
        if not include_testers:
            qs = qs.filter(is_tester=False)

        created = 0
        for client in qs:
            code = generate_referral_code(client.firm_name)
            ReferralLink.objects.create(client=client, code=code)
            self.stdout.write(
                f'  + {client.firm_name:<40s} -> {code}')
            created += 1

        total = ReferralLink.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f'Created {created} new referral link(s). '
            f'Total in system: {total}.'))
