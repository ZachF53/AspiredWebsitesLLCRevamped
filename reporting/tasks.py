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
def _primary_website(client):
    """The client's primary (oldest) Website, or None. Used to stamp the
    per-website FK on uptime rows during the Phase-D teardown."""
    try:
        acct = client.migrated_account
    except Exception:
        return None
    return acct.websites.order_by('created_at').first() if acct else None


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
        # client.website is the canonical live URL (2026-05-25 refactor).
        if not client.website:
            continue

        url = client.website
        if not url.startswith('http'):
            url = f'https://{url}'
        site = _primary_website(client)

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
                website_new=site,
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
                check_and_fire_alert(client, site)

        except requests.RequestException as exc:
            UptimeRecord.objects.create(
                client=client,
                website_new=site,
                response_time_ms=None,
                status_code=None,
                is_up=False,
                error_message=str(exc)[:200],
            )
            check_and_fire_alert(client, site)
        checked += 1

    return f'Checked {checked} client site(s).'


def check_and_fire_alert(client, site=None):
    """
    Open a downtime alert after 3 consecutive failed checks — once per
    outage, so a long outage does not spam the admin on every check.
    `site` is the Website to stamp on the alert (Phase-D per-website FK).
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
        client=client, website_new=site,
        consecutive_failures=3, alert_sent=True)

    live_url = client.website or '(unknown)'
    check_link = (
        f'/admin-dashboard/websites/{site.id}/uptime/' if site
        else '/admin-dashboard/accounts/')
    send_admin_alert(
        subject=f'🔴 Site Down: {client.firm_name}',
        message=(
            f'{client.firm_name} has been down for 3 consecutive checks.\n'
            f'Domain: {live_url}\n'
            f'Check: {check_link}'
        ),
    )


# ── Part 2: GBP NAP sync ────────────────────────────────────────────────────

GBP_NOT_CONNECTED = 'GBP not connected'


def _gbp_is_connected(client):
    """True when an agency operator's Google account is connected.

    Phase 6 — manager-invite model: ONE operator token covers every
    client. So this check is effectively "did anyone connect Google
    yet?" rather than per-client. Operator must also have bound the
    client to a GBP location resource via
    ClientProfile.gbp_location_name.
    """
    from social.services import google_access_token
    if not google_access_token(client):
        return False
    return bool(getattr(client, 'gbp_location_name', '') or '')


def _normalise(s):
    """Cheap normalisation for NAP comparisons."""
    if not s:
        return ''
    return ' '.join(s.lower().split())


@shared_task
def check_gbp_sync():
    """Weekly NAP comparison between each client's site and GBP listing.

    Phase 6 — uses social.services.google_access_token + the bound
    ClientProfile.gbp_location_name. Per-client try/except so one
    failing API call doesn't abort the whole run. On any is_mismatch,
    email LEAD_NOTIFICATION_EMAIL (best-effort).
    """
    from django.conf import settings as _settings
    from django.core.mail import send_mail

    from clients.models import ClientProfile

    from .models import GBPSyncCheck

    clients = ClientProfile.objects.filter(status='active')
    recorded = 0
    mismatch_count = 0
    for client in clients:
        if client.stage != 'live':
            continue

        if not _gbp_is_connected(client):
            GBPSyncCheck.objects.create(
                client=client,
                field_name='website',
                website_value=GBP_NOT_CONNECTED,
                gbp_value='Connect GBP via /admin-dashboard/gbp/connect/',
                is_mismatch=False,
            )
            recorded += 1
            continue

        # Connected client — make a real GBP fetch.
        try:
            from reporting.google_gbp import fetch_location
            from reporting.models import GbpOperatorToken
            token = (GbpOperatorToken.objects
                     .order_by('created_at').first())
            data = fetch_location(token, client.gbp_location_name)
        except Exception:
            logger.exception(
                'check_gbp_sync: fetch_location failed for %s', client.pk)
            GBPSyncCheck.objects.create(
                client=client,
                field_name='website',
                website_value='(error)',
                gbp_value='GBP API error — see logs',
                is_mismatch=False,
            )
            continue

        if not data:
            continue

        # Pull GBP-side values.
        gbp_name = (data.get('title') or '').strip()
        phones = data.get('phoneNumbers') or {}
        gbp_phone = (phones.get('primaryPhone') or '').strip()
        sa = data.get('storefrontAddress') or {}
        gbp_address = ' '.join(sa.get('addressLines') or []).strip()
        gbp_website = (data.get('websiteUri') or '').strip()

        comparisons = [
            ('business_name', (client.firm_name or '').strip(), gbp_name),
            ('phone', (client.phone or '').strip(), gbp_phone),
            ('address', (client.address or '').strip(), gbp_address),
            ('website', (client.website or '').strip(), gbp_website),
        ]
        client_mismatch = False
        for field_name, web_val, gbp_val in comparisons:
            mismatch = bool(web_val) and bool(gbp_val) and (
                _normalise(web_val) != _normalise(gbp_val))
            GBPSyncCheck.objects.create(
                client=client,
                field_name=field_name,
                website_value=web_val,
                gbp_value=gbp_val,
                is_mismatch=mismatch,
            )
            recorded += 1
            if mismatch:
                client_mismatch = True
                mismatch_count += 1

        # Per-client alert email — only one per run per client even if
        # multiple fields mismatched.
        if client_mismatch:
            try:
                send_mail(
                    subject=(
                        f'[GBP drift] {client.firm_name} — NAP fields '
                        f'differ from Google Business Profile'),
                    message=(
                        f'NAP drift detected for {client.firm_name}.\n\n'
                        f'See /admin-dashboard/gbp/clients/{client.id}/nap/'
                        f' for the field-by-field comparison.'),
                    from_email=getattr(
                        _settings, 'DEFAULT_FROM_EMAIL',
                        'zacherylong@aspiredwebsites.com'),
                    recipient_list=[getattr(
                        _settings, 'LEAD_NOTIFICATION_EMAIL',
                        'zacherylong@aspiredwebsites.com')],
                    fail_silently=True,
                )
            except Exception:
                logger.exception(
                    'check_gbp_sync: drift alert email failed for %s',
                    client.pk)

    return (f'Recorded {recorded} GBP sync row(s); '
            f'{mismatch_count} mismatch(es).')


# ── Part 3: Keyword rank tracking ───────────────────────────────────────────

def _gsc_query_position(token, kw):
    """Query Google Search Console for the avg position + impressions +
    clicks of one keyword over the past 7 days.

    Returns a dict with keys 'position' (rounded int or None),
    'impressions' (int), 'clicks' (int), or None if no rows or the
    request failed.
    """
    import datetime as _dt

    import requests

    # Resolve the GSC property from the client's website. Try both
    # https://example.com/ and sc-domain:example.com (domain property).
    site = (kw.client.website or '').strip()
    if not site:
        logger.warning(
            '_gsc_query_position: keyword %s has no client website set',
            kw.pk)
        return None

    if not site.startswith('http'):
        site = 'https://' + site
    site = site.rstrip('/') + '/'
    domain_property = 'sc-domain:' + (
        site.split('//', 1)[1].split('/', 1)[0])

    end = _dt.date.today()
    start = end - _dt.timedelta(days=7)
    body = {
        'startDate': start.isoformat(),
        'endDate':   end.isoformat(),
        'dimensions': ['query'],
        'dimensionFilterGroups': [{
            'filters': [{
                'dimension': 'query',
                'operator':  'equals',
                'expression': kw.keyword,
            }],
        }],
        'rowLimit': 1,
    }
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type':  'application/json',
    }
    for property_url in (site, domain_property):
        try:
            r = requests.post(
                ('https://searchconsole.googleapis.com/webmasters/v3'
                 f'/sites/{requests.utils.quote(property_url, safe="")}'
                 '/searchAnalytics/query'),
                json=body, headers=headers, timeout=20)
        except requests.RequestException:
            logger.exception(
                '_gsc_query_position: network error for %s', property_url)
            continue
        if r.status_code != 200:
            # Try the next property shape.
            continue
        rows = (r.json() or {}).get('rows') or []
        if not rows:
            return None
        row = rows[0]
        pos = row.get('position')
        return {
            'position':    int(round(pos)) if pos else None,
            'impressions': int(row.get('impressions') or 0),
            'clicks':      int(row.get('clicks') or 0),
        }
    return None


@shared_task
def check_keyword_ranks():
    """Weekly GSC rank pull for every active TrackedKeyword.

    Phase 6 — connected clients (operator has linked Google account)
    get a real KeywordRankRecord. Disconnected clients are skipped so
    the manual-entry admin path keeps working.
    """
    from social.services import google_access_token

    from .models import KeywordRankRecord, TrackedKeyword

    created = 0
    skipped_no_token = 0
    errors = 0
    for kw in (TrackedKeyword.objects
               .filter(is_active=True)
               .select_related('client')):
        token = google_access_token(kw.client)
        if not token:
            skipped_no_token += 1
            continue
        try:
            row = _gsc_query_position(token, kw)
        except Exception:
            logger.exception(
                'check_keyword_ranks: _gsc_query_position raised for %s',
                kw.pk)
            errors += 1
            continue
        if row is None:
            continue
        KeywordRankRecord.objects.create(
            keyword=kw,
            position=row.get('position'),
            impressions=row.get('impressions', 0),
            clicks=row.get('clicks', 0),
        )
        created += 1
    return (f'Keyword ranks: {created} created, '
            f'{skipped_no_token} skipped (no token), '
            f'{errors} errored.')


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
    """Email the report file to the client via the branded HTML template."""
    import os

    from clients.emails import send_branded

    client = report.client
    month_str = report.report_month.strftime('%B %Y')
    recipient = getattr(client.user, 'email', '') if client.user_id else ''
    if not recipient:
        report.status = 'failed'
        report.save(update_fields=['status', 'updated_at'])
        return

    name = client.contact_name or client.firm_name
    uptime = report.uptime_30d if report.uptime_30d is not None else 'N/A'
    portal_url = 'https://aspiredwebsites.com/portal/reports/'

    text_body = (
        f'Hi {name},\n\n'
        f'Your monthly performance report for {month_str} is attached.\n\n'
        f'Uptime this month: {uptime}{"%" if uptime != "N/A" else ""}\n\n'
        f'Log into your portal anytime to see your full activity history:\n'
        f'{portal_url}\n\n'
        f'— Zachery Long\nAspired Websites LLC\n'
    )

    attachments = None
    abs_path = os.path.join(settings.MEDIA_ROOT, report.pdf_path or '')
    if report.pdf_path and os.path.exists(abs_path):
        mime = 'application/pdf' if abs_path.endswith('.pdf') else 'text/html'
        with open(abs_path, 'rb') as fh:
            attachments = [(os.path.basename(abs_path), fh.read(), mime)]

    try:
        send_branded(
            subject=(f'Your Monthly Report — {month_str} — '
                     f'{client.firm_name}'),
            template='monthly_report',
            context={
                'name': name,
                'month_str': month_str,
                'uptime': uptime,
                'portal_url': portal_url,
                'preheader': (
                    f'{month_str} performance report attached.'),
            },
            recipient_list=[recipient],
            text_body=text_body,
            from_email=settings.EMAIL_FROM_NO_REPLY,
            attachments=attachments,
            fail_silently=False,
        )
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
    if not client.website:
        return 'No live site to crawl.'

    base_url = client.website
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
        website_new=_primary_website(client),
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
    """Send the NPS survey email with 0-10 scoring buttons."""
    from clients.emails import send_branded

    recipient = getattr(client.user, 'email', '') if client.user_id else ''
    if not recipient:
        return
    name = client.contact_name or client.firm_name
    base_url = f'{settings.SITE_BASE_URL}/nps/{survey.survey_token}/'

    text_lines = [
        f'Hi {name},', '',
        'A quick question — on a scale of 0 to 10, how likely are you to '
        'recommend Aspired Websites to a friend or colleague?', '',
    ]
    text_lines += [f'  {n}: {base_url}{n}/' for n in range(11)]
    text_lines += ['', 'Just click the number that fits. Thank you!', '',
                   '— Zachery Long', 'Aspired Websites LLC']

    send_branded(
        subject='Quick question about your website',
        template='nps_survey',
        context={
            'name': name,
            'base_url': base_url,
            'scores_low': list(range(0, 6)),    # 0-5
            'scores_high': list(range(6, 11)),  # 6-10
            'preheader': (
                'On a scale of 0–10, how likely are you to recommend us?'),
        },
        recipient_list=[recipient],
        text_body='\n'.join(text_lines),
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
    from clients.emails import send_branded

    recipient = getattr(client.user, 'email', '') if client.user_id else ''
    if not recipient:
        return
    name = client.contact_name or client.firm_name
    text_body = (
        f'Hi {name},\n\n'
        f"It's been a month since your site launched — I hope it's been "
        f'working well for you.\n\n'
        f"If you've had a good experience, I'd love to ask a small favor: "
        f'would you be willing to record a quick 1-2 minute video sharing '
        f'what the process was like?\n\n'
        f'You can record it on your phone and email it back. Even 60 '
        f'seconds would mean a lot.\n\n'
        f'No pressure at all — just thought I\'d ask.\n\n'
        f'— Zachery Long\nAspired Websites LLC\n'
    )
    send_branded(
        subject='Would you share your experience with Aspired Websites?',
        template='testimonial_request',
        context={
            'name': name,
            'preheader': (
                'A quick favor — a 1–2 minute video of your experience.'),
        },
        recipient_list=[recipient],
        text_body=text_body,
    )


@shared_task
def send_testimonial_requests():
    """One-time testimonial request ~30 days after a client's site launched."""
    from clients.models import ClientProfile

    # Post-2026-05-25 refactor: stage + launch_date on ClientProfile.
    thirty_days_ago = (timezone.now() - timedelta(days=30)).date()
    eligible = ClientProfile.objects.filter(
        stage='live',
        launch_date__lte=thirty_days_ago,
        testimonial_requested_at__isnull=True,
    )

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
        # Canonical URL: client.website (post-2026-05-25 backfill).
        target_url = client.website or ''
        if not target_url:
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
            target_url=target_url,
            target_ip=client.do_droplet_ip,
            scan_type='full',
            is_scheduled=True,
        )
        async_result = run_vulnerability_scan_task.delay(str(scan.id))
        scan.celery_task_id = async_result.id or ''
        scan.save(update_fields=['celery_task_id', 'updated_at'])
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


