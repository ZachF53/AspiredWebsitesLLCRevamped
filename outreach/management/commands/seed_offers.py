"""
Seed the Offer table from the constants in outreach/sequences.py.

    python manage.py seed_offers
    python manage.py seed_offers --activate    # also switch them on

Idempotent. Existing rows are matched on ``key`` and left alone unless
--overwrite is passed, because the whole point of moving offers into the
database was that a human can edit them there; re-seeding must not stamp
over an edit somebody made in the admin.
"""

from django.core.management.base import BaseCommand

from outreach import sequences
from outreach.models import Offer


class Command(BaseCommand):
    help = 'Create Offer rows from outreach/sequences.OFFERS.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--activate', action='store_true',
            help='Mark seeded offers active. Off by default so nothing '
                 'becomes sendable just because it was seeded.')
        parser.add_argument(
            '--overwrite', action='store_true',
            help='Reset existing rows to the code constants, DISCARDING '
                 'any admin edits.')

    def handle(self, *args, **opts):
        w = self.stdout.write
        created = updated = skipped = 0

        for key, spec in sequences.OFFERS.items():
            defaults = {
                'name': spec['name'],
                'appeals_to': spec.get('appeals_to', ''),
                'fulfilment_cost': spec.get('fulfilment_cost', ''),
                'pitch': spec['pitch'],
                'restate': spec['restate'],
                'ask': spec['ask'],
                'proposed_by': 'human',
                'active': bool(opts['activate']),
            }
            row = Offer.objects.filter(key=key).first()
            if row is None:
                Offer.objects.create(key=key, **defaults)
                created += 1
                w(self.style.SUCCESS(f'  created  {key}'))
            elif opts['overwrite']:
                for field, value in defaults.items():
                    # Never silently flip active off on an existing row.
                    if field == 'active' and not opts['activate']:
                        continue
                    setattr(row, field, value)
                row.save()
                updated += 1
                w(self.style.WARNING(f'  overwrote {key}'))
            else:
                skipped += 1
                w(f'  exists   {key} (use --overwrite to reset)')

        w('')
        w(f'created={created} overwritten={updated} left alone={skipped}')
        active = Offer.objects.filter(active=True).count()
        w(f'active offers: {active} / {Offer.objects.count()}')
        if not active:
            w(self.style.WARNING(
                'None are active. Campaigns can still build copy from '
                'them, but activate the ones you intend to test at '
                '/admin/outreach/offer/'))
