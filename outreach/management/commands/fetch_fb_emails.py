"""
Find Facebook pages for no-website leads and read their contact email.

    # Free: just find the pages. Costs nothing, changes no email.
    python manage.py fetch_fb_emails --find-only

    # Estimate the paid step without running it (the DEFAULT).
    python manage.py fetch_fb_emails

    # Do it.
    python manage.py fetch_fb_emails --confirm

This is the only route to an email for leads sourced by ``scrape_maps``:
they have no website, so there is no homepage to scrape an address from.
See ``outreach/facebook_contacts.py`` for the measured hit rates and for
why storage is tiered rather than boolean.
"""

from django.core.management.base import BaseCommand, CommandError

from outreach import facebook_contacts as fb
from outreach.models import Lead


class Command(BaseCommand):
    help = ('Find Facebook pages for leads with no website and extract '
            'contact emails. Dry-run unless --confirm is passed.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Max leads to process. 0 = all eligible.')
        parser.add_argument(
            '--find-only', action='store_true',
            help='Only search for pages (free). Skips the paid step.')
        parser.add_argument(
            '--confirm', action='store_true',
            help='Required to run the billable extraction step.')
        parser.add_argument(
            '--business-type', default='',
            help='Restrict to one business_type, e.g. HVAC.')

    def handle(self, *args, **opts):
        w = self.stdout.write
        ok, warn, bad = (self.style.SUCCESS, self.style.WARNING,
                         self.style.ERROR)
        bold = self.style.MIGRATE_HEADING

        # Only leads that still need an email. A lead with one already is
        # not worth a Brave query or a cent of Apify spend.
        qs = Lead.objects.filter(email='', unsubscribed=False)
        if opts['business_type']:
            qs = qs.filter(business_type=opts['business_type'])
        if opts['limit']:
            qs = qs[:opts['limit']]
        leads = list(qs)
        if not leads:
            w(warn('No leads need an email. Nothing to do.'))
            return

        w(bold('\n=== FACEBOOK -> EMAIL ==='))
        w(f'  leads needing an email : {len(leads)}')
        w(f'  already have a page    : '
          f'{sum(1 for l in leads if l.facebook_url)}')

        # ── Step 1: find pages (free) ─────────────────────────────────
        w(bold('\n1. FINDING PAGES (Brave, free)'))
        try:
            found, checked = fb.find_facebook_pages(leads, log=w)
        except fb.FacebookContactError as exc:
            raise CommandError(str(exc))
        w(f'\n  searched {checked}, found {found} new page(s)')

        for l in leads:
            l.refresh_from_db()
        with_pages = [l for l in leads if l.facebook_url]
        w(f'  leads with a page now  : {len(with_pages)}')

        if opts['find_only']:
            w(warn('\n  --find-only: stopping before the paid step.\n'))
            return
        if not with_pages:
            w(warn('\n  No pages to read. Nothing to bill.\n'))
            return

        # ── Step 2: read pages (paid) ─────────────────────────────────
        urls = list(dict.fromkeys(l.facebook_url for l in with_pages))
        estimate = (fb.COST_PER_RUN_START_USD
                    + fb.COST_PER_PAGE_USD * len(urls))
        w(bold('\n2. READING PAGES (Apify, paid)'))
        w(f'  unique pages : {len(urls)}')
        w(f'  estimate     : ${estimate:.2f}')

        if not opts['confirm']:
            w(warn('\n  DRY RUN - nothing billed. Re-run with --confirm.\n'))
            return

        try:
            items, ledger = fb.fetch_page_contacts(urls)
        except fb.FacebookContactError as exc:
            raise CommandError(str(exc))
        w(f'  returned     : {len(items)} row(s)')
        if ledger:
            w(f'  billed       : ${ledger.cost_usd} (provisional)')

        # ── Step 3: verify + store ────────────────────────────────────
        w(bold('\n3. VERIFYING'))
        stats = fb.apply_contacts(items, with_pages)
        w(f'  stored, phone-verified : {stats["confident"]}')
        w(f'  stored, NEEDS REVIEW   : {stats["review"]}')
        w(f'  rejected               : {stats["reject"]}')
        w(f'  page had no email      : {stats["none"]}')
        w(f'  page errors            : {stats["error"]}')
        if stats['unmatched']:
            w(bad(f'  unmatched rows         : {stats["unmatched"]}'))

        if stats['review']:
            w(warn(f'\n  {stats["review"]} email(s) matched on NAME ONLY and '
                   f'are flagged.'))
            w(warn('  Check them before sending: '
                   '/admin-dashboard/outreach/review/'))

        w(ok(f'\n  leads with an email now: '
             f'{Lead.objects.exclude(email="").count()}\n'))
