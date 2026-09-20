"""
Recompute VulnerabilityFinding.severity for existing Nikto findings
using the current `_classify_nikto_msg` heuristic.

Why this exists: severity is computed once, at scan time, and saved —
it never re-evaluates itself. When reporting/scanners.py's Nikto
classifier is fixed (see the "uncommon header" false-positive fix,
where security-positive headers like X-XSS-Protection/X-Frame-Options
were scored CRITICAL/MEDIUM instead of INFO), every finding already
sitting in the database keeps its stale, wrong severity forever unless
something reclassifies it.

`finding.title` is the exact Nikto message text (`msg[:200]` at parse
time in reporting/scanners.py::_parse_nikto_xml), so severity can be
recomputed directly from stored data — no need to re-run the scan.

After updating findings, each affected VulnerabilityScan's denormalized
counts (critical_count/high_count/medium_count/low_count/info_count)
are recomputed to match.

Idempotent — a finding whose recomputed severity matches its current
value is left untouched.

Usage:
  python manage.py reclassify_nikto_findings --dry-run
  python manage.py reclassify_nikto_findings
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Recompute severity for existing Nikto findings against the '
        'current classifier and refresh each affected scan\'s counts.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would change; write nothing.')

    def handle(self, *args, **options):
        from reporting.models import VulnerabilityFinding, VulnerabilityScan
        from reporting.scanners import _classify_nikto_msg

        dry_run = options['dry_run']

        findings = VulnerabilityFinding.objects.filter(tool='nikto')
        changed = 0
        touched_scan_ids = set()

        for f in findings:
            new_sev = _classify_nikto_msg(f.title)
            if new_sev == f.severity:
                continue
            self.stdout.write(
                f'  {f.severity} -> {new_sev}  [{f.scan_id}]  {f.title[:80]}'
                + (' [DRY RUN]' if dry_run else ''))
            changed += 1
            touched_scan_ids.add(f.scan_id)
            if not dry_run:
                f.severity = new_sev
                f.save(update_fields=['severity', 'updated_at'])

        rescanned = 0
        if not dry_run:
            for scan in VulnerabilityScan.objects.filter(
                    id__in=touched_scan_ids):
                counts = {k: 0 for k in
                          ('critical', 'high', 'medium', 'low', 'info')}
                for sev in scan.findings.values_list('severity', flat=True):
                    if sev in counts:
                        counts[sev] += 1
                scan.critical_count = counts['critical']
                scan.high_count = counts['high']
                scan.medium_count = counts['medium']
                scan.low_count = counts['low']
                scan.info_count = counts['info']
                scan.save(update_fields=[
                    'critical_count', 'high_count', 'medium_count',
                    'low_count', 'info_count', 'updated_at'])
                rescanned += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. findings reclassified: {changed}  '
            f'scans recounted: {rescanned if not dry_run else len(touched_scan_ids)}'))
        if dry_run:
            self.stdout.write(self.style.WARNING(
                'No writes performed. Re-run without --dry-run to apply.'))
