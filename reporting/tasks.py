"""
Reporting Celery tasks — uptime monitoring, GBP NAP sync, conversion-drop
alerts, and weekly keyword rank checks.

External integrations (Google Business Profile, Google Search Console) are
not yet connected — those tasks degrade gracefully and log/record the gap
rather than failing.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Admin notification ──────────────────────────────────────────────────────

def send_admin_alert(subject, message):
    """Email an operational alert to the admin notification address."""
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.EMAIL_FROM_NO_REPLY,
            recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
            fail_silently=True,
        )
    except Exception:
        logger.exception('send_admin_alert failed: %s', subject)


# ── Part 1: Uptime monitoring ───────────────────────────────────────────────

@shared_task
def check_client_uptime():
    """Ping every active, launched client site. Scheduled every 5 minutes."""
    import requests
    from clients.models import ClientProfile, UptimeRecord, UptimeAlert

    # do_droplet_ip is a GenericIPAddressField — when blank it is stored as
    # NULL (never ''), so isnull=False alone selects clients with a server.
    active_clients = ClientProfile.objects.filter(
        status='active', do_droplet_ip__isnull=False,
    )

    checked = 0
    for client in active_clients:
        project = client.projects.filter(stage='live').first()
        if not project or not project.live_url:
            continue

        url = project.live_url
        if not url.startswith('http'):
            url = f'https://{url}'

        try:
            start = timezone.now()
            response = requests.get(
                url, timeout=15, allow_redirects=True,
                headers={'User-Agent': 'AspiredWebsites-Monitor/1.0'},
            )
            response_time = int(
                (timezone.now() - start).total_seconds() * 1000)
            is_up = response.status_code < 500

            UptimeRecord.objects.create(
                client=client,
                response_time_ms=response_time,
                status_code=response.status_code,
                is_up=is_up,
                error_message='' if is_up else f'HTTP {response.status_code}',
            )

            if is_up:
                UptimeAlert.objects.filter(
                    client=client, is_resolved=False,
                ).update(is_resolved=True, resolved_at=timezone.now())
            else:
                check_and_fire_alert(client)

        except requests.RequestException as exc:
            UptimeRecord.objects.create(
                client=client,
                response_time_ms=None,
                status_code=None,
                is_up=False,
                error_message=str(exc)[:200],
            )
            check_and_fire_alert(client)
        checked += 1

    return f'Checked {checked} client site(s).'


def check_and_fire_alert(client):
    """
    Open a downtime alert after 3 consecutive failed checks — once per
    outage, so a long outage does not spam the admin on every check.
    """
    from clients.models import UptimeRecord, UptimeAlert

    recent = list(
        UptimeRecord.objects.filter(client=client).order_by('-checked_at')[:3]
    )
    if len(recent) < 3 or not all(not r.is_up for r in recent):
        return

    if UptimeAlert.objects.filter(client=client, is_resolved=False).exists():
        return  # an alert is already open for this outage

    UptimeAlert.objects.create(
        client=client, consecutive_failures=3, alert_sent=True)

    project = client.projects.filter(stage='live').first()
    live_url = project.live_url if project else '(unknown)'
    send_admin_alert(
        subject=f'🔴 Site Down: {client.firm_name}',
        message=(
            f'{client.firm_name} has been down for 3 consecutive checks.\n'
            f'Domain: {live_url}\n'
            f'Check: /admin-dashboard/clients/{client.id}/uptime/'
        ),
    )


# ── Part 2: GBP NAP sync ────────────────────────────────────────────────────

GBP_NOT_CONNECTED = 'GBP not connected'


def _gbp_is_connected(client):
    """
    True when this client has a usable Google Business Profile connection.

    Phase 4 social OAuth is not built yet, so this is always False today —
    check_gbp_sync records an informational row rather than a real compare.
    """
    return False


@shared_task
def check_gbp_sync():
    """
    Check NAP (name/phone/address/website) consistency between each client's
    site and their Google Business Profile. Scheduled weekly (Mon 9am).

    GBP OAuth is a Phase 4 dependency — until it lands this records a single
    informational GBPSyncCheck per client instead of a real comparison.
    """
    from clients.models import ClientProfile
    from .models import GBPSyncCheck

    clients = ClientProfile.objects.filter(status='active')
    recorded = 0
    for client in clients:
        project = client.projects.filter(stage='live').first()
        if not project:
            continue

        if not _gbp_is_connected(client):
            GBPSyncCheck.objects.create(
                client=client,
                field_name='website',
                website_value=GBP_NOT_CONNECTED,
                gbp_value='Connect GBP in Phase 4 setup',
                is_mismatch=False,
            )
            recorded += 1
            continue

        # When GBP OAuth lands: scrape NAP fields from project.live_url,
        # fetch the same fields from the GBP API, and record a GBPSyncCheck
        # per field — flagging is_mismatch and notifying the admin on a diff.

    return f'Recorded GBP sync status for {recorded} client(s).'


# ── Part 3: Keyword rank tracking ───────────────────────────────────────────

@shared_task
def check_keyword_ranks():
    """
    Refresh ranking positions for every active tracked keyword. Scheduled
    weekly (Mon 7am).

    Live positions come from the Google Search Console API, which requires
    per-client OAuth (Phase 4). Until that is connected this is a safe
    no-op — staff can enter KeywordRankRecord rows manually via the admin.
    """
    from .models import TrackedKeyword

    active = TrackedKeyword.objects.filter(is_active=True).count()
    logger.info(
        'check_keyword_ranks: %s active keyword(s); GSC API not connected — '
        'no automatic ranks fetched.', active,
    )
    return f'{active} active keyword(s); GSC not connected.'


# ── Part 4: Conversion-drop alerts ──────────────────────────────────────────

@shared_task
def check_conversion_drops():
    """
    Compare this month's form submissions to last month's per client.
    A drop of 30%+ raises an admin alert. Scheduled on the 2nd at 8am.
    """
    from clients.models import ClientProfile
    from .models import ConversionEvent

    now = timezone.now()
    this_month_start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_start = (this_month_start - timedelta(days=1)).replace(day=1)

    alerted = 0
    for client in ClientProfile.objects.filter(status='active'):
        this_month = ConversionEvent.objects.filter(
            client=client, event_type='form_submit',
            event_timestamp__gte=this_month_start,
        ).count()
        last_month = ConversionEvent.objects.filter(
            client=client, event_type='form_submit',
            event_timestamp__gte=last_month_start,
            event_timestamp__lt=this_month_start,
        ).count()

        if last_month == 0 or this_month == 0:
            continue

        drop_pct = ((last_month - this_month) / last_month) * 100
        if drop_pct >= 30:
            send_admin_alert(
                subject=f'⚠ Conversion Drop: {client.firm_name}',
                message=(
                    f'Form submissions dropped {drop_pct:.0f}% this month.\n'
                    f'Last month: {last_month}\n'
                    f'This month: {this_month}\n'
                    f'Check: /admin-dashboard/clients/{client.id}/conversions/'
                ),
            )
            alerted += 1

    return f'Conversion-drop check complete — {alerted} alert(s) sent.'


# ── Phase 5b Part 1: monthly PDF reports ────────────────────────────────────

def _month_end(month_start):
    """First day of the month after `month_start`."""
    if month_start.month == 12:
        return month_start.replace(year=month_start.year + 1, month=1)
    return month_start.replace(month=month_start.month + 1)


def _report_summary(report_month, uptime_pct, forms, phones, improved):
    """A plain-English summary paragraph for the report (static template)."""
    month = report_month.strftime('%B')
    parts = []
    if uptime_pct is not None:
        parts.append(f'Your site was online {uptime_pct}% of {month}.')
    parts.append(
        f'Visitors submitted {forms} contact form{"" if forms == 1 else "s"} '
        f'and clicked your phone number {phones} '
        f'time{"" if phones == 1 else "s"}.')
    if improved:
        parts.append(
            f'{improved} keyword{"" if improved == 1 else "s"} moved up in '
            f'Google rankings this month.')
    return ' '.join(parts)


@shared_task
def generate_monthly_report(client_id, report_month_str):
    """Generate and send one client's monthly PDF report. report_month_str: YYYY-MM-01."""
    import os
    from datetime import date

    from django.template.loader import render_to_string

    from clients.models import ClientProfile, SiteChangelogEntry

    from .conversion_helpers import conversion_counts
    from .keyword_helpers import build_keyword_rows
    from .models import ConversionEvent, MonthlyReport
    from .uptime_helpers import (
        get_avg_response_time, get_uptime_chart_data, get_uptime_percentage,
    )

    client = ClientProfile.objects.filter(id=client_id).first()
    if client is None:
        return 'No such client.'
    report_month = date.fromisoformat(report_month_str).replace(day=1)

    report, created = MonthlyReport.objects.get_or_create(
        client=client, report_month=report_month,
        defaults={'status': 'generating'})
    if not created and report.status == 'sent':
        return 'Already sent — skipped.'

    month_start = report_month
    month_end = _month_end(month_start)

    uptime_pct = get_uptime_percentage(client, days=30)
    avg_ms = get_avg_response_time(client, days=30)

    def _count(event_type):
        return ConversionEvent.objects.filter(
            client=client, event_type=event_type,
            event_timestamp__date__gte=month_start,
            event_timestamp__date__lt=month_end).count()

    form_subs, phone_clicks, cta_clicks = (
        _count('form_submit'), _count('phone_click'), _count('cta_click'))

    # Form submissions bucketed by week of the month for the PDF bar chart.
    from datetime import timedelta as _td
    weekly_forms = []
    week_start = month_start
    week_no = 1
    while week_start < month_end:
        week_end = min(week_start + _td(days=7), month_end)
        weekly_forms.append({
            'label': f'Wk {week_no}',
            'count': ConversionEvent.objects.filter(
                client=client, event_type='form_submit',
                event_timestamp__date__gte=week_start,
                event_timestamp__date__lt=week_end).count(),
        })
        week_start, week_no = week_end, week_no + 1
    peak_week = max((w['count'] for w in weekly_forms), default=0) or 1
    for week in weekly_forms:
        week['bar_h'] = round(week['count'] / peak_week * 100)

    changelog = list(SiteChangelogEntry.objects.filter(
        client=client, date_of_change__gte=month_start,
        date_of_change__lt=month_end, is_client_visible=True))

    keyword_rows = build_keyword_rows(client, active_only=True)
    page1 = sum(1 for r in keyword_rows if r['position'] and r['position'] <= 10)
    improved = sum(1 for r in keyword_rows if r['trend']['css'] == 'up')

    uptime_chart = get_uptime_chart_data(client, days=30)
    peak = max((d['avg_response_ms'] or 0 for d in uptime_chart), default=0) or 1
    for day in uptime_chart:
        day['bar_h'] = round((day['avg_response_ms'] or 0) / peak * 100)

    context = {
        'client': client,
        'report_month': report_month,
        'next_month': month_end,
        'uptime_pct': uptime_pct,
        'avg_response_ms': avg_ms,
        'form_submissions': form_subs,
        'phone_clicks': phone_clicks,
        'cta_clicks': cta_clicks,
        'changelog_entries': changelog,
        'keyword_rows': keyword_rows[:10],
        'keywords_on_page_1': page1,
        'keywords_improved': improved,
        'conversion_counts': conversion_counts(client),
        'weekly_forms': weekly_forms,
        'uptime_chart': uptime_chart,
        'summary': _report_summary(
            report_month, uptime_pct, form_subs, phone_clicks, improved),
        'generated_at': timezone.now(),
    }
    html_string = render_to_string('reporting/monthly_report.html', context)

    rel_dir = os.path.join('reports', str(client.id))
    abs_dir = os.path.join(settings.MEDIA_ROOT, rel_dir)
    os.makedirs(abs_dir, exist_ok=True)
    filename = f'report-{report_month.strftime("%Y-%m")}.pdf'
    abs_path = os.path.join(abs_dir, filename)

    try:
        from weasyprint import HTML
        HTML(string=html_string).write_pdf(abs_path)
        report.pdf_path = os.path.join(rel_dir, filename).replace('\\', '/')
    except Exception:
        # WeasyPrint needs native GTK libs (present on the Ubuntu server, not
        # on Windows dev) — fall back to an .html file so the report persists.
        logger.exception('WeasyPrint failed — writing report HTML fallback')
        html_name = filename[:-4] + '.html'
        with open(os.path.join(abs_dir, html_name), 'w', encoding='utf-8') as fh:
            fh.write(html_string)
        report.pdf_path = os.path.join(rel_dir, html_name).replace('\\', '/')

    report.uptime_30d = uptime_pct
    report.avg_response_ms = avg_ms
    report.form_submissions = form_subs
    report.phone_clicks = phone_clicks
    report.keywords_on_page_1 = page1
    report.keywords_improved = improved
    report.status = 'ready'
    report.save()

    send_monthly_report_email(report)
    return f'Report generated for {client.firm_name}.'


