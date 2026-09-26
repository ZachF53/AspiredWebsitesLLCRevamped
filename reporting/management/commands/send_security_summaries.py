"""
Manually run the monthly security summary — the same code path as the
`send-security-summaries` beat task (reporting.security_summary.
run_security_summaries).

    python manage.py send_security_summaries --dry-run
    python manage.py send_security_summaries --month 2026-09
    python manage.py send_security_summaries --website <uuid> --resend

Without --month it reports on the previous month, exactly like the
1st-of-month beat run. Reports already sent for that month are skipped
unless --resend is given.
"""

import uuid
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from reporting.security_summary import previous_month, run_security_summaries


class Command(BaseCommand):
    help = ('Generate and email the monthly one-page security summary to '
            'every site on a paid plan (same path as the beat task).')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='List who would get a summary and how fresh their scan '
                 'data is. Writes nothing, sends nothing.')
        parser.add_argument(
            '--website', action='append', default=[],
            help='Website UUID to limit the run to (repeatable). The site '
                 'must still be on an eligible plan.')
        parser.add_argument(
            '--month',
            help='Report month as YYYY-MM (default: previous month).')
        parser.add_argument(
            '--resend', action='store_true',
            help='Re-send summaries already marked sent for the month.')

    def handle(self, *args, **opts):
        month = opts.get('month')
        if month:
            try:
                year, mon = (int(p) for p in month.split('-'))
                report_month = date(year, mon, 1)
            except (ValueError, TypeError):
                raise CommandError('--month must be YYYY-MM, e.g. 2026-09')
        else:
            report_month = previous_month()

        website_ids = []
        for raw in opts.get('website') or []:
            try:
                website_ids.append(uuid.UUID(raw))
            except ValueError:
                raise CommandError(f'--website {raw!r} is not a UUID')

        result = run_security_summaries(
            report_month=report_month,
            website_ids=website_ids or None,
            dry_run=opts['dry_run'],
            resend=opts['resend'],
        )

        label = 'DRY RUN — ' if opts['dry_run'] else ''
        self.stdout.write(
            f"{label}Security summaries for {report_month:%B %Y}: "
            f"{len(result['sites'])} eligible site(s)")
        for row in result['sites']:
            line = (f"  {row['name']} ({row['website_id']}) -> "
                    f"{row['recipient'] or '(no email)'}: "
                    f"{row.get('outcome', '?')}")
            if opts['dry_run']:
                line += (f" | scan: {row['scan'] or 'NONE FRESH'}"
                         f" | server check: "
                         f"{row['droplet_check'] or ('NONE FRESH' if row['hosted'] else 'n/a (not hosted)')}")
            self.stdout.write(line)
        if not opts['dry_run']:
            self.stdout.write(
                f"Sent {result['sent']}, failed {result['failed']}, "
                f"skipped {result['skipped']}.")
        for name, reason in result.get('problems') or []:
            self.stdout.write(self.style.WARNING(f'  ! {name}: {reason}'))
