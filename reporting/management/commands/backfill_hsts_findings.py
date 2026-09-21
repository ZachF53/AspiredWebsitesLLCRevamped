"""
Add the "HSTS not enabled" finding to existing scans whose SSL Labs
raw_data already shows hstsPolicy.status == 'absent'.

Why this exists: reporting/scanners.py::run_ssl_scan now raises an
explicit low-severity finding when HSTS is missing, but that only
happens when the scan actually runs. Every VulnerabilityScan already
sitting in the database ran the SSL Labs check before this existed —
the full raw_data (including hstsPolicy) is already stored on
scan.raw_ssl, so the finding can be derived from data already on hand
without re-hitting the SSL Labs API.

Idempotent — skips a scan that already has an "HSTS not enabled"
finding.

Usage:
  python manage.py backfill_hsts_findings --dry-run
  python manage.py backfill_hsts_findings
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Add the "HSTS not enabled" finding to existing scans whose '
        'already-stored SSL Labs data shows HSTS is absent.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would change; write nothing.')

    def handle(self, *args, **options):
        from reporting.models import VulnerabilityFinding, VulnerabilityScan
        from reporting.scanners import _hsts_finding, _hsts_missing

        dry_run = options['dry_run']

        scans = VulnerabilityScan.objects.exclude(raw_ssl={})
        added = 0

        for scan in scans:
            raw_ssl = scan.raw_ssl or {}
            details = (((raw_ssl.get('raw_data') or {}).get('endpoints')
                        or [{}])[0].get('details') or {})
            if not _hsts_missing(details):
                continue
            if VulnerabilityFinding.objects.filter(
                    scan=scan, tool='ssl', title='HSTS not enabled').exists():
                continue

            domain = scan.target_url or (scan.website_new.url
                                          if scan.website_new else '')
            f = _hsts_finding(domain)
            self.stdout.write(
                f'  + HSTS not enabled  [{scan.id}]'
                + (' [DRY RUN]' if dry_run else ''))
            added += 1
            if not dry_run:
                VulnerabilityFinding.objects.create(
                    scan=scan, tool='ssl', severity=f['severity'],
                    title=f['title'], description=f['description'],
                    recommendation=f['recommendation'],
                    evidence=f['evidence'],
                )
                scan.low_count = scan.findings.filter(
                    severity='low').count()
                scan.findings_count = scan.findings.count()
                scan.save(update_fields=[
                    'low_count', 'findings_count', 'updated_at'])

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. findings added: {added}'))
        if dry_run:
            self.stdout.write(self.style.WARNING(
                'No writes performed. Re-run without --dry-run to apply.'))