def send_monthly_report_email(report):
    """Email the report file to the client via the SendGrid SMTP backend."""
    import os

    from django.core.mail import EmailMessage

    client = report.client
    month_str = report.report_month.strftime('%B %Y')
    recipient = getattr(client.user, 'email', '') if client.user_id else ''
    if not recipient:
        report.status = 'failed'
        report.save(update_fields=['status', 'updated_at'])
        return

    name = client.contact_name or client.firm_name
    uptime = report.uptime_30d if report.uptime_30d is not None else 'N/A'
    html = (
        f'<p>Hi {name},</p>'
        f'<p>Your monthly performance report for {month_str} is attached.</p>'
        f'<p>Your site was online {uptime}% of {month_str}. Log into your '
        f'portal anytime to see your full activity history:</p>'
        f'<p><a href="https://aspiredwebsites.com/portal/reports/">'
        f'View Your Portal</a></p>'
        f'<p>— Zachery Long<br>Aspired Websites LLC<br>210-896-2536</p>'
    )
    email = EmailMessage(
        subject=f'Your Monthly Report — {month_str} — {client.firm_name}',
        body=html,
        from_email=settings.EMAIL_FROM_NO_REPLY,
        to=[recipient],
    )
    email.content_subtype = 'html'

    abs_path = os.path.join(settings.MEDIA_ROOT, report.pdf_path or '')
    if report.pdf_path and os.path.exists(abs_path):
        mime = 'application/pdf' if abs_path.endswith('.pdf') else 'text/html'
        with open(abs_path, 'rb') as fh:
            email.attach(os.path.basename(abs_path), fh.read(), mime)

    try:
        email.send(fail_silently=False)
        report.status = 'sent'
        report.sent_at = timezone.now()
    except Exception:
        logger.exception('Monthly report email failed for %s', client.pk)
        report.status = 'failed'
    report.save()


