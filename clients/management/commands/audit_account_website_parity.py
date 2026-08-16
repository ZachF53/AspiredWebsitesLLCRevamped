"""Report migration gaps between ClientProfile/Project and Account/Website."""

import json

from django.core.management.base import BaseCommand, CommandError

from clients.parity import audit_account_website_parity


class Command(BaseCommand):
    help = (
        'Read-only Phase-D audit for Account/Website ownership, canonical '
        'foreign keys, field drift, and duplicate external identifiers.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--strict', action='store_true',
            help='Exit non-zero when structural errors are present.')
        parser.add_argument(
            '--fail-on-warnings', action='store_true',
            help='With --strict, also exit non-zero for drift/review warnings.')
        parser.add_argument(
            '--fail-on-operational', action='store_true',
            help='With --strict, also exit non-zero while operational items '
                 '(e.g. fully_paid without ledger evidence) are unresolved.')
        parser.add_argument(
            '--json', action='store_true', dest='as_json',
            help='Emit machine-readable JSON.')
        parser.add_argument(
            '--detail-limit', type=int, default=20,
            help='Maximum example rows shown per finding (default: 20).')

    def handle(self, *args, **options):
        report = audit_account_website_parity(
            detail_limit=max(options['detail_limit'], 0))

        if options['as_json']:
            self.stdout.write(json.dumps(report.as_dict(), indent=2))
        else:
            self.stdout.write('Account/Website parity audit (read-only)')
            for label, count in report.counts.items():
                self.stdout.write(f'  {label}: {count}')

            if report.findings:
                self.stdout.write('')
                for finding in report.findings:
                    self.stdout.write(
                        f'[{finding.severity.upper()}] {finding.code}: '
                        f'{finding.count}')
                    for example in finding.examples:
                        self.stdout.write(f'    {example}')
            else:
                self.stdout.write('\nNo parity findings.')

            self.stdout.write(
                f'\nTotals: {report.error_count} error occurrence(s), '
                f'{report.warning_count} warning occurrence(s), '
                f'{report.operational_count} operational item(s).')

            if report.operational_count:
                self.stdout.write(self.style.WARNING(
                    'Operational items are UNRESOLVED and need a human to '
                    'verify them against the real world. They do not block '
                    'the migration gate; they block the affected site from '
                    'launching.'))

        should_fail = report.error_count > 0
        if options['fail_on_warnings']:
            should_fail = should_fail or report.warning_count > 0
        if options['fail_on_operational']:
            should_fail = should_fail or report.operational_count > 0
        if options['strict'] and should_fail:
            raise CommandError('Account/Website parity audit failed.')
