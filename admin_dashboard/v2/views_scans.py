"""
v2 vulnerability-scan detail/list views.

v1 (admin_dashboard/views_scans.py) is untouched and stays as the
system of record for admins who haven't switched to v2. These are thin
v2-styled adaptations: same models, same Celery task, same PDF
generator, same email helper — only the templates, URL names, and
plain render() (no _admin_context, which is a v1-only helper) differ.
The row-decoration/formatting helpers are imported from v1's module
rather than duplicated.
"""

import logging
import os

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from admin_dashboard.decorators import admin_required
from admin_dashboard.views_scans import (
    _build_scan_rows,
    _build_tool_blocks,
    _ssl_grade_class,
)
from clients.display import owner_label

logger = logging.getLogger(__name__)


@admin_required
def scans_list(request):
    """Fleet-wide scan dashboard — v2 equivalent of v1's scans_list."""
    from clients.account_models import Website
    from reporting.models import VulnerabilityScan

    website_id = (request.GET.get('website') or '').strip()
    status = (request.GET.get('status') or '').strip()

    qs = (VulnerabilityScan.objects
          .select_related('website_new', 'website_new__account')
          .order_by('-created_at'))
    if website_id:
        qs = qs.filter(website_new_id=website_id)
    if status:
        qs = qs.filter(status=status)

    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get('page', 1))
    rows = _build_scan_rows(page.object_list)

    pending_count = VulnerabilityScan.objects.filter(status='pending').count()
    running_count = VulnerabilityScan.objects.filter(status='running').count()

    websites = (Website.objects
                .filter(status='active', account__status='active')
                .select_related('account')
                .order_by('account__name', 'name'))

    qs_params = request.GET.copy()
    qs_params.pop('page', None)

    return render(request, 'admin_dashboard/v2/scans_list.html', {
        'rows': rows,
        'page': page,
        'paginator': paginator,
        'total_scans': qs.count(),
        'pending_count': pending_count,
        'running_count': running_count,
        'websites': websites,
        'selected_website': website_id,
        'selected_status': status,
        'status_choices': VulnerabilityScan.STATUS_CHOICES,
        'type_choices': VulnerabilityScan.SCAN_TYPE_CHOICES,
        'filter_qs': qs_params.urlencode(),
        'auto_refresh': (pending_count + running_count) > 0,
    })


@admin_required
def scans_table(request):
    """HTMX partial — table rows only, polled while scans are in flight."""
    from reporting.models import VulnerabilityScan

    website_id = (request.GET.get('website') or '').strip()
    status = (request.GET.get('status') or '').strip()

    qs = (VulnerabilityScan.objects
          .select_related('website_new', 'website_new__account')
          .order_by('-created_at'))
    if website_id:
        qs = qs.filter(website_new_id=website_id)
    if status:
        qs = qs.filter(status=status)
    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get('page', 1))
    rows = _build_scan_rows(page.object_list)

    return render(request, 'admin_dashboard/v2/_scan_rows.html',
                  {'rows': rows, 'page': page})


@admin_required
def scan_detail(request, scan_id):
    """Severity-grouped findings + per-tool summary — v2 styling."""
    from reporting.models import VulnerabilityScan

    scan = get_object_or_404(
        VulnerabilityScan.objects.select_related(
            'website_new', 'website_new__account'),
        id=scan_id,
    )

    if not scan.been_reviewed:
        scan.been_reviewed = True
        scan.reviewed_at = timezone.now()
        scan.save(update_fields=['been_reviewed', 'reviewed_at', 'updated_at'])

    findings = list(scan.findings.order_by('severity', 'tool', 'title'))
    by_sev = {sev: [] for sev in ('critical', 'high', 'medium', 'low', 'info')}
    for f in findings:
        by_sev.setdefault(f.severity, []).append(f)

    sev_meta = [
        ('critical', 'Critical', '🔴', True),
        ('high',     'High',     '🟠', True),
        ('medium',   'Medium',   '🟡', False),
        ('low',      'Low',      '🔵', False),
        ('info',     'Info',     'ℹ',  False),
    ]
    severity_groups = [
        {'severity': sev, 'label': label, 'glyph': glyph,
         'open_by_default': by_default and bool(by_sev.get(sev)),
         'items': by_sev.get(sev) or []}
        for sev, label, glyph, by_default in sev_meta
    ]

    duration = None
    if scan.started_at and scan.completed_at:
        duration = int((scan.completed_at - scan.started_at).total_seconds())

    ssl_grade = (scan.raw_ssl or {}).get('grade')
    open_count = sum(1 for f in findings if f.status == 'open')

    return render(request, 'admin_dashboard/v2/scan_detail.html', {
        'scan': scan,
        'severity_groups': severity_groups,
        'findings_total': len(findings),
        'open_count': open_count,
        'duration_seconds': duration,
        'tool_blocks': _build_tool_blocks(scan),
        'ssl_grade': ssl_grade,
        'ssl_grade_class': _ssl_grade_class(ssl_grade),
    })


