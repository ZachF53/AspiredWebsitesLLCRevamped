"""
Annual business health report admin.

Split out of admin_dashboard/views.py; re-exported from
`admin_dashboard.views` so urls.py keeps working unchanged.
"""

import datetime
import logging

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)
from .decorators import admin_required

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 4 — Annual Business Health Report
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def annual_reports_list(request):
    """All annual reports in one table — newest year first per client."""
    from clients.models import AnnualReport
    reports = (
        AnnualReport.objects
        .select_related('client')
        .order_by('-report_year', 'client__firm_name')
    )
    return render(request, 'admin_dashboard/annual_reports_list.html',
                  _admin_context(
                      'annual_reports',
                      reports=reports,
                  ))


@admin_required
def annual_report_detail(request, report_id):
    """Single-report detail + action buttons."""
    from clients.models import AnnualReport
    report = get_object_or_404(AnnualReport, id=report_id)
    return render(request, 'admin_dashboard/annual_report_detail.html',
                  _admin_context(
                      'annual_reports',
                      report=report,
                      data=report.report_data or {},
                  ))


@admin_required
@require_POST
def annual_report_send(request, report_id):
    """Email the PDF to the client via SendGrid."""
    import base64
    import os
    from pathlib import Path

    from clients.models import AnnualReport

    report = get_object_or_404(AnnualReport, id=report_id)
    client = report.client
    if report.status not in ('ready', 'sent'):
        return HttpResponseBadRequest(
            'Report must be in status ready or sent to send.')
    to_email = client.user.email if client.user else ''
    if not to_email:
        return HttpResponseBadRequest(
            'Client has no email on file — cannot send.')
    if not report.pdf_path:
        return HttpResponseBadRequest(
            'Report has no PDF — regenerate first.')

    abs_path = Path(settings.MEDIA_ROOT) / report.pdf_path
    if not abs_path.exists():
        return HttpResponseBadRequest(
            'PDF file is missing — regenerate first.')

    contact_name = (client.contact_name
                    or (client.user.get_full_name() if client.user else '')
                    or 'there')

    first_name = (contact_name or '').split(' ')[0] or 'there'

    text_body = (
        f"Hi {first_name},\n\n"
        f"Your {report.report_year} Annual Business Health Report "
        f"is attached. It covers a full year of website performance, "
        f"security work, and growth.\n\n"
        f"I'd love to schedule a quick call to walk through it "
        f"together. Reply with a couple of times that work for you.\n\n"
        f"— Zachery Long\nAspired Websites LLC\n"
    )

    ext = os.path.splitext(abs_path)[1].lower() or '.pdf'
    mime = 'application/pdf' if ext == '.pdf' else 'text/html'
    with open(abs_path, 'rb') as fh:
        pdf_bytes = fh.read()

    subject = (f'Your {report.report_year} Annual Website '
               f'Performance Report — {client.firm_name}')
    from clients.emails import send_branded
    try:
        send_branded(
            subject=subject,
            template='annual_report',
            context={
                'name': first_name,
                'client_firm': client.firm_name,
                'report_year': report.report_year,
                'preheader': (
                    f'A full year of performance, security, and growth.'),
            },
            recipient_list=[to_email],
            text_body=text_body,
            from_email=getattr(settings, 'EMAIL_FROM_MAIN',
                               settings.DEFAULT_FROM_EMAIL),
            attachments=[
                (f'annual-report-{report.report_year}{ext}',
                 pdf_bytes, mime)],
            fail_silently=False,
        )
    except Exception as exc:  # noqa: BLE001
        return HttpResponse(f'Email send failed: {str(exc)[:200]}',
                            status=500)

    report.status = 'sent'
    report.sent_at = timezone.now()
    report.save(update_fields=['status', 'sent_at', 'updated_at'])

    return redirect('admin_dashboard:annual_report_detail',
                    report_id=report.id)


@admin_required
@require_POST
def annual_report_regenerate(request, report_id):
    """
    Force a fresh generation pass — flips the row back to
    `generating` (so the idempotency guard in the task doesn't
    short-circuit) and queues the Celery task.
    """
    from clients.models import AnnualReport
    from clients.tasks import generate_annual_report

    report = get_object_or_404(AnnualReport, id=report_id)
    report.status = 'generating'
    report.save(update_fields=['status', 'updated_at'])
    generate_annual_report.apply_async(
        args=[str(report.website_new_id), report.report_year])
    return redirect('admin_dashboard:annual_report_detail',
                    report_id=report.id)


@admin_required
def annual_report_generate(request):
    """
    Manual on-demand generation — admin picks a client + year,
    we queue the Celery task and bounce to the detail page.
    """
    from clients.account_models import Website
    from clients.models import AnnualReport
    from clients.tasks import generate_annual_report

    if request.method == 'POST':
        cid = (request.POST.get('client_id') or '').strip()
        year_raw = (request.POST.get('report_year') or '').strip()
        if not cid:
            return HttpResponseBadRequest('client_id required.')
        try:
            year = int(year_raw)
        except ValueError:
            return HttpResponseBadRequest(
                'report_year must be an integer.')
        # The report covers one site's year. Keyed on the website,
        # matching the unique constraint.
        site = get_object_or_404(
            Website.objects.select_related('account'), id=cid)
        report, _ = AnnualReport.objects.get_or_create(
            website_new=site, report_year=year,
            defaults={'status': 'generating'},
        )
        report.status = 'generating'
        report.save(update_fields=['status', 'updated_at'])
        generate_annual_report.apply_async(
            args=[str(site.id), year])
        return redirect(
            'admin_dashboard:annual_report_detail',
            report_id=report.id)

    clients = (Website.objects.filter(account__is_tester=False)
               .select_related('account')
               .order_by('account__name', 'name'))
    return render(request, 'admin_dashboard/annual_report_generate.html',
                  _admin_context(
                      'annual_reports',
                      clients=clients,
                      default_year=(timezone.now().year - 1),
                  ))


@admin_required
def annual_report_download(request, report_id):
    """Serve the PDF (or .html fallback) inline."""
    from pathlib import Path

    from django.http import FileResponse, Http404

    from clients.models import AnnualReport

    report = get_object_or_404(AnnualReport, id=report_id)
    if not report.pdf_path:
        raise Http404('Report has no PDF yet.')
    abs_path = Path(settings.MEDIA_ROOT) / report.pdf_path
    if not abs_path.exists():
        raise Http404('PDF file missing.')
    content_type = ('application/pdf' if abs_path.suffix.lower() == '.pdf'
                    else 'text/html')
    return FileResponse(open(abs_path, 'rb'),
                        content_type=content_type)