@shared_task
def send_monthly_reports():
    """Generate + send last month's report for every active maintenance client."""
    from datetime import date

    from clients.models import ClientProfile

    today = timezone.localdate()
    if today.month == 1:
        report_month = date(today.year - 1, 12, 1)
    else:
        report_month = date(today.year, today.month - 1, 1)

    count = 0
    for client in ClientProfile.objects.filter(
            status='active', maintenance_active=True):
        generate_monthly_report(str(client.id), report_month.isoformat())
        count += 1
    return f'Processed {count} monthly report(s) for {report_month}.'


# ── Phase 5b Part 2: content freshness ──────────────────────────────────────

@shared_task
def generate_freshness_report(client_id):
    """Crawl a client's live site and score every page for content freshness."""
    from clients.models import ClientProfile

    from .freshness import calculate_freshness_score, crawl_site
    from .models import ContentFreshnessReport

    client = ClientProfile.objects.filter(id=client_id).first()
    if client is None:
        return 'No such client.'
    project = client.projects.filter(stage='live').first()
    if not project or not project.live_url:
        return 'No live site to crawl.'

    base_url = project.live_url
    if not base_url.startswith('http'):
        base_url = f'https://{base_url}'

    pages = crawl_site(base_url, max_pages=50)
    report_data = []
    for page in pages:
        score = calculate_freshness_score(page)
        last_mod = page.get('last_modified')
        report_data.append({
            'url': page['url'],
            'title': page['title'],
            'last_modified': last_mod.isoformat() if last_mod else None,
            'word_count_estimate': page.get('word_count'),
            'freshness_score': score,
            'priority': ('high' if score < 50
                         else 'medium' if score < 70 else 'good'),
        })
    report_data.sort(key=lambda item: item['freshness_score'])

    ContentFreshnessReport.objects.create(
        client=client,
        pages_analyzed=len(pages),
        pages_needing_update=sum(
            1 for p in report_data if p['priority'] == 'high'),
        report_data=report_data,
    )
    return f'Freshness report for {client.firm_name}: {len(pages)} page(s).'


