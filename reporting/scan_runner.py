"""
Vulnerability-scan orchestrator.

`run_full_scan(scan_id)` is the single entry point — called by the
Celery task wrapper, by the on-demand admin button, and (for testing)
synchronously from `manage.py shell`. It walks the scan_type matrix,
runs the relevant tools, persists raw output + parsed findings, and
flips the scan's status to complete/failed.

Each tool's results land on the scan row immediately (`update_fields`)
so a long-running full scan still surfaces partial progress to the UI.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from reporting.models import VulnerabilityFinding, VulnerabilityScan
from reporting.scanners import (
    normalize_findings, run_nikto_scan, run_nmap_scan,
    run_ssl_scan, run_wpscan,
)

logger = logging.getLogger(__name__)


# Which tools run for each scan_type — keyed so the orchestrator stays
# declarative and easy to extend.
_TOOLS_BY_TYPE = {
    'full':  {'nmap', 'nikto', 'ssl', 'wpscan'},
    'ports': {'nmap'},
    'web':   {'nikto', 'wpscan'},
    'ssl':   {'ssl'},
    'quick': {'nmap', 'ssl'},
}


def run_full_scan(scan_id):
    """
    Execute the scan identified by `scan_id`. Idempotent on partial
    failure — re-running will re-walk the matrix and overwrite the
    raw_* JSON fields, so retries don't double-count findings (the
    findings table is wiped at the start).
    """
    try:
        scan = VulnerabilityScan.objects.get(id=scan_id)
    except VulnerabilityScan.DoesNotExist:
        logger.warning('run_full_scan: scan %s not found', scan_id)
        return

    scan.status = 'running'
    scan.started_at = timezone.now()
    scan.error_message = ''
    scan.save(update_fields=['status', 'started_at', 'error_message',
                             'updated_at'])
    # Idempotent retry — clear old findings before recounting.
    VulnerabilityFinding.objects.filter(scan=scan).delete()

    tools = _TOOLS_BY_TYPE.get(scan.scan_type, _TOOLS_BY_TYPE['full'])
    all_findings = []

    try:
        if 'nmap' in tools and scan.target_ip:
            nmap_timeout = 60 if scan.scan_type == 'quick' else 120
            res = run_nmap_scan(scan.target_ip, timeout=nmap_timeout)
            scan.raw_nmap = res
            scan.save(update_fields=['raw_nmap', 'updated_at'])
            all_findings.extend(normalize_findings(
                res.get('findings') or [], 'nmap'))

        if 'nikto' in tools and scan.target_url:
            res = run_nikto_scan(scan.target_url, timeout=180)
            scan.raw_nikto = res
            scan.save(update_fields=['raw_nikto', 'updated_at'])
            all_findings.extend(normalize_findings(
                res.get('findings') or [], 'nikto'))

        if 'ssl' in tools and scan.target_url:
            from reporting.scanners import _strip_to_domain
            domain = _strip_to_domain(scan.target_url)
            res = run_ssl_scan(domain, timeout=30)
            scan.raw_ssl = res
            scan.save(update_fields=['raw_ssl', 'updated_at'])
            all_findings.extend(normalize_findings(
                res.get('findings') or [], 'ssl'))

        if 'wpscan' in tools and scan.target_url:
            res = run_wpscan(scan.target_url, timeout=120)
            scan.raw_wpscan = res
            scan.save(update_fields=['raw_wpscan', 'updated_at'])
            if not res.get('skipped'):
                all_findings.extend(normalize_findings(
                    res.get('findings') or [], 'wpscan'))

        # Persist findings + counts.
        counts = {k: 0 for k in
                  ('critical', 'high', 'medium', 'low', 'info')}
        objects = []
        for f in all_findings:
            severity = f.get('severity', 'info')
            if severity in counts:
                counts[severity] += 1
            objects.append(VulnerabilityFinding(
                scan=scan,
                tool=f['tool'],
                title=f['title'],
                severity=severity,
                description=f.get('description', ''),
                recommendation=f.get('recommendation', ''),
                evidence=f.get('evidence', ''),
                cve_id=f.get('cve_id', ''),
            ))
        VulnerabilityFinding.objects.bulk_create(objects)

        scan.findings_count = len(objects)
        scan.critical_count = counts['critical']
        scan.high_count = counts['high']
        scan.medium_count = counts['medium']
        scan.low_count = counts['low']
        scan.info_count = counts['info']
        scan.status = 'complete'
        scan.completed_at = timezone.now()
        scan.save()

        if counts['critical'] or counts['high']:
            _notify_admin_scan_complete(scan)

    except Exception as exc:  # noqa: BLE001 — orchestrator catch-all
        logger.exception('Vulnerability scan failed: %s', scan_id)
        scan.status = 'failed'
        scan.error_message = str(exc)[:500]
        scan.completed_at = timezone.now()
        scan.save(update_fields=['status', 'error_message',
                                 'completed_at', 'updated_at'])


def _notify_admin_scan_complete(scan):
    """Email the admin when a scan turns up critical / high findings."""
    subject = (f'[Scan] {scan.client.firm_name} — '
               f'{scan.critical_count} Critical / '
               f'{scan.high_count} High')
    message = (
        f'Vulnerability scan complete for {scan.client.firm_name}.\n\n'
        f'Findings:\n'
        f'  Critical: {scan.critical_count}\n'
        f'  High:     {scan.high_count}\n'
        f'  Medium:   {scan.medium_count}\n'
        f'  Low:      {scan.low_count}\n\n'
        f'Review at:\n'
        f'{settings.SITE_BASE_URL}/admin-dashboard/scans/{scan.id}/\n'
    )
    try:
        send_mail(
            subject, message,
            getattr(settings, 'EMAIL_FROM_NO_REPLY',
                    settings.DEFAULT_FROM_EMAIL),
            [settings.LEAD_NOTIFICATION_EMAIL],
            fail_silently=True,
        )
    except Exception:
        logger.exception('Failed to send scan-complete email')
