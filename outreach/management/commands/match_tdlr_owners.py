"""
Name the person behind each Texas HVAC lead, from the public TDLR register.

    python manage.py match_tdlr_owners                  # report only
    python manage.py match_tdlr_owners --write          # save the names

Free -- it is a public CSV download. See ``outreach/tdlr.py`` for why a
match is only recorded when it is unambiguous.
"""

from django.core.management.base import BaseCommand, CommandError

from outreach import tdlr
from outreach.models import Lead


class Command(BaseCommand):
    help = ('Match Texas leads to the public TDLR A/C contractor licence '
            'register to recover the owner name. Report-only by default.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--write', action='store_true',
            help='Save the matched names. Without it, report only.')
        parser.add_argument(
            '--state', default='TX',
            help='Only leads in this state. The register is Texas-only.')
        parser.add_argument(
            '--business-type', default='',
            help='Restrict to one business_type, e.g. HVAC.')
        parser.add_argument(
            '--overwrite', action='store_true',
            help='Replace existing contact names.')

    def handle(self, *args, **opts):
        w = self.stdout.write
        ok, warn = self.style.SUCCESS, self.style.WARNING
        bold = self.style.MIGRATE_HEADING

        if opts['state'].upper() != 'TX':
            raise CommandError(
                'The TDLR register covers Texas only; --state must be TX.')

        qs = Lead.objects.filter(state__iexact='TX')
        if opts['business_type']:
            qs = qs.filter(business_type=opts['business_type'])
        if not opts['overwrite']:
            qs = qs.filter(attorney_name='')
        leads = list(qs)
        if not leads:
            w(warn('No Texas leads need an owner name.'))
            return

        w(bold('\n=== TDLR OWNER MATCH ==='))
        w(f'  leads to match : {len(leads)}')
        w('  downloading the public register...')
        try:
            rows = tdlr.fetch_register()
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f'Could not fetch the TDLR register: {exc}')
        groups = tdlr.index_by_name(rows)
        ambiguous_keys = sum(1 for v in groups.values() if len(v) > 1)
        w(f'  licence rows   : {len(rows)}')
        w(f'  distinct names : {len(groups)} '
          f'({ambiguous_keys} shared by >1 licensee)')

        if not opts['write']:
            # Same logic, no writes, so the preview cannot disagree with
            # what --write would do.
            hit = amb = miss = 0
            w(bold('\n  preview:'))
            for lead in leads:
                row, how = tdlr.match_lead(lead, groups)
                if row is not None:
                    hit += 1
                    if hit <= 15:
                        w(f'    {lead.firm_name[:26]:28} -> '
                          f'{tdlr.format_person(row[tdlr.COL_NAME])[:26]:28} '
                          f'({how})')
                elif 'share this business name' in how:
                    amb += 1
                else:
                    miss += 1
            w(f'\n  would match : {hit}')
            w(f'  ambiguous   : {amb}  (left alone - a wrong owner name '
              f'is worse than none)')
            w(f'  no match    : {miss}')
            w(warn('\n  REPORT ONLY - pass --write to save.\n'))
            return

        matched, report = tdlr.apply_to_leads(
            leads, groups, overwrite=opts['overwrite'])
        for lead, person, how in report:
            if person:
                w(f'    {lead.firm_name[:26]:28} -> {person[:26]:28} ({how})')
        amb = sum(1 for _, p, h in report
                  if p is None and 'share this business name' in h)
        w(ok(f'\n  named {matched} lead(s)'))
        w(f'  ambiguous, left alone : {amb}')
        w(f'  leads with a name now : '
          f'{Lead.objects.exclude(attorney_name="").count()}\n')