@shared_task
def generate_freshness_reports():
    """Quarterly freshness crawl for every active maintenance client."""
    from clients.models import ClientProfile
    count = 0
    for client in ClientProfile.objects.filter(
            status='active', maintenance_active=True):
        generate_freshness_report(str(client.id))
        count += 1
    return f'Freshness reports generated for {count} client(s).'


# ── Phase 5b Part 3: NPS surveys ────────────────────────────────────────────

def send_nps_email(client, survey):
    """Send the NPS survey email with 0-10 scoring links."""
    from django.core.mail import send_mail

    recipient = getattr(client.user, 'email', '') if client.user_id else ''
    if not recipient:
        return
    name = client.contact_name or client.firm_name
    base = f'{settings.SITE_BASE_URL}/nps/{survey.survey_token}/'

    text_lines = [
        f'Hi {name},', '',
        'A quick question — on a scale of 0 to 10, how likely are you to '
        'recommend Aspired Websites to a friend or colleague?', '',
    ]
    text_lines += [f'  {n}: {base}{n}/' for n in range(11)]
    text_lines += ['', 'Just click the number that fits. Thank you!', '',
                   '— Zachery Long', 'Aspired Websites LLC']

    buttons = ''.join(
        f'<a href="{base}{n}/" style="display:inline-block;width:34px;'
        f'height:34px;line-height:34px;margin:3px;text-align:center;'
        f'border-radius:6px;background:#E8650A;color:#ffffff;'
        f'text-decoration:none;font-weight:bold;">{n}</a>'
        for n in range(11)
    )
    html = (
        f'<p>Hi {name},</p>'
        f'<p>A quick question — on a scale of 0 to 10, how likely are you to '
        f'recommend Aspired Websites to a friend or colleague?</p>'
        f'<p>{buttons}</p>'
        f'<p>Just tap the number that fits. Thank you!</p>'
        f'<p>— Zachery Long<br>Aspired Websites LLC</p>'
    )
    send_mail(
        subject='Quick question about your website',
        message='\n'.join(text_lines),
        from_email=settings.EMAIL_FROM_MAIN,
        recipient_list=[recipient],
        html_message=html,
        fail_silently=True,
    )