# ── DMARC aggregate-report ingest ──────────────────────────────────────────

@shared_task
def ingest_dmarc_imap_task():
    """
    Daily. Polls the configured IMAP mailbox for DMARC aggregate reports
    and ingests every attachment found. Opt-in via DMARC_IMAP_* env vars;
    the command no-ops cleanly when they're not set, so this task is
    safe to schedule on every environment.
    """
    from io import StringIO

    from django.core.management import call_command

    buf = StringIO()
    try:
        call_command('ingest_dmarc_imap', stdout=buf, stderr=buf)
    except Exception:  # noqa: BLE001
        logger.exception('ingest_dmarc_imap task failed')
        return 'failed'
    output = buf.getvalue().strip()
    # One-line summary line is plenty for the Celery log.
    last = output.splitlines()[-1] if output else ''
    return last or 'ok'


# ── Redis connection monitor ───────────────────────────────────────────────

# Categories we bucket clients into, derived from the CLIENT SETNAME
# label set in AspiredWebsitesRevamped.redis_naming. Order matters —
# first prefix that matches wins.
_REDIS_NAME_BUCKETS = [
    ('gunicorn-',     'gunicorn'),
    ('celery-worker', 'celery_worker'),
    ('celery-beat',   'celery_beat'),
    ('celery-',       'celery'),
    ('daphne-',       'daphne'),
    ('runserver-',    'runserver'),
    ('mgmt-',         'mgmt'),
    ('py-',           'py'),
]


