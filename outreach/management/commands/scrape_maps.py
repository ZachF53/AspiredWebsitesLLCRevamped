"""
Source no-website businesses from Google Maps via Apify.

    # Estimate only — costs nothing, and this is the DEFAULT.
    python manage.py scrape_maps --cities "San Antonio,Austin" --max 100

    # Actually spend money.
    python manage.py scrape_maps --cities "San Antonio,Austin" --max 100 \
        --confirm

    # Re-import a run that was triggered in the Apify Console — free.
    python manage.py scrape_maps --dataset-id D0yXKcxHBVbI4Tw4F

``--confirm`` is required for any run that bills. The actor is
pay-per-place and a mistyped ``--max`` is money, so the safe mode is the
one you get by default.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from outreach import apify_maps
from outreach.apify_source import ApifyError, ApifyQuotaReached, budget_status
from outreach.pipeline import import_leads


class Command(BaseCommand):
    help = ('Scrape Google Maps for businesses with no website and import '
            'them as leads. Dry-run unless --confirm is passed.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--niche', default='HVAC contractor',
            help='Maps search term. Default: "HVAC contractor".')
        parser.add_argument(
            '--cities', default='San Antonio,Austin',
            help='Comma-separated city list. One actor run per city.')
        parser.add_argument(
            '--state', default='TX',
            help='Two-letter state, used in the query and the screen.')
        parser.add_argument(
            '--business-type', default='HVAC',
            help=('business_type stamped on every imported lead. MUST be '
                  'set: import_leads falls back to "Law Firm" when it is '
                  'blank, which would mis-segment the whole batch.'))
        parser.add_argument(
            '--max', type=int, default=100, dest='max_results',
            help='Max places per city. Clamped by OutreachSettings.')
        parser.add_argument(
            '--with-website', action='store_true',
            help=('Scrape places that HAVE a website instead. These can '
                  'go through the normal email pipeline.'))
        parser.add_argument(
            '--all-places', action='store_true',
            help='No website filter at all — cheaper per place, mixed list.')
        parser.add_argument(
            '--dataset-id', default='',
            help='Import an existing Apify dataset instead of scraping. Free.')
        parser.add_argument(
            '--fallback-city', default='',
            help=('City stamped on service-area businesses, which Maps '
                  'returns with no address at all. Defaults to the first '
                  '--cities entry; set explicitly with --dataset-id.'))
        parser.add_argument(
            '--confirm', action='store_true',
            help='Required to start a billable run. Without it, estimate only.')

    def handle(self, *args, **opts):
        w = self.stdout.write
        ok = self.style.SUCCESS
        warn = self.style.WARNING
        bad = self.style.ERROR
        bold = self.style.MIGRATE_HEADING

        business_type = (opts['business_type'] or '').strip()
        if not business_type:
            raise CommandError(
                '--business-type cannot be blank: import_leads would stamp '
                'these leads "Law Firm".')

        if opts['with_website'] and opts['all_places']:
            raise CommandError(
                '--with-website and --all-places are mutually exclusive.')
        if opts['with_website']:
            website_filter = apify_maps.WEBSITE_WITH
        elif opts['all_places']:
            website_filter = apify_maps.WEBSITE_ANY
        else:
            website_filter = apify_maps.WEBSITE_WITHOUT

        # ── Free path: import an existing dataset ─────────────────────
        if opts['dataset_id']:
            w(bold(f'\nImporting dataset {opts["dataset_id"]} (free)\n'))
            fallback_city = (
                opts['fallback_city']
                or (opts['cities'] or '').split(',')[0].strip())
            try:
                leads, ledger = apify_maps.import_from_dataset(
                    opts['dataset_id'], target_state=opts['state'],
                    business_type=business_type,
                    website_filter=website_filter,
                    fallback_city=fallback_city,
                    fallback_state=opts['state'])
            except ApifyError as exc:
                raise CommandError(str(exc))
            self._import(leads, ledger, business_type)
            return

        cities = [c.strip() for c in (opts['cities'] or '').split(',')
                  if c.strip()]
        if not cities:
            raise CommandError('--cities is empty.')

        # ── Cost preview, always ──────────────────────────────────────
        _, n_filters = apify_maps.build_actor_input(
            niche=opts['niche'], city=cities[0], state=opts['state'],
            max_places=opts['max_results'], website_filter=website_filter)
        per_city = apify_maps.estimate_cost_usd(
            opts['max_results'], n_filters)
        total_est = per_city * len(cities)
        status = budget_status()

        w(bold('\n=== APIFY MAPS SCRAPE ==='))
        w(f'  niche          : {opts["niche"]}')
        w(f'  cities         : {", ".join(cities)}  ({len(cities)} runs)')
        w(f'  state          : {opts["state"]}')
        w(f'  business_type  : {business_type}')
        w(f'  website filter : {website_filter}')
        w(f'  max per city   : {opts["max_results"]}')
        w(f'  filters applied: {n_filters}  '
          f'(billed per place, per filter)')
        w('')
        w(f'  est. per city  : ${per_city:.2f}')
        w(f'  est. TOTAL     : ${total_est:.2f}')
        w(f'  budget MTD     : ${status["spent_usd"]:.2f} / '
          f'${status["budget_usd"]:.2f}')

        if website_filter == apify_maps.WEBSITE_WITHOUT:
            w(warn('\n  NOTE: no-website leads arrive with NO EMAIL and '
                   'cannot be'))
            w(warn('  cold-emailed. Their only contact channel is the '
                   'phone number.'))

        if not opts['confirm']:
            w(warn('\n  DRY RUN - nothing scraped, nothing billed.'))
            w('  Re-run with --confirm to spend the estimate above.\n')
            return

        # ── Billable path ─────────────────────────────────────────────
        grand = {'total': 0, 'imported': 0, 'duplicates': 0,
                 'suppressed': 0, 'errors': 0}
        spent = Decimal('0')

        for city in cities:
            w(bold(f'\n--- {city}, {opts["state"]} ---'))
            try:
                leads, ledger = apify_maps.run_maps_search(
                    niche=opts['niche'], city=city, state=opts['state'],
                    max_results=opts['max_results'],
                    website_filter=website_filter,
                    business_type=business_type)
            except ApifyQuotaReached as exc:
                w(warn(f'  quota reached: {exc}'))
                w(warn('  stopping — later cities would hit the same gate.'))
                break
            except ApifyError as exc:
                w(bad(f'  failed: {exc}'))
                continue

            w(f'  places returned : {ledger.results_returned}')
            w(f'  screened out    : {ledger.results_rejected}')
            w(f'  mappable        : {len(leads)}')
            w(f'  billed          : ${ledger.cost_usd}')
            spent += Decimal(str(ledger.cost_usd or 0))

            res = self._import(leads, ledger, business_type)
            for k in grand:
                grand[k] += res.get(k, 0)

        w(bold('\n=== TOTAL ==='))
        for k, v in grand.items():
            w(f'  {k:12} {v}')
        w(f'  billed       ${spent}')
        w(ok('\nNext: python manage.py outreach_status\n'))

    def _import(self, leads, ledger, business_type):
        """Persist a batch and record the count on the run ledger."""
        w = self.stdout.write
        if not leads:
            w('  nothing to import')
            return {}
        res = import_leads(
            leads, source='google_maps',
            business_type_override=business_type)
        w(f'  imported        : {res["imported"]}')
        w(f'  duplicates      : {res["duplicates"]}')
        if res.get('suppressed'):
            w(f'  suppressed      : {res["suppressed"]}')
        if res.get('errors'):
            w(f'  errors          : {res["errors"]}')
        if res.get('flagged_for_review'):
            w(f'  flagged review  : {res["flagged_for_review"]}')
        ledger.leads_imported = res['imported']
        ledger.save(update_fields=['leads_imported'])
        return res
