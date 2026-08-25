"""
Copy Google ratings onto qualified leads, or show what that would cost.

    python manage.py google_profiles --dry-run
    python manage.py google_profiles --limit 50

Each lookup is a billed Places call, so --dry-run reports exactly how many
leads qualify and what they would cost before any of it is spent.
"""

from django.core.management.base import BaseCommand

from outreach import google_profile


class Command(BaseCommand):
    help = 'Look up qualified leads in Google Places and store their rating.'

    # Text Search (New), Essentials SKU. Used only to show the operator a
    # figure before they commit; nothing bills off this constant.
    COST_PER_LOOKUP_USD = 0.032

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show who qualifies and the cost. No calls.')
        parser.add_argument('--limit', type=int, default=50)

    def handle(self, *args, **options):
        w = self.stdout.write
        ok, warn, bad = (self.style.SUCCESS, self.style.WARNING,
                         self.style.ERROR)

        allowed, why = google_profile.check_allowed()
        used = google_profile.lookups_today()

        qualified = google_profile.qualified_leads()
        n = min(len(qualified), options['limit'])

        w(self.style.MIGRATE_HEADING('\nGoogle Places join'))
        w(f'  qualified, not yet checked : {len(qualified)}')
        w(f'  lookups used today         : {used}')
        w(f'  this run would look up     : {n}')
        w(f'  estimated cost             : '
          f'${n * self.COST_PER_LOOKUP_USD:.2f}')

        if not allowed:
            w(bad(f'\n  BLOCKED: {why}'))
            return

        if options['dry_run']:
            w(self.style.MIGRATE_HEADING('\nWould look up'))
            for lead in qualified[:n][:15]:
                w(f'  {lead.firm_name[:34]:36} {lead.city[:16]:18} '
                  f'{(lead.website or "")[:34]}')
            if n > 15:
                w(f'  ... and {n - 15} more')
            w(warn('\nDry run — no calls made, nothing charged.'))
            return

        summary = google_profile.backfill(limit=options['limit'])
        if summary.get('reason'):
            w(bad(f"  {summary['reason']}"))
            return

        w(self.style.MIGRATE_HEADING('\nResult'))
        w(f"  looked up   : {summary['looked_up']}")
        w(ok(f"  matched     : {summary['matched']}"))
        w(ok(f"  citable     : {summary['citable']}   "
             f"(enough reviews to open an email with)"))
        w(f"  no listing  : {summary['no_listing']}")
        w(warn(f"  rejected    : {summary['rejected']}   "
               f"(a hit existed but did not provably match)"))
        if summary['errors']:
            w(bad(f"  errors      : {summary['errors']}"))
        w('')