def _categorize_client_name(name):
    """'gunicorn-1234' → 'gunicorn'; empty/unknown → 'unknown'."""
    if not name:
        return 'unknown'
    for prefix, bucket in _REDIS_NAME_BUCKETS:
        if name.startswith(prefix):
            return bucket
    return 'unknown'


@shared_task
def snapshot_redis_clients_task():
    """
    Every 5 minutes — call CLIENT LIST against the configured Redis,
    bucket each connection by its name prefix, write a snapshot row,
    then prune snapshots older than 30 days.

    Best-effort: a Redis hiccup logs the exception but does NOT
    propagate (we don't want the monitor itself contributing to a
    Redis-related outage).
    """
    from datetime import timedelta

    from django.conf import settings as _s
    from django.utils import timezone as _tz

    from reporting.models import RedisConnectionSnapshot

    try:
        import redis
        r = redis.from_url(_s.REDIS_URL)
        raw = r.execute_command('CLIENT', 'LIST')
        # redis-py returns bytes for CLIENT LIST in some versions.
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode('utf-8', 'replace')
        lines = [line for line in raw.splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        logger.exception('redis snapshot task: CLIENT LIST failed')
        return 'failed'

    counts = {}
    for line in lines:
        name = ''
        for field in line.split(' '):
            if field.startswith('name='):
                name = field[len('name='):]
                break
        bucket = _categorize_client_name(name)
        counts[bucket] = counts.get(bucket, 0) + 1

    # Keep a short sample of the raw output for forensics — limited so
    # we never store megabytes per snapshot if the client list explodes.
    sample = '\n'.join(lines[:30])
    if len(lines) > 30:
        sample += f'\n... ({len(lines) - 30} more)'

    snapshot = RedisConnectionSnapshot.objects.create(
        total=len(lines),
        by_category=counts,
        sample_raw=sample[:8000],
    )

    # Prune older than 30 days. Single DELETE — no model signals fire
    # on bulk delete, which is what we want here.
    cutoff = _tz.now() - timedelta(days=30)
    deleted, _ = RedisConnectionSnapshot.objects.filter(
        captured_at__lt=cutoff).delete()

    return (
        f'total={snapshot.total} categories={counts} '
        f'pruned={deleted}'
    )


@shared_task
def provision_ga4_task(website_id):
    """Create the GA4 property + web stream for a Website at intake
    completion. Best-effort; no-ops if Google isn't connected / configured."""
    from clients.account_models import Website
    from reporting.ga4 import provision_ga4_for_website
    website = Website.objects.filter(id=website_id).first()
    if website is None:
        return 'no website'
    mid = provision_ga4_for_website(website)
    return f'ga4={mid or "skipped"}'
