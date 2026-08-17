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
4. **Schema surface** — how many legacy FK columns and tables the removal
   migration has to account for.

It never writes, and it never drops anything.
"""

import pathlib

from django.core.management.base import BaseCommand


# Modules that legitimately reference the legacy models after cutover:
# the migration tooling itself, the models module that defines them, and
# the parity/backfill machinery whose entire job is the transition.
_ALLOWED_REFERENCES = (
    'clients/models.py',
    'clients/parity.py',
    'clients/account_setup.py',
    'clients/canonical_stamping.py',
    'clients/canonical_iteration.py',
    'clients/signals.py',
    'clients/apps.py',
    'clients/account_models.py',
    'clients/management/commands/',
    'migrations/',
    'migrations_planned/',
)


class Command(BaseCommand):
    help = ('Read-only readiness report for dropping the legacy '
            'ClientProfile/Project tables.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--strict', action='store_true',
            help='Exit non-zero unless every precondition is satisfied.')

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
        readers = self._runtime_readers()
        for path in readers[:15]:
            self.stdout.write(f'  {path}')
        if len(readers) > 15:
            self.stdout.write(f'  ... and {len(readers) - 15} more')
        if readers:
            blockers.append(
                f'{len(readers)} runtime module(s) still read '
                'ClientProfile/Project')
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
        else:
            self.stdout.write(self.style.SUCCESS(
                'All checkable preconditions satisfied.'))
            self.stdout.write(
                'Still confirm by hand: verified backup, timed '
                'PostgreSQL-to-PostgreSQL rehearsal, Waves 1-5 observed.')

    def _runtime_readers(self):
        root = pathlib.Path('.')
        hits = []
        for path in sorted(root.rglob('*.py')):
            text = str(path).replace('\\', '/')
            if any(part in text for part in ('myvenv', 'node_modules')):
                continue
            if 'test' in path.name:
                continue
            if any(allowed in text for allowed in _ALLOWED_REFERENCES):
                continue
            try:
                source = path.read_text(encoding='utf-8', errors='replace')
            except Exception:
                continue
            if 'ClientProfile' in source or 'clients.Project' in source:
                hits.append(text)
        return hits
