"""
Monthly 1-page security summary — a standalone per-client PDF combining
the latest completed VulnerabilityScan (external scan findings) and the
latest completed DropletHealthCheck (SSH-based audit; absent entirely
for WordPress sites). Deliberately separate from the multi-page
performance MonthlyReport clients already get — kept a true 1-pager,
not folded into that report as another growing section.

`generate_security_summary(website, report_month)` is the entry point,
used by both the monthly Celery task and (for testing / on-demand
regeneration) directly.
"""

import logging
import os

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from clients.display import owner_label
from reporting.models import DropletHealthCheck, SecuritySummaryReport, VulnerabilityScan

logger = logging.getLogger(__name__)


def _overall_status(open_critical, open_high, disk_percent,
                     pending_updates, pip_vulns, services_down):
    """Simple, documented heuristic — red on anything actively broken
    or exploitable, yellow on anything that needs attention but isn't
    urgent, green otherwise."""
    if open_critical > 0 or services_down > 0:
        return 'red'
    if disk_percent is not None and disk_percent >= 90:
        return 'red'
    if open_high > 0 or pip_vulns:
        return 'yellow'
    if pending_updates is not None and pending_updates > 10:
        return 'yellow'
    return 'green'


def generate_security_summary(website, report_month):
    """
    Create/update this website's SecuritySummaryReport for
    `report_month` (any date within the month — normalised to the 1st),
    render the PDF, and return the report row.

    Idempotent per (website, report_month) — unique_together on the
    model means a re-run updates the same row rather than creating a
    duplicate, same pattern as MonthlyReport.
    """
    report_month = report_month.replace(day=1)

    report, _ = SecuritySummaryReport.objects.get_or_create(
        website_new=website, report_month=report_month,
        defaults={'status': 'generating'})

    scan = (VulnerabilityScan.objects
            .filter(website_new=website, status='complete')
            .order_by('-completed_at').first())
    droplet_check = (DropletHealthCheck.objects
                      .filter(website_new=website, status='complete')
                      .order_by('-completed_at').first())

    open_critical = open_high = 0
    if scan is not None:
        open_critical = scan.findings.filter(
            severity='critical', status='open').count()
        open_high = scan.findings.filter(
            severity='high', status='open').count()

    disk_percent = pending_updates = pip_vulns = None
    services_down_count = 0
    if droplet_check is not None:
        disk_percent = droplet_check.disk_usage_percent
        pending_updates = droplet_check.pending_os_updates_count
        pip_vulns = droplet_check.pip_audit_vulnerability_count
        services_down_count = len(droplet_check.services_down or [])

    overall = _overall_status(
        open_critical, open_high, disk_percent, pending_updates,
        pip_vulns or 0, services_down_count)

    owner_name = owner_label(website)
    is_wordpress = website.build_platform == 'wordpress'
    month_str = report_month.strftime('%B %Y')

    html_string = render_to_string('reporting/security_summary.html', {
        'owner_name': owner_name,
        'month_str': month_str,
        'overall_status': overall,
        'scan': scan,
        'droplet_check': droplet_check,
        'open_critical_count': open_critical,
        'open_high_count': open_high,
        'disk_usage_percent': disk_percent,
        'pending_os_updates_count': pending_updates,
        'pip_audit_vulnerability_count': pip_vulns,
        'services_down_count': services_down_count,
        'is_wordpress': is_wordpress,
    })

    rel_dir = os.path.join('security-summaries', str(website.id))
    abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    date_slug = report_month.strftime('%Y-%m')
    pdf_filename = f'summary-{date_slug}.pdf'
    rel_pdf_path = os.path.join(rel_dir, pdf_filename)
    abs_pdf_path = os.path.join(abs_dir, pdf_filename)

    rendered_rel = rel_pdf_path
    try:
        from weasyprint import HTML
        HTML(string=html_string,
             base_url=settings.MEDIA_ROOT).write_pdf(abs_pdf_path)
    except Exception as exc:  # noqa: BLE001 — WeasyPrint / GTK / lib gaps
        logger.warning(
            'WeasyPrint failed for security summary %s (%s) — falling '
            'back to HTML', report.id, exc)
        html_filename = f'summary-{date_slug}.html'
        rendered_rel = os.path.join(rel_dir, html_filename)
        with open(os.path.join(abs_dir, html_filename),
                  'w', encoding='utf-8') as fh:
            fh.write(html_string)

    report.pdf_path = rendered_rel
    report.status = 'ready'
    report.latest_scan = scan
    report.latest_droplet_check = droplet_check
    report.open_critical_count = open_critical
    report.open_high_count = open_high
    report.disk_usage_percent = disk_percent
    report.pending_os_updates_count = pending_updates
    report.pip_audit_vulnerability_count = pip_vulns
    report.services_down_count = services_down_count
    report.overall_status = overall
    report.save()
    return report
