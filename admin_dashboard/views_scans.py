"""
Vulnerability scan list and run UI.

Split out of admin_dashboard/views.py. `admin_dashboard.views`
re-exports these names, so urls.py — which references them as
`views.<name>` — keeps working unchanged.
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .decorators import admin_required
from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Phase 6c — vulnerability scans (Part 1 UI: list + run)
# ────────────────────────────────────────────────────────────────────────────

def _scan_row_border(scan):
    """Pick the left-border colour for one scans-table row."""
    if scan.status == 'failed':
        return 'red'
    if scan.status == 'running':
        return 'teal'
    if scan.status == 'pending':
        return 'muted'
    # complete
    if scan.critical_count:
        return 'red'
    if scan.high_count:
        return 'orange'
    return 'green'


def _build_scan_rows(scans):
    """Decorate VulnerabilityScan iterables with display extras."""
    rows = []
    for s in scans:
        duration = None
        if s.started_at and s.completed_at:
            duration = int(
                (s.completed_at - s.started_at).total_seconds())
        rows.append({
            'scan': s,
            'duration_seconds': duration,
            'duration_label': _format_duration(duration),
            'border': _scan_row_border(s),
        })
    return rows


def _format_duration(seconds):
    """Human-readable scan duration.

    < 60s     → "Ns"            (e.g. "14s")
    < 1 hour  → "Nm Ms"         (e.g. "1m 17s", "5m 0s" if exact)
    >= 1 hour → "Nh Mm"         (rare for scans; trims the seconds)
    None / 0  → ""

    Was previously assembled inline in the template with chained
    |slice and |divisibleby filters that produced output like
    "m 77s" for any 2-digit value. Doing the math in Python is the
    boring, correct fix.
    """
    if not seconds:
        return ''
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f'{m}m {s}s' if s else f'{m}m'
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f'{h}h {m}m' if m else f'{h}h'


@admin_required
def scans_list(request):
    """
    Full scan dashboard — filters, pagination, stats, HTMX auto-refresh
    when scans are pending or running, run-new-scan modal.
    """
    from clients.account_models import Website
    from reporting.models import VulnerabilityScan

    client_id = (request.GET.get('client') or '').strip()
    status = (request.GET.get('status') or '').strip()

    qs = (VulnerabilityScan.objects
          .select_related('website_new', 'website_new__account')
          .order_by('-created_at'))
    if client_id:
        qs = qs.filter(client_id=client_id)
    if status:
        qs = qs.filter(status=status)

    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get('page', 1))
    rows = _build_scan_rows(page.object_list)

    pending_count = VulnerabilityScan.objects.filter(
        status='pending').count()
    running_count = VulnerabilityScan.objects.filter(
        status='running').count()
    last_scan = VulnerabilityScan.objects.order_by(
        '-created_at').first()

    # A scan targets one droplet, so the picker lists sites.
    clients = (Website.objects
               .filter(status='active', account__status='active')
               .select_related('account')
               .order_by('account__name', 'name'))

    # Preserve filter querystring (sans `page`) so the HTMX partial
    # respects the filters across each poll.
    qs_params = request.GET.copy()
    qs_params.pop('page', None)
    filter_qs = qs_params.urlencode()

    return render(request, 'admin_dashboard/scans_list.html',
                  _admin_context(
                      'scans',
                      rows=rows,
                      page=page,
                      paginator=paginator,
                      total_scans=qs.count(),
                      pending_count=pending_count,
                      running_count=running_count,
                      last_scan=last_scan,
                      clients=clients,
                      selected_client=client_id,
                      selected_status=status,
                      status_choices=VulnerabilityScan.STATUS_CHOICES,
                      type_choices=VulnerabilityScan.SCAN_TYPE_CHOICES,
                      filter_qs=filter_qs,
                      auto_refresh=(pending_count + running_count) > 0,
                  ))


@admin_required
def scans_table(request):
    """HTMX partial — only the table rows, polled every 15s."""
    from reporting.models import VulnerabilityScan

    client_id = (request.GET.get('client') or '').strip()
    status = (request.GET.get('status') or '').strip()

    qs = (VulnerabilityScan.objects
          .select_related('client')
          .order_by('-created_at'))
    if client_id:
        qs = qs.filter(client_id=client_id)
    if status:
        qs = qs.filter(status=status)
    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get('page', 1))
    rows = _build_scan_rows(page.object_list)

    return render(request, 'admin_dashboard/_scan_rows.html',
                  {'rows': rows, 'page': page})


# ── scan detail helpers ─────────────────────────────────────────────────────

def _ssl_grade_class(grade):
    """CSS class for the SSL grade circle on the scan detail page."""
    if not grade:
        return None
    first = (grade or '').strip()[:1].upper()
    if first == 'A':
        return 'a'
    if first == 'B':
        return 'b'
    if first == 'C':
        return 'c'
    if first in ('D', 'E', 'F', 'T', 'M'):
        return 'f'
    return None


def _build_tool_blocks(scan):
    """
    Per-tool execution summary on the scan-detail page. `status` is one
    of 'ok' / 'skipped' / 'error' / 'idle'; `summary` is a short human
    one-liner ("3 findings", "Grade A", "Skipped — not WordPress", …).
    """
    blocks = []
    for tool, label, raw in (
            ('nmap', 'nmap', scan.raw_nmap),
            ('nikto', 'Nikto', scan.raw_nikto),
            ('ssl', 'SSL Labs', scan.raw_ssl),
            ('wpscan', 'WPScan', scan.raw_wpscan),
    ):
        raw = raw or {}
        if not raw:
            blocks.append({'tool': tool, 'label': label,
                           'status': 'idle', 'summary': 'not run'})
            continue
        if raw.get('skipped'):
            blocks.append({'tool': tool, 'label': label,
                           'status': 'skipped',
                           'summary': raw.get('reason') or 'skipped'})
            continue
        if raw.get('error'):
            blocks.append({'tool': tool, 'label': label,
                           'status': 'error',
                           'summary': str(raw.get('error'))[:120]})
            continue
        if tool == 'ssl':
            grade = raw.get('grade') or '—'
            blocks.append({'tool': tool, 'label': label,
                           'status': 'ok',
                           'summary': f'Grade {grade}'})
        else:
            n = len(raw.get('findings') or [])
            blocks.append({'tool': tool, 'label': label,
                           'status': 'ok',
                           'summary': (
                               f'{n} finding{"" if n == 1 else "s"}')})
    return blocks


@admin_required
def scan_detail(request, scan_id):
    """
    Scan detail — severity-grid header, SSL grade circle if present,
    findings grouped by severity, per-tool execution summary.
    """
    from reporting.models import VulnerabilityScan

    scan = get_object_or_404(
        VulnerabilityScan.objects.select_related('client'),
        id=scan_id,
    )

    # Mark the scan reviewed on first open. Drives the Today's Focus
    # widget — unreviewed scans with criticals stay on the list;
    # once an admin opens this page the item falls off so the home
    # dashboard stays focused on "unseen" work. Idempotent.
    if not scan.been_reviewed:
        scan.been_reviewed = True
        scan.reviewed_at = timezone.now()
        scan.save(update_fields=[
            'been_reviewed', 'reviewed_at', 'updated_at'])

    findings = list(scan.findings.order_by('severity', 'tool', 'title'))

    by_sev = {sev: [] for sev in
              ('critical', 'high', 'medium', 'low', 'info')}
    for f in findings:
        by_sev.setdefault(f.severity, []).append(f)

    # List form (template-friendly — Django templates can't index a
    # dict by variable key without a custom tag).
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
        duration = int(
            (scan.completed_at - scan.started_at).total_seconds())

    ssl_grade = (scan.raw_ssl or {}).get('grade')
    open_count = sum(1 for f in findings if f.status == 'open')

    return render(request, 'admin_dashboard/scan_detail.html',
                  _admin_context(
                      'scans',
                      scan=scan,
                      severity_groups=severity_groups,
                      findings_total=len(findings),
                      open_count=open_count,
                      duration_seconds=duration,
                      tool_blocks=_build_tool_blocks(scan),
                      ssl_grade=ssl_grade,
                      ssl_grade_class=_ssl_grade_class(ssl_grade),
                  ))


@admin_required
def generate_scan_pdf_view(request, scan_id):
    """
    Generate the scan PDF and stream it back as an attachment so the
    button click → file save dialog is a single user action.

    Accepts both GET (plain <a> click — browser downloads the PDF)
    and POST (HTMX Regenerate button — returns a banner with a
    Download link). Idempotent side effect (renders to the same
    on-disk path), admin-only, so dropping @require_POST is safe.

    The old version returned a tiny HTML banner with a "Download
    report →" link inside it; the user clicked the button, the banner
    quietly swapped in below the buttons, and they reasonably
    concluded "Generate PDF doesn't send a PDF". Now we just serve
    the file.

    The legacy banner-via-HTMX flow stays available on the
    "Regenerate" button (POST → JSON-style banner) for cases where
    the admin only wants to refresh the on-disk file without
    triggering a download — but the primary path is download.
    """
    import os

    from django.http import FileResponse
    from reporting.models import VulnerabilityScan
    from reporting.scan_runner import generate_scan_pdf

    scan = get_object_or_404(
        VulnerabilityScan.objects.select_related('client'), id=scan_id)

    # Always (re)generate — guarantees the served file matches the
    # current scan data, not a stale render from a previous click.
    pdf_path = generate_scan_pdf(str(scan.id))
    if not pdf_path:
        # POST + HTMX path returns a banner; GET path returns plain
        # error HTML. Detect by the swap header htmx sets.
        if request.headers.get('HX-Request') == 'true':
            return HttpResponse(
                '<div class="scan-banner scan-banner--error">'
                'PDF generation failed — check server logs.'
                '</div>', status=500)
        return HttpResponse(
            'PDF generation failed — check server logs.',
            status=500, content_type='text/plain')

    scan.refresh_from_db()
    abs_path = os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
    ext = os.path.splitext(abs_path)[1] or '.pdf'

    # "Regenerate" button is still an HTMX POST and expects a banner
    # response. Honor that — it keeps the existing JS contract.
    if request.headers.get('HX-Request') == 'true':
        download_url = reverse(
            'admin_dashboard:scan_download_pdf', args=[scan.id])
        return HttpResponse(
            f'<div class="scan-banner scan-banner--info">'
            f'PDF re-generated. '
            f'<a href="{download_url}" class="btn-primary btn-sm" '
            f'style="margin-left:.5rem;">Download report &rarr;</a>'
            f'</div>')

    # Primary "Generate PDF Report" path — serve the file directly.
    slug = scan.client.firm_name.replace(' ', '-')
    month = (scan.completed_at or scan.created_at).strftime('%Y-%m')
    filename = f'security-report-{slug}-{month}{ext}'
    return FileResponse(
        open(abs_path, 'rb'),
        as_attachment=True,
        filename=filename,
        content_type=('application/pdf'
                      if ext == '.pdf' else 'text/html'),
    )


@admin_required
def download_scan_pdf(request, scan_id):
    """
    Serve the rendered scan PDF (or HTML fallback) as an attachment.
    `pdf_path` on the scan is RELATIVE to MEDIA_ROOT.
    """
    import os

    from django.http import FileResponse, Http404
    from reporting.models import VulnerabilityScan

    scan = get_object_or_404(VulnerabilityScan, id=scan_id)
    if not scan.pdf_path:
        raise Http404('Report not generated yet.')
    abs_path = os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
    if not os.path.exists(abs_path):
        raise Http404('Report file missing on disk.')

    slug = scan.client.firm_name.replace(' ', '-')
    month = (scan.completed_at or scan.created_at).strftime('%Y-%m')
    ext = os.path.splitext(abs_path)[1] or '.pdf'
    filename = f'security-report-{slug}-{month}{ext}'

    return FileResponse(
        open(abs_path, 'rb'),
        as_attachment=True,
        filename=filename,
        content_type=('application/pdf'
                      if ext == '.pdf' else 'text/html'),
    )


@admin_required
@require_POST
def send_scan_report(request, scan_id):
    """
    Email the scan PDF to the client via SendGrid. Generates the PDF
    first if it isn't on disk yet. Updates `sent_to_client` + `sent_at`
    on the scan record so the button can flip to "Resend Report".
    Returns an HTMX-friendly HTML banner.
    """
    import base64
    import os

    from reporting.models import VulnerabilityScan
    from reporting.scan_runner import generate_scan_pdf

    scan = get_object_or_404(
        VulnerabilityScan.objects.select_related('client'), id=scan_id)
    client = scan.client

    def _banner(kind, msg, status=200):
        return HttpResponse(
            f'<div class="scan-banner scan-banner--{kind}">{msg}</div>',
            status=status)

    # Make sure the PDF exists.
    abs_path = (os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
                if scan.pdf_path else None)
    if not abs_path or not os.path.exists(abs_path):
        generate_scan_pdf(str(scan.id))
        scan.refresh_from_db()
        abs_path = (os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
                    if scan.pdf_path else None)
    if not abs_path or not os.path.exists(abs_path):
        return _banner('error', 'Could not generate PDF.', status=500)

    client_email = client.user.email if client.user else ''
    if not client_email:
        return _banner(
            'error', 'No email address on file for this client.',
            status=400)

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

    contact_name = client.contact_name or client.firm_name
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
            subject=(f'Your Security Report — {month_str} — '
                     f'{client.firm_name}'),
            template='security_report',
            context={
                'name': first_name,
                'client_firm': client.firm_name,
                'month_str': month_str,
                'critical_count': scan.critical_count,
                'high_count': scan.high_count,
                'security_url': (
                    f'{settings.SITE_BASE_URL}/portal/security/'),
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
    except Exception as exc:  # noqa: BLE001 — surface to operator
        return _banner(
            'error', f'Email send failed: {str(exc)[:200]}', status=500)

    scan.sent_to_client = True
    scan.sent_at = timezone.now()
    scan.save(update_fields=[
        'sent_to_client', 'sent_at', 'updated_at'])

    return _banner('info', f'Report sent to {client_email}.')


@admin_required
@require_POST
def toggle_auto_send_scans(request, client_id):
    """HTMX toggle — flip Website.auto_send_scan_reports."""
    from clients.account_models import Website
    client = get_object_or_404(
        Website.objects.select_related('account'), id=client_id)
    client.auto_send_scan_reports = not client.auto_send_scan_reports
    client.save(update_fields=['auto_send_scan_reports', 'updated_at'])
    return render(request,
                  'admin_dashboard/_auto_send_scans_toggle.html',
                  {'client': client})


@admin_required
@require_POST
def update_finding_status(request, finding_id):
    """
    HTMX POST: change a VulnerabilityFinding's status.

    Body:
      status          — open | accepted_risk | false_positive | resolved
      acceptance_note — required text when status == accepted_risk

    Returns the refreshed finding card HTML so HTMX swaps it in place.
    """
    from reporting.models import VulnerabilityFinding

    finding = get_object_or_404(VulnerabilityFinding, id=finding_id)
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
        # Moving away from accepted_risk — wipe the acceptance metadata
        # so the audit trail doesn't show stale acceptance details.
        finding.accepted_by = ''
        finding.accepted_at = None
        finding.acceptance_note = ''
    finding.save(update_fields=[
        'status', 'accepted_by', 'accepted_at',
        'acceptance_note', 'updated_at',
    ])

    return render(request, 'admin_dashboard/_finding_card.html',
                  {'f': finding, 'expanded': True})


@admin_required
@require_POST
def run_scan(request):
    """
    Trigger a scan from the admin client detail page. Body:
      client_id (required), scan_type (default 'full').
    Returns an HTMX fragment for inline status, or redirects if not HTMX.
    """
    from clients.account_models import Website
    from reporting.models import VulnerabilityScan
    from reporting.tasks import run_vulnerability_scan_task

    client_id = (request.POST.get('client_id') or '').strip()
    scan_type = (request.POST.get('scan_type') or 'full').strip()
    if scan_type not in dict(VulnerabilityScan.SCAN_TYPE_CHOICES):
        scan_type = 'full'

    # A scan targets one site's URL and its droplet's IP, both of which
    # are the Website's.
    client = get_object_or_404(
        Website.objects.select_related('account'), id=client_id)
    target_url = client.url or ''
    target_ip = client.do_droplet_ip or ''

    if not (target_url or target_ip):
        return HttpResponseBadRequest(
            'Site has no live URL or Droplet IP to scan.')

    scan = VulnerabilityScan.objects.create(
        website_new=client,
        target_url=target_url,
        target_ip=target_ip,
        scan_type=scan_type,
        is_scheduled=False,
    )
    async_result = run_vulnerability_scan_task.delay(str(scan.id))
    scan.celery_task_id = async_result.id or ''
    scan.save(update_fields=['celery_task_id', 'updated_at'])

    if request.headers.get('HX-Request') == 'true':
        return HttpResponse(
            f'<div class="scan-banner scan-banner--info">'
            f'Scan started — check '
            f'<a href="{reverse("admin_dashboard:scans_list")}">Scans</a> '
            f'for results.</div>')
    return redirect('admin_dashboard:scans_list')


@admin_required
@require_POST
def scan_cancel(request, scan_id):
    """
    Stop a stuck/long-running scan from the scan detail page.

    Two-part teardown:
      1. Revoke the Celery task (terminate=True kills the worker
         process running it — necessary because the scan subprocess
         calls nmap/Nikto which can hang indefinitely on network
         issues)
      2. Mark the scan row as 'cancelled' so the UI reflects it
         immediately + the daily auto-scan cron won't see it as
         'last completed' and reset its scheduling window

    Safe to call on a scan whose Celery task is already gone (worker
    restart, etc.) — revoke is best-effort, the DB update always runs.
    Only acts on scans in 'pending' or 'running' status.
    """
    from django.contrib import messages
    from django.utils import timezone
    from reporting.models import VulnerabilityScan

    scan = get_object_or_404(VulnerabilityScan, id=scan_id)
    if scan.status not in ('pending', 'running'):
        messages.info(
            request,
            f'This scan is already {scan.get_status_display().lower()} — '
            f'nothing to cancel.')
        return redirect('admin_dashboard:scan_detail', scan_id=scan_id)

    if scan.celery_task_id:
        try:
            from AspiredWebsitesRevamped.celery import app as celery_app
            celery_app.control.revoke(
                scan.celery_task_id, terminate=True, signal='SIGTERM')
            logger.info(
                'scan_cancel: revoked celery task %s for scan %s',
                scan.celery_task_id, scan.id)
        except Exception:
            logger.exception(
                'scan_cancel: revoke failed for task %s — proceeding '
                'with DB-only cancellation',
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
    return redirect('admin_dashboard:scan_detail', scan_id=scan_id)