@shared_task
def send_nps_surveys():
    """Send NPS surveys to eligible maintenance clients (none recent, 30d+ old)."""
    from clients.models import ClientProfile

    from .models import NPSSurvey

    now = timezone.now()
    eligible = ClientProfile.objects.filter(
        maintenance_active=True,
        created_at__lte=now - timedelta(days=30),
    ).exclude(
        nps_surveys__sent_at__gte=now - timedelta(days=90),
    ).distinct()

    count = 0
    for client in eligible:
        survey = NPSSurvey.objects.create(client=client)
        send_nps_email(client, survey)
        count += 1
    return f'Sent {count} NPS survey(s).'


# ── Phase 5b Part 4: video testimonial requests ─────────────────────────────

def send_testimonial_email(client):
    """Send the one-time video testimonial request email."""
    from django.core.mail import send_mail

    recipient = getattr(client.user, 'email', '') if client.user_id else ''
    if not recipient:
        return
    name = client.contact_name or client.firm_name
    body = (
        f'Hi {name},\n\n'
        f"It's been a month since your site launched — I hope it's been "
        f'working well for you.\n\n'
        f"If you've had a good experience, I'd love to ask a small favor: "
        f'would you be willing to record a quick 1-2 minute video sharing '
        f'what the process was like?\n\n'
        f'You can record it on your phone and send it to '
        f'zacherylong@aspiredwebsites.com — even 60 seconds would mean a lot.'
        f'\n\nNo pressure at all — just thought I\'d ask!\n\n'
        f'— Zachery Long\nAspired Websites LLC\n'
    )
    send_mail(
        subject='Would you share your experience with Aspired Websites?',
        message=body,
        from_email=settings.EMAIL_FROM_MAIN,
        recipient_list=[recipient],
        fail_silently=True,
    )


@shared_task
def send_testimonial_requests():
    """One-time testimonial request ~30 days after a client's site launched."""
    from clients.models import ClientProfile

    thirty_days_ago = (timezone.now() - timedelta(days=30)).date()
    eligible = ClientProfile.objects.filter(
        projects__stage='live',
        projects__launch_date__lte=thirty_days_ago,
        testimonial_requested_at__isnull=True,
    ).distinct()

    count = 0
    for client in eligible:
        send_testimonial_email(client)
        client.testimonial_requested_at = timezone.now()
        client.save(update_fields=['testimonial_requested_at', 'updated_at'])
        count += 1
    return f'Sent {count} testimonial request(s).'


# ── Phase 6c — vulnerability scanner ──────────────────────────────────────

