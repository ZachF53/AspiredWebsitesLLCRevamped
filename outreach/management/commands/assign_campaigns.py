"""
Place ready leads into A/B arms — or show what that would do.

    python manage.py assign_campaigns --dry-run
    python manage.py assign_campaigns

The dry run exists because assignment is the last reversible step. Once a
lead is pushed to Instantly it is queued to email a real stranger, and
"undo" means asking Instantly to stop a campaign that may already have
sent. Reading the planned split first costs nothing.
"""

from django.core.management.base import BaseCommand

from outreach.assignment import assign_leads, open_campaigns


class Command(BaseCommand):
    help = 'Assign ready leads to campaign arms (offer A/B/C...).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show the split without writing anything.')
        parser.add_argument(
            '--limit', type=int, default=500,
            help='Maximum leads to place in this run (default 500).')

    def handle(self, *args, **options):
        w = self.stdout.write
        ok = self.style.SUCCESS
        warn = self.style.WARNING
        bad = self.style.ERROR

        arms = open_campaigns()
        if not arms:
            w(bad('No arm is accepting leads.'))
            w('An arm accepts leads when it is active, has an Instantly '
              'campaign id,')
            w('and has not reached its lead_target.')
            w('Manage them at /admin-dashboard/outreach/campaigns/')
            return

        w(self.style.MIGRATE_HEADING('\nOpen arms'))
        for c in arms:
            target = str(c.lead_target) if c.lead_target else 'unlimited'
            offer = c.offer.name if c.offer else '(default offer)'
            w(f'  {c.name:30} {c.assigned:>5} / {target:>9}   {offer}')

        summary = assign_leads(
            limit=options['limit'], dry_run=options['dry_run'])

        verb = 'Would assign' if summary['dry_run'] else 'Assigned'
        w(self.style.MIGRATE_HEADING(f'\n{verb}'))
        if not summary['by_campaign']:
            w('  nothing — no ready leads matched an open arm.')
        for name, n in sorted(summary['by_campaign'].items()):
            w(ok(f'  {name:30} {n:>5}'))

        if summary['skipped_no_campaign']:
            w(self.style.MIGRATE_HEADING('\nReady, but no arm will take them'))
            for reason, n in sorted(summary['reasons'].items()):
                w(warn(f'  {n:>5}  {reason}'))
            w('\n  These are HELD, not dropped. Open a matching arm and '
              'they flow on')
            w('  the next run.')

        if summary['dry_run']:
            w(warn('\nDry run — nothing was written.'))
        w('')
