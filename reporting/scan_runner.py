"""
Vulnerability-scan orchestrator.

`run_full_scan(scan_id)` is the single entry point — called by the
Celery task wrapper, by the on-demand admin button, and (for testing)
synchronously from `manage.py shell`. It walks the scan_type matrix,
runs the relevant tools, persists raw output + parsed findings, and
flips the scan's status to complete/failed.

Each tool's results land on the scan row immediately (`update_fields`)
so a long-running full scan still surfaces partial progress to the UI.

`generate_scan_pdf(scan_id)` renders the branded PDF report. WeasyPrint
on prod (GTK in the base snapshot); on Windows dev the call to
`HTML.write_pdf` raises and we fall back to a .html sibling — same
pattern as `clients/pdf_utils.py`.
"""

import logging
import os
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
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

        # Auto-render the PDF so the dashboard's Download Report button
        # works the moment the scan flips to complete. Errors here don't
        # fail the scan — admins can still regenerate from the UI.
        try:
            generate_scan_pdf(str(scan.id))
        except Exception:
            logger.exception(
                'Vulnerability scan %s — auto PDF generation failed',
                scan.id)

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
    """
    Notify the right party when a scan turns up critical / high findings.

    If `client.auto_send_scan_reports` is True, the client gets the PDF
    by email (silently — no admin alert). Otherwise the admin gets a
    Needs You alert and decides per-scan whether to send.
    """
    # Auto-send path — fire and forget; failure falls back to the admin
    # alert below so a missed email doesn't get silently swallowed.
    if getattr(scan.client, 'auto_send_scan_reports', False):
        try:
            _auto_send_to_client(scan)
            return
        except Exception:
            logger.exception(
                'auto-send failed for scan %s — falling back to admin alert',
                scan.id)

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


# ── PDF report generation ──────────────────────────────────────────────────

# Inner severity order — keeps the rendered findings sections in the same
# order regardless of how the scan happened to store them.
_SEVERITY_SECTION_META = [
    ('critical', 'Critical', '🔴', 'crit'),
    ('high',     'High',     '🟠', 'high'),
    ('medium',   'Medium',   '🟡', 'med'),
    ('low',      'Low',      '🔵', 'low'),
    ('info',     'Info',     'ℹ',  'info'),
]


def _ssl_grade_class(grade):
    """Map the SSL Labs grade letter to a CSS class for the cover circle."""
    if not grade:
        return None
    first = grade.strip()[:1].upper()
    if first == 'A':
        return 'a'
    if first == 'B':
        return 'b'
    if first == 'C':
        return 'c'
    if first in ('D', 'E', 'F', 'T', 'M'):
        return 'f'
    return None


def _tools_used_summary(scan):
    """Compact `["nmap (3 findings)", …]` list for the cover stats."""
    out = []
    raws = (
        ('nmap', 'nmap', scan.raw_nmap),
        ('Nikto', 'Nikto', scan.raw_nikto),
        ('SSL Labs', 'SSL Labs', scan.raw_ssl),
        ('WPScan', 'WPScan', scan.raw_wpscan),
    )
    for label, _, raw in raws:
        raw = raw or {}
        if not raw:
            continue
        if raw.get('skipped'):
            out.append(f'{label} (skipped)')
            continue
        if raw.get('error'):
            out.append(f'{label} (error)')
            continue
        if label == 'SSL Labs':
            out.append(f'SSL Labs (Grade {raw.get("grade") or "—"})')
        else:
            n = len(raw.get('findings') or [])
            out.append(f'{label} ({n} finding{"" if n == 1 else "s"})')
    return out