@shared_task
def run_vulnerability_scan_task(scan_id):
    """
    Celery wrapper around `run_full_scan`. Used by both the scheduled
    cadence and the on-demand admin button.
    """
    from reporting.scan_runner import run_full_scan
    run_full_scan(scan_id)


@shared_task
def check_scan_schedule():
    """
    Daily at 3am. For each active client with a Droplet IP and a live
    project, decide whether a scan is due:

      - first scan: 30 days after `do_droplet_created_at`
        (or immediately if the creation date isn't known — legacy)
      - subsequent: 30 days after the last *completed* scan

    Due scans are queued via `run_vulnerability_scan_task.delay`.
    """
    from clients.models import ClientProfile
    from reporting.models import VulnerabilityScan

    now = timezone.now()
    interval = timedelta(days=30)

    eligible = ClientProfile.objects.filter(
        status='active',
        do_droplet_ip__isnull=False,
    )

    queued = 0
    for client in eligible:
        if not client.do_droplet_ip:
            continue
        project = client.projects.filter(stage='live').first()
        if not project or not project.live_url:
            continue

        last = (VulnerabilityScan.objects
                .filter(client=client, status='complete')
                .order_by('-completed_at').first())

        if last is None:
            if client.do_droplet_created_at is None:
                should_scan = True   # legacy — kick the first scan now
            else:
                should_scan = now >= (
                    client.do_droplet_created_at + interval)
        else:
            should_scan = now >= (last.completed_at + interval)

        if not should_scan:
            continue

        scan = VulnerabilityScan.objects.create(
            client=client,
            target_url=project.live_url,
            target_ip=client.do_droplet_ip,
            scan_type='full',
            is_scheduled=True,
        )
        run_vulnerability_scan_task.delay(str(scan.id))
        queued += 1

    return f'Queued {queued} scheduled scan(s).'


# ── Tier 2 session recording — retention + storage report ─────────────────

@shared_task
def delete_expired_recordings():
    """
    Nightly at 02:00. Drops every `SessionRecording` whose
    `expires_at` has passed (30-day retention enforced at write
    time). Keeps the lightweight PageSession aggregate around —
    only the heavy rrweb event blobs are pruned.

    Returns a short summary string for Celery logs.
    """
    from django.utils import timezone as _tz
    from reporting.models import SessionRecording

    qs = SessionRecording.objects.filter(expires_at__lte=_tz.now())
    count = qs.count()
    if count:
        # Log the affected clients before we drop the rows so the
        # operator can audit if anything looks off.
        affected = list(qs.values_list(
            'client__firm_name', flat=True).distinct())
        logger.info(
            'session-recording purge: %d row(s) across %d client(s): %s',
            count, len(affected), ', '.join(sorted(affected))[:200])
        qs.delete()
    return f'Deleted {count} expired recording(s).'


@shared_task
def recording_storage_report():
    """
    Weekly. Emails the operator a warning for any session-recording
    client whose stored bytes exceed 500 MB. Lets us catch a chatty
    site before it costs real storage.
    """
    from django.conf import settings as _s
    from django.core.mail import send_mail
    from django.db.models import Count, Sum

    from clients.models import ClientProfile
    from reporting.models import SessionRecording

    clients = ClientProfile.objects.filter(
        session_recording_enabled=True)
    warnings = 0
    for client in clients:
        stats = SessionRecording.objects.filter(
            client=client, status='complete',
        ).aggregate(
            total_recordings=Count('id'),
            total_size_kb=Sum('estimated_size_kb'),
        )
        total_mb = (stats['total_size_kb'] or 0) / 1024
        if total_mb <= 500:
            continue
        try:
            send_mail(
                subject=(f'Storage warning: {client.firm_name} '
                         f'recordings at {total_mb:.0f}MB'),
                message=(
                    f'{client.firm_name} has '
                    f'{stats["total_recordings"]} '
                    f'session recording(s) using {total_mb:.0f}MB. '
                    f'Consider reducing retention or archiving '
                    f'older recordings.\n'),
                from_email=getattr(
                    _s, 'EMAIL_FROM_MAIN',
                    _s.DEFAULT_FROM_EMAIL),
                recipient_list=[_s.LEAD_NOTIFICATION_EMAIL],
                fail_silently=True,
            )
            warnings += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                'storage-report email failed for %s', client.pk)
    return f'Sent {warnings} storage warning(s).'