@admin_required
@require_POST
def scan_cancel(request, scan_id):
    """Revoke a stuck scan's Celery task and mark it cancelled."""
    from reporting.models import VulnerabilityScan

    scan = get_object_or_404(VulnerabilityScan, id=scan_id)
    if scan.status not in ('pending', 'running'):
        messages.info(
            request,
            f'This scan is already {scan.get_status_display().lower()} — '
            f'nothing to cancel.')
        return redirect('admin_dashboard:v2_scan_detail', scan_id=scan_id)

    if scan.celery_task_id:
        try:
            from AspiredWebsitesRevamped.celery import app as celery_app
            celery_app.control.revoke(
                scan.celery_task_id, terminate=True, signal='SIGTERM')
        except Exception:
            logger.exception(
                'v2 scan_cancel: revoke failed for task %s',
                scan.celery_task_id)

    scan.status = 'cancelled'
    scan.completed_at = timezone.now()
    scan.error_message = (
        f'Cancelled by admin ({request.user}) at '
        f'{timezone.now().isoformat()}'
    )[:2000]
    scan.save(update_fields=[
        'status', 'completed_at', 'error_message', 'updated_at'])
    messages.success(
        request,
        f'Scan cancelled. '
        f'{"Worker process killed." if scan.celery_task_id else ""}')
    return redirect('admin_dashboard:v2_scan_detail', scan_id=scan_id)


@admin_required
def scan_generate_pdf(request, scan_id):
    """Regenerate the scan PDF and stream it back as an attachment."""
    from reporting.models import VulnerabilityScan
    from reporting.scan_runner import generate_scan_pdf

    scan = get_object_or_404(
        VulnerabilityScan.objects.select_related('website_new'), id=scan_id)

    pdf_path = generate_scan_pdf(str(scan.id))
    if not pdf_path:
        messages.error(request, 'PDF generation failed — check server logs.')
        return redirect('admin_dashboard:v2_scan_detail', scan_id=scan_id)

    scan.refresh_from_db()
    abs_path = os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
    ext = os.path.splitext(abs_path)[1] or '.pdf'
    slug = owner_label(scan).replace(' ', '-')
    month = (scan.completed_at or scan.created_at).strftime('%Y-%m')
    filename = f'security-report-{slug}-{month}{ext}'
    return FileResponse(
        open(abs_path, 'rb'),
        as_attachment=True,
        filename=filename,
        content_type='application/pdf' if ext == '.pdf' else 'text/html',
    )


@admin_required
def scan_download_pdf(request, scan_id):
    """Serve the already-generated scan PDF (or HTML fallback)."""
    from reporting.models import VulnerabilityScan

    scan = get_object_or_404(VulnerabilityScan, id=scan_id)
    if not scan.pdf_path:
        raise Http404('Report not generated yet.')
    abs_path = os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
    if not os.path.exists(abs_path):
        raise Http404('Report file missing on disk.')

    slug = owner_label(scan).replace(' ', '-')
    month = (scan.completed_at or scan.created_at).strftime('%Y-%m')
    ext = os.path.splitext(abs_path)[1] or '.pdf'
    filename = f'security-report-{slug}-{month}{ext}'
    return FileResponse(
        open(abs_path, 'rb'),
        as_attachment=True,
        filename=filename,
        content_type='application/pdf' if ext == '.pdf' else 'text/html',
    )


