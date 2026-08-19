"""
Report whether the legacy ClientProfile/Project tables can safely be dropped.

Dropping them is irreversible without a restore, so this command answers the
question with evidence rather than opinion. It is read-only.

It checks four things:

1. **Parity** — the strict gate must be clean. A single unmapped row means
   data disappears at drop time.
2. **Orphan risk** — dependent rows that still carry a legacy owner but no
   canonical one. These are exactly the rows that would be orphaned.
3. **Runtime readers** — source files still importing ClientProfile or
   Project. The cutover contract requires no runtime code to read them as a
   canonical source; until that count reaches zero, dropping the tables
   breaks the application rather than the data.

   Measured by AST (`clients.legacy_audit`), not by grep. Only live code
   reads block. A ForeignKey declaration is removed by the drop migration
   itself, so counting it as a blocker would make the gate unsatisfiable;
   a docstring mention breaks nothing at all.
4. **Schema surface** — how many legacy FK columns and tables the removal
   migration has to account for.

It never writes, and it never drops anything.
"""

from django.core.management.base import BaseCommand

from clients.legacy_audit import (
    scan_repository,
    scan_templates,
    summarise,
)


class Command(BaseCommand):
    help = ('Read-only readiness report for dropping the legacy '
            'ClientProfile/Project tables.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--strict', action='store_true',
            help='Exit non-zero unless every precondition is satisfied.')
        parser.add_argument(
            '--list-readers', action='store_true',
            help='List every blocking module and its line numbers, so the '
                 'remaining cutover work can be split up.')

    def handle(self, *args, **opts):
        blockers = []

        # ---- 1. parity ----
        from clients.parity import audit_account_website_parity

        report = audit_account_website_parity(detail_limit=3)
        self.stdout.write('Parity gate')
        self.stdout.write(
            f'  errors: {report.error_count}  '
            f'warnings: {report.warning_count}  '
            f'operational: {report.operational_count}')
        if report.error_count or report.warning_count:
            blockers.append(
                f'parity gate not clean ({report.error_count} errors, '
                f'{report.warning_count} warnings)')

        # ---- 2. rows that would be orphaned ----
        from django.apps import apps

        from clients.canonical_stamping import build_plan

        self.stdout.write('')
        self.stdout.write('Rows carrying a legacy owner but no canonical one')
        orphan_total = 0
        for model, (account_field, website_field, _) in build_plan().items():
            filters = {'client__isnull': False}
            if account_field:
                filters[f'{account_field}__isnull'] = True
            elif website_field:
                filters[f'{website_field}__isnull'] = True
            else:
                continue
            count = model.objects.filter(**filters).count()
            if count:
                orphan_total += count
                self.stdout.write(
                    f'  {model._meta.label}: {count}')
        if orphan_total:
            blockers.append(f'{orphan_total} row(s) would be orphaned')
        else:
            self.stdout.write('  none')

        # ---- 3. runtime readers ----
        self.stdout.write('')
        self.stdout.write('Runtime modules still referencing the legacy models')

        reports = scan_repository()
        totals = summarise(reports)
        blocking = [r for r in reports if r.blocks_removal]

        self.stdout.write(
            f'  live code reads: {totals["code_reads"]} line(s) across '
            f'{totals["blocking_modules"]} module(s)  <- blocks the drop')
        self.stdout.write(
            f'  FK declarations: {totals["schema_lines"]} line(s)  '
            '<- removed by the drop migration itself')
        self.stdout.write(
            f'  comments/docstrings: {totals["prose_mentions"]}  '
            '<- harmless')

        limit = None if opts['list_readers'] else 15
        shown = blocking if limit is None else blocking[:limit]
        for report in shown:
            detail = ''
            if opts['list_readers']:
                lines = ', '.join(str(n) for n in report.code_lines)
                detail = f'  (lines {lines})'
            self.stdout.write(
                f'  {len(report.code_lines):3d}  {report.path}{detail}')
        if limit is not None and len(blocking) > limit:
            self.stdout.write(f'  ... and {len(blocking) - limit} more '
                              '(--list-readers for all)')

        if blocking:
            blockers.append(
                f'{len(blocking)} runtime module(s) still read '
                'ClientProfile/Project')
        else:
            self.stdout.write('  none')

        # ---- 3b. templates ----
        #
        # The scan above parses Python, so for the whole cutover this was
        # a blind spot: twenty-two templates named a row's owner through
        # the legacy FK while the gate reported zero blockers. A `{% url %}`
        # with an empty argument raises NoReverseMatch and 500s the page;
        # a `{{ }}` resolves to the empty string and returns 200 with the
        # client's name missing, which is the one nothing catches.
        self.stdout.write('')
        self.stdout.write('Templates still resolving an owner via the '
                          'legacy FK')

        template_findings = scan_templates()
        breaking = [f for f in template_findings if f.severity == 'url']
        silent = [f for f in template_findings if f.severity == 'display']

        if template_findings:
            self.stdout.write(
                f'  {len(breaking)} that raise NoReverseMatch (500), '
                f'{len(silent)} that render blank (200)')
            for finding in template_findings:
                mark = '500' if finding.severity == 'url' else '   '
                lines = ', '.join(str(n) for n in finding.lines)
                self.stdout.write(
                    f'  {mark}  {finding.path}  {finding.variable} '
                    f'(lines {lines})')
            blockers.append(
                f'{len(template_findings)} template variable(s) still '
                'resolve an owner through the legacy FK')
        else:
            self.stdout.write('  none')

        # ---- 4. schema surface ----
        from clients.models import ClientProfile, Project

        columns = 0
        for model in apps.get_models():
            for field in model._meta.get_fields():
                related = getattr(field, 'related_model', None)
                if (related in (ClientProfile, Project)
                        and getattr(field, 'concrete', False)):
                    columns += 1
        self.stdout.write('')
        self.stdout.write(
            f'Schema surface: {columns} legacy FK column(s), 2 table(s)')
        self.stdout.write(
            f'Legacy rows: {ClientProfile.objects.count()} ClientProfile, '
            f'{Project.objects.count()} Project')

        # ---- verdict ----
        self.stdout.write('')
        if blockers:
            self.stdout.write(self.style.WARNING('NOT READY:'))
            for item in blockers:
                self.stdout.write(f'  - {item}')
            self.stdout.write('')
            self.stdout.write(
                'Also required and not checkable from here: a verified '
                'PostgreSQL backup, a timed PostgreSQL-to-PostgreSQL '
                'rehearsal on restored production data, and Waves 1-5 '
                'deployed and observed.')
            if opts['strict']:
                raise SystemExit(1)
            return
        else:
            self.stdout.write(self.style.SUCCESS(
                'All checkable preconditions satisfied.'))
            self.stdout.write(
                'Still confirm by hand: verified backup, timed '
                'PostgreSQL-to-PostgreSQL rehearsal, Waves 1-5 observed.')