def generate_scan_pdf(scan_id) -> str | None:
    """
    Render the vulnerability report for `scan_id` to PDF and stash the
    path on the scan. Returns the absolute path, or None on failure.

    Storage layout: ``MEDIA_ROOT/scans/<client-id>/scan-report-YYYY-MM-DD.pdf``.
    The stored ``scan.pdf_path`` is RELATIVE to MEDIA_ROOT so it survives
    a MEDIA_ROOT move (the download view re-joins with the current root).

    Skips false-positive findings; accepted-risk ones render with the
    acceptance note inline. WeasyPrint on prod; HTML sibling fallback on
    Windows dev when GTK/libgobject isn't available.
    """
    try:
        scan = VulnerabilityScan.objects.select_related('client').get(
            id=scan_id)
    except VulnerabilityScan.DoesNotExist:
        logger.warning('generate_scan_pdf: scan %s not found', scan_id)
        return None
    if scan.status != 'complete':
        logger.info('generate_scan_pdf: scan %s not complete — skipping',
                    scan_id)
        return None

    # Findings, grouped and ordered — exclude false positives entirely;
    # keep accepted_risk so the recipient sees the full picture + the note.
    findings = list(
        scan.findings.exclude(status='false_positive')
        .order_by('severity', 'tool', 'title')
    )
    by_sev = {sev: [] for sev, *_ in _SEVERITY_SECTION_META}
    for f in findings:
        by_sev.setdefault(f.severity, []).append(f)
    severity_sections = [
        {'severity': sev, 'label': label, 'glyph': glyph,
         'bar_class': bar_class, 'items': by_sev.get(sev) or []}
        for sev, label, glyph, bar_class in _SEVERITY_SECTION_META
    ]

    duration = None
    if scan.started_at and scan.completed_at:
        duration = int(
            (scan.completed_at - scan.started_at).total_seconds())

    ssl_grade = (scan.raw_ssl or {}).get('grade')

    next_scan = None
    if scan.completed_at:
        next_scan = scan.completed_at + timedelta(days=30)

    context = {
        'scan': scan,
        'client': scan.client,
        'grouped_findings': by_sev,
        'severity_sections': severity_sections,
        'open_count': sum(1 for f in findings if f.status == 'open'),
        'ssl_grade': ssl_grade,
        'ssl_grade_class': _ssl_grade_class(ssl_grade),
        'tools_used': _tools_used_summary(scan),
        'duration_seconds': duration,
        'next_scan_date': next_scan,
        'report_date': scan.completed_at or timezone.now(),
    }
    html_string = render_to_string(
        'reporting/vulnerability_report.html', context)

    # Filesystem layout under MEDIA_ROOT — relative path is what we store
    # on the model so a future media-root move doesn't break old rows.
    rel_dir = os.path.join('scans', str(scan.client.id))
    abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)

    date_slug = (scan.completed_at or timezone.now()).strftime('%Y-%m-%d')
    pdf_filename = f'scan-report-{date_slug}.pdf'
    rel_pdf_path = os.path.join(rel_dir, pdf_filename)
    abs_pdf_path = os.path.join(abs_dir, pdf_filename)

    rendered_path = abs_pdf_path
    rendered_rel = rel_pdf_path
    try:
        from weasyprint import HTML
        HTML(string=html_string,
             base_url=settings.MEDIA_ROOT).write_pdf(abs_pdf_path)
    except Exception as exc:  # noqa: BLE001 — WeasyPrint / GTK / lib gaps
        # Windows-dev fallback: drop an HTML sibling so the file path
        # is at least openable — same pattern as clients/pdf_utils.py.
        logger.warning(
            'WeasyPrint failed for scan %s (%s) — falling back to HTML',
            scan_id, exc)
        html_filename = f'scan-report-{date_slug}.html'
        rendered_rel = os.path.join(rel_dir, html_filename)
        rendered_path = os.path.join(abs_dir, html_filename)
        with open(rendered_path, 'w', encoding='utf-8') as fh:
            fh.write(html_string)

    scan.pdf_path = rendered_rel
    scan.save(update_fields=['pdf_path', 'updated_at'])
    return rendered_path


def _auto_send_to_client(scan):
    """
    SendGrid-deliver the freshly rendered PDF to the client when
    `auto_send_scan_reports` is on. Raises on any failure so the caller
    in `_notify_admin_scan_complete` can fall back to the admin alert.
    """
    import base64
    import os

    from django.conf import settings
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import (
        Attachment, Disposition, FileContent, FileName, FileType, Mail,
    )

    client = scan.client
    client_email = client.user.email if client.user else ''
    if not client_email:
        raise RuntimeError('client has no email on file')

    abs_path = (os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
                if scan.pdf_path else None)
    if not abs_path or not os.path.exists(abs_path):
        # Re-render once more — the auto-render hook may have failed.
        generate_scan_pdf(str(scan.id))
        scan.refresh_from_db()
        abs_path = (os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
                    if scan.pdf_path else None)
    if not abs_path or not os.path.exists(abs_path):
        raise RuntimeError('PDF not on disk after render')

    month_str = (scan.completed_at or scan.created_at).strftime('%B %Y')
    severity_line = (
        f'{scan.critical_count} critical and {scan.high_count} high '
        f'severity issue(s) were identified that require attention.'
        if (scan.critical_count or scan.high_count) else
        'No critical or high severity issues were detected. '
        'Your site is in good standing.'
    )
    contact_name = client.contact_name or client.firm_name
    html_content = (
        f'<p>Hi {contact_name},</p>'
        f'<p>Your monthly security assessment for {month_str} is attached.</p>'
        f'<p>{severity_line}</p>'
        f'<p>Full history in your portal: '
        f"<a href='{settings.SITE_BASE_URL}/portal/security/'>"
        f"Security Reports</a></p>"
        f'<p>— Zachery Long<br>Aspired Websites LLC<br>'
        f'210-896-2536<br>zacherylong@aspiredwebsites.com</p>'
    )

    # SDK path — append the legal address footer manually (this
    # bypasses Django's mail backend, so AspiredEmailBackend's
    # signature work doesn't fire here automatically).
    from core.email_signature import append_signature
    _, html_content = append_signature(html=html_content)

    message = Mail(
        from_email=getattr(settings, 'EMAIL_FROM_NO_REPLY',
                           settings.DEFAULT_FROM_EMAIL),
        to_emails=client_email,
        subject=(f'Your Security Report — {month_str} — '
                 f'{client.firm_name}'),
        html_content=html_content,
    )
    with open(abs_path, 'rb') as fh:
        encoded = base64.b64encode(fh.read()).decode()
    ext = os.path.splitext(abs_path)[1] or '.pdf'
    mime = 'application/pdf' if ext == '.pdf' else 'text/html'
    message.attachment = Attachment(
        FileContent(encoded),
        FileName(f'security-report-{month_str}{ext}'),
        FileType(mime),
        Disposition('attachment'),
    )

    SendGridAPIClient(settings.SENDGRID_API_KEY).send(message)

    scan.sent_to_client = True
    scan.sent_at = timezone.now()
    scan.save(update_fields=['sent_to_client', 'sent_at', 'updated_at'])
    logger.info('auto-sent scan PDF for %s to %s', scan.id, client_email)