@admin_required
@require_POST
def scan_send_report(request, scan_id):
    """Email the scan PDF to the client, same send path v1 uses."""
    from reporting.models import VulnerabilityScan
    from reporting.scan_runner import generate_scan_pdf

    scan = get_object_or_404(
        VulnerabilityScan.objects.select_related(
            'website_new', 'website_new__account'), id=scan_id)
    website = scan.website_new
    if website is None or website.account is None:
        messages.error(request, 'This scan has no linked account to email.')
        return redirect('admin_dashboard:v2_scan_detail', scan_id=scan_id)

    account = website.account
    abs_path = (os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
                if scan.pdf_path else None)
    if not abs_path or not os.path.exists(abs_path):
        generate_scan_pdf(str(scan.id))
        scan.refresh_from_db()
        abs_path = (os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
                    if scan.pdf_path else None)
    if not abs_path or not os.path.exists(abs_path):
        messages.error(request, 'Could not generate PDF.')
        return redirect('admin_dashboard:v2_scan_detail', scan_id=scan_id)

    client_email = account.user.email if account.user else ''
    if not client_email:
        messages.error(request, 'No email address on file for this account.')
        return redirect('admin_dashboard:v2_scan_detail', scan_id=scan_id)

    month_str = (scan.completed_at or scan.created_at).strftime('%B %Y')
    if scan.critical_count or scan.high_count:
        severity_line = (
            f"{scan.critical_count} critical and {scan.high_count} "
            f"high severity issue(s) were identified that require "
            f"attention.")
    else:
        severity_line = (
            "No critical or high severity issues were detected. "
            "Your site is in good standing.")

    contact_name = account.contact_name or account.name
    first_name = (contact_name or '').split(' ')[0] or 'there'

    text_body = (
        f'Hi {first_name},\n\n'
        f'Please find attached your monthly security assessment report '
        f'for {month_str}.\n\n'
        f'{severity_line}\n\n'
        f'The full report is attached as a PDF. You can also log into '
        f'your portal to view your security history:\n'
        f'{settings.SITE_BASE_URL}/portal/security/\n\n'
        f'— Zachery Long\nAspired Websites LLC\n'
    )

    ext = os.path.splitext(abs_path)[1] or '.pdf'
    mime = 'application/pdf' if ext.lower() == '.pdf' else 'text/html'
    with open(abs_path, 'rb') as fh:
        pdf_bytes = fh.read()

    from clients.emails import send_branded
    try:
        send_branded(
            subject=f'Your Security Report — {month_str} — {account.name}',
            template='security_report',
            context={
                'name': first_name,
                'client_firm': account.name,
                'month_str': month_str,
                'critical_count': scan.critical_count,
                'high_count': scan.high_count,
                'security_url': f'{settings.SITE_BASE_URL}/portal/security/',
                'preheader': severity_line,
            },
            recipient_list=[client_email],
            text_body=text_body,
            from_email=getattr(settings, 'EMAIL_FROM_NO_REPLY',
                               settings.DEFAULT_FROM_EMAIL),
            attachments=[
                (f'security-report-{month_str}{ext}', pdf_bytes, mime)],
            fail_silently=False,
        )
    except Exception as exc:
        messages.error(request, f'Email send failed: {str(exc)[:200]}')
        return redirect('admin_dashboard:v2_scan_detail', scan_id=scan_id)

    scan.sent_to_client = True
    scan.sent_at = timezone.now()
    scan.save(update_fields=['sent_to_client', 'sent_at', 'updated_at'])
    messages.success(request, f'Report sent to {client_email}.')
    return redirect('admin_dashboard:v2_scan_detail', scan_id=scan_id)


@admin_required
@require_POST
def finding_status_update(request, finding_id):
    """Change a VulnerabilityFinding's status, then bounce back to the scan."""
    from reporting.models import VulnerabilityFinding

    finding = get_object_or_404(
        VulnerabilityFinding.objects.select_related('scan'), id=finding_id)
    new_status = (request.POST.get('status') or '').strip()
    valid = {choice for choice, _ in VulnerabilityFinding.STATUS_CHOICES}
    if new_status not in valid:
        return HttpResponseBadRequest('invalid status')

    finding.status = new_status
    if new_status == 'accepted_risk':
        finding.accepted_by = (
            request.user.get_full_name() or request.user.username)[:100]
        finding.accepted_at = timezone.now()
        finding.acceptance_note = (
            request.POST.get('acceptance_note') or '').strip()
    else:
        finding.accepted_by = ''
        finding.accepted_at = None
        finding.acceptance_note = ''
    finding.save(update_fields=[
        'status', 'accepted_by', 'accepted_at',
        'acceptance_note', 'updated_at',
    ])
    return redirect('admin_dashboard:v2_scan_detail', scan_id=finding.scan_id)
