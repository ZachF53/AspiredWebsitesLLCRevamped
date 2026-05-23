"""
Celery tasks for the clients app — Phase 7 Part 1.

`calculate_all_health_scores` runs every morning at 06:00, walks
every active non-tester maintenance client, persists a fresh
`ClientHealthScore`, and (de-bouncing on the per-client 7-day
window) fires a churn-risk alert email when the score is critical.

`take_monthly_revenue_snapshot` runs at 01:00 on the 1st of every
month and stamps a `RevenueSnapshot` row that the BI dashboard's
trend chart reads.

Beat entries live in `AspiredWebsitesRevamped/settings.py` under
CELERY_BEAT_SCHEDULE.
"""

import json
import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def calculate_all_health_scores():
    """
    Recalculate health for every active non-tester client. Returns the
    count of scores written (handy for monitoring the cron run).
    """
    from clients.health import calculate_client_health
    from clients.models import ClientProfile

    qs = ClientProfile.objects.filter(
        status='active',
        is_tester=False,
    )

    written = 0
    for client in qs:
        try:
            score = calculate_client_health(client)
            score.save()
            written += 1
            if score.churn_risk:
                _fire_churn_alert(client, score)
        except Exception:
            logger.exception(
                'Health score calc failed for %s', client.pk)
            continue
    return f'Wrote {written} health score(s).'


def _fire_churn_alert(client, score):
    """
    Email the admin once per 7-day rolling window per client when a
    fresh score is critical. De-duplicates on prior `churn_risk=True`
    rows so a string of bad days doesn't spam the inbox.
    """
    from clients.models import ClientHealthScore

    week_ago = timezone.now() - timedelta(days=7)
    prior_alerts = ClientHealthScore.objects.filter(
        client=client,
        churn_risk=True,
        calculated_at__gte=week_ago,
    ).exclude(pk=score.pk).count()
    if prior_alerts:
        return  # Already alerted this week.

    subject = (f'[Churn Risk] {client.firm_name} — '
               f'Health Score {score.score}/100')
    message = (
        f'Client health score has dropped into the critical band.\n\n'
        f'Client:       {client.firm_name}\n'
        f'Score:        {score.score}/100  ({score.health_status})\n'
        f'Payment:      {score.payment_score}/100\n'
        f'Engagement:   {score.engagement_score}/100\n'
        f'NPS:          {score.nps_score_component}/100\n'
        f'Uptime:       {score.uptime_score}/100\n'
        f'Support:      {score.support_score}/100\n\n'
        f'Review at:\n'
        f'{settings.SITE_BASE_URL}/admin-dashboard/clients/'
        f'{client.id}/\n'
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
        logger.exception('Failed to send churn-risk email')


@shared_task
def take_monthly_revenue_snapshot():
    """
    Persist this month's RevenueSnapshot row. Idempotent — running by
    hand or twice in one month just overwrites the existing row.
    """
    from clients.revenue import take_revenue_snapshot
    snap = take_revenue_snapshot()
    return (
        f'Snapshot {snap.snapshot_month}: '
        f'MRR ${snap.mrr_total} '
        f'({snap.active_maintenance_clients} maint clients)'
    )


@shared_task
def check_case_study_prompts():
    """
    Daily — for every client launched 30+ days ago with no CaseStudy
    yet, email the admin a 'case study needed' prompt. De-duplicates
    on a 7-day rolling window so a slow week doesn't spam the inbox.
    """
    from clients.models import CaseStudy, ClientProfile

    thirty_days_ago = (timezone.now() - timedelta(days=30)).date()
    week_ago = timezone.now() - timedelta(days=7)

    candidates = (
        ClientProfile.objects
        .filter(
            projects__stage='live',
            projects__launch_date__lte=thirty_days_ago,
            is_tester=False,
        )
        .exclude(case_studies__isnull=False)
        .distinct()
    )

    sent = 0
    for client in candidates:
        # 7-day dedupe key — settings cache works across workers.
        cache_key = f'cs_prompt:{client.id}'
        from django.core.cache import cache
        if cache.get(cache_key):
            continue

        subject = f'Case study needed: {client.firm_name}'
        url = (f'{settings.SITE_BASE_URL}'
               f'/admin-dashboard/case-studies/new/?client={client.id}')
        body = (
            f'{client.firm_name} launched 30+ days ago and still has no '
            f'case study. The results are now in long enough to write '
            f'one up.\n\n'
            f'Draft the case study (AI Draft button pre-fills it):\n'
            f'{url}\n'
        )
        try:
            send_mail(
                subject, body,
                getattr(settings, 'EMAIL_FROM_NO_REPLY',
                        settings.DEFAULT_FROM_EMAIL),
                [settings.LEAD_NOTIFICATION_EMAIL],
                fail_silently=True,
            )
            cache.set(cache_key, '1', timeout=7 * 24 * 3600)
            sent += 1
        except Exception:
            logger.exception(
                'case-study prompt email failed for %s', client.pk)
    return f'Sent {sent} case-study prompt(s).'


@shared_task
def run_intelligence_for_client(client_id):
    """
    Run the Website Intelligence Engine for a single client. Creates
    an `IntelligenceReport` row plus one `IntelligenceSuggestion`
    per suggestion Claude returned.

    Idempotent at month-grain: if a report row already exists for
    this client + this calendar month, returns without re-running
    (so an admin running the monthly beat twice is a no-op).

    Returns a short summary string for Celery logs / shell calls.
    """
    from datetime import date

    from clients.intelligence import run_intelligence_analysis
    from clients.models import (
        ClientProfile, IntelligenceReport, IntelligenceSuggestion,
    )

    try:
        client = ClientProfile.objects.get(id=client_id)
    except ClientProfile.DoesNotExist:
        return f'Client {client_id} not found.'

    report_month = date.today().replace(day=1)
    existing = (IntelligenceReport.objects
                .filter(client=client, report_month=report_month)
                .first())
    if existing:
        return (f'Already ran for {client.firm_name} '
                f'this month ({report_month.isoformat()}).')

    result = run_intelligence_analysis(client)
    suggestions = result.get('suggestions') or []

    if result.get('error') and not suggestions:
        status = 'failed'
    elif not suggestions:
        status = 'no_suggestions'
    else:
        status = 'complete'

    report = IntelligenceReport.objects.create(
        client=client,
        report_month=report_month,
        data_snapshot=result.get('data_snapshot', {}) or {},
        overall_assessment=result.get('overall_assessment', '') or '',
        suggestions_count=len(suggestions),
        status=status,
        total_tokens_used=int(result.get('tokens_used', 0) or 0),
    )

    valid_types = {choice for choice, _
                   in IntelligenceSuggestion.SUGGESTION_TYPE_CHOICES}
    for s in suggestions:
        s_type = (s.get('type') or 'other').strip().lower()
        if s_type not in valid_types:
            s_type = 'other'
        try:
            fee = float(s.get('one_time_fee') or 0)
        except (TypeError, ValueError):
            fee = 0
        IntelligenceSuggestion.objects.create(
            client=client,
            report=report,
            suggestion_type=s_type,
            title=(s.get('title') or '')[:300],
            description=s.get('description', '') or '',
            expected_impact=s.get('expected_impact', '') or '',
            implementation_notes=s.get('implementation_notes', '') or '',
            one_time_fee=fee,
            maintenance_equivalent=s.get(
                'maintenance_equivalent', '') or '',
            is_in_maintenance_scope=bool(
                s.get('is_in_maintenance_scope')),
            data_sources=s.get('data_sources') or [],
            ai_reasoning=json.dumps(s, default=str),
            status='pending_review',
        )

    return (f'{client.firm_name}: {len(suggestions)} '
            f'suggestion(s), status={status}.')


@shared_task
def run_monthly_intelligence():
    """
    Trigger `run_intelligence_for_client` for every active non-tester
    client on the 15th of the month. Staggers calls 30 seconds apart
    so a busy month doesn't bunch-up against the Anthropic rate limit.
    """
    from clients.models import ClientProfile

    clients = list(
        ClientProfile.objects
        .filter(status='active', is_tester=False)
        .order_by('firm_name')
    )
    for i, client in enumerate(clients):
        run_intelligence_for_client.apply_async(
            args=[str(client.id)],
            countdown=i * 30,
        )
    return f'Queued {len(clients)} client analyses.'


@shared_task
def generate_annual_report(client_id, year):
    """
    Generate the year-in-review PDF for one client + year.

    Idempotent on `(client, year)`: re-running for a row that is
    already `ready` or `sent` is a no-op so an operator can mash
    "Generate" without consequence.

    Renders via WeasyPrint with an HTML fallback (Windows dev /
    fresh servers without the native libs — same pattern as
    `clients.pdf_utils` and `clients.proposal_pdf`).
    """
    from pathlib import Path

    from django.conf import settings
    from django.core.mail import send_mail
    from django.template.loader import render_to_string

    from clients.intelligence import (
        gather_annual_data, generate_annual_narrative,
    )
    from clients.models import AnnualReport, ClientProfile

    try:
        client = ClientProfile.objects.get(id=client_id)
    except ClientProfile.DoesNotExist:
        return f'Client {client_id} not found.'

    existing = AnnualReport.objects.filter(
        client=client, report_year=year).first()
    if existing and existing.status in ('ready', 'sent'):
        return (f'Already ready: {client.firm_name} {year} '
                f'(status={existing.status})')

    report, _ = AnnualReport.objects.get_or_create(
        client=client, report_year=year,
        defaults={'status': 'generating'},
    )
    report.status = 'generating'
    report.save(update_fields=['status', 'updated_at'])

    try:
        data = gather_annual_data(client, year)
        report.report_data = data
        report.save(update_fields=['report_data', 'updated_at'])

        narrative, tokens = generate_annual_narrative(client, data)
        report.executive_summary = narrative.get(
            'executive_summary', '') or ''
        report.year_in_review = narrative.get(
            'year_in_review', '') or ''
        report.looking_ahead = narrative.get(
            'looking_ahead', '') or ''
        report.total_tokens_used = int(tokens or 0)
        report.save(update_fields=[
            'executive_summary', 'year_in_review',
            'looking_ahead', 'total_tokens_used', 'updated_at',
        ])

        html_string = render_to_string(
            'clients/annual_report.html', {
                'client': client,
                'report': report,
                'data': data,
                'year': year,
            })

        rel_dir = Path('annual_reports') / str(client.id)
        abs_dir = Path(settings.MEDIA_ROOT) / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)

        rel_pdf = rel_dir / f'annual-report-{year}.pdf'
        abs_pdf = Path(settings.MEDIA_ROOT) / rel_pdf

        try:
            from weasyprint import HTML
            HTML(string=html_string).write_pdf(str(abs_pdf))
            saved_rel = str(rel_pdf).replace('\\', '/')
        except Exception:
            logger.exception(
                'WeasyPrint failed for annual report %s/%s — '
                'falling back to .html', client.pk, year)
            rel_html = rel_dir / f'annual-report-{year}.html'
            (Path(settings.MEDIA_ROOT) / rel_html).write_text(
                html_string, encoding='utf-8')
            saved_rel = str(rel_html).replace('\\', '/')

        report.pdf_path = saved_rel
        report.status = 'ready'
        report.save(update_fields=[
            'pdf_path', 'status', 'updated_at'])

        # Operator notification — best-effort, never blocks the task.
        try:
            send_mail(
                subject=(f'Annual Report Ready: '
                         f'{client.firm_name} — {year}'),
                message=(
                    f'Annual report generated for '
                    f'{client.firm_name}.\n\n'
                    f'Review and send at:\n'
                    f'{settings.SITE_BASE_URL}/admin-dashboard/'
                    f'annual-reports/{report.id}/\n'),
                from_email=getattr(
                    settings, 'EMAIL_FROM_MAIN',
                    settings.DEFAULT_FROM_EMAIL),
                recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
                fail_silently=True,
            )
        except Exception:
            logger.exception('annual-report admin email failed')

        return (f'Ready: {client.firm_name} {year} '
                f'({report.total_tokens_used} tokens)')
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'generate_annual_report failed for %s/%s',
            client.pk, year)
        report.status = 'failed'
        report.save(update_fields=['status', 'updated_at'])
        return f'FAILED: {client.firm_name} {year}: {exc}'


@shared_task
def run_competitor_gap_analysis(client_id):
    """
    Crawl the client + every competitor, hand the lists to Claude,
    persist a `CompetitorGapReport`. Idempotent at month grain:
    re-running for a client that already has a row this month is
    a no-op.

    No competitors set → row marked `no_competitors` so the admin
    table still shows it (and we won't keep trying every minute).
    Missing live URL → row marked `failed` for the same reason.
    """
    from datetime import date

    from clients.intelligence import (
        analyze_competitor_gaps, crawl_site_for_pages,
    )
    from clients.models import (
        ClientCompetitor, ClientProfile, CompetitorGapReport,
    )

    try:
        client = ClientProfile.objects.get(id=client_id)
    except ClientProfile.DoesNotExist:
        return f'Client {client_id} not found.'

    report_month = date.today().replace(day=1)
    if CompetitorGapReport.objects.filter(
            client=client, report_month=report_month).exists():
        return (f'Already ran for {client.firm_name} this month '
                f'({report_month.isoformat()}).')

    competitors = list(
        ClientCompetitor.objects.filter(client=client)[:3])
    if not competitors:
        CompetitorGapReport.objects.create(
            client=client, report_month=report_month,
            status='no_competitors',
        )
        return f'{client.firm_name}: no competitors set.'

    project = client.projects.filter(stage='live').first()
    client_url = (project.live_url if project else '') or ''
    if not client_url:
        CompetitorGapReport.objects.create(
            client=client, report_month=report_month,
            status='failed',
            overall_assessment='Client has no live URL set.',
        )
        return (f'{client.firm_name}: skipped — no live URL.')

    report = CompetitorGapReport.objects.create(
        client=client, report_month=report_month,
        status='generating',
    )

    try:
        client_pages = crawl_site_for_pages(
            client_url, max_pages=30)
        report.client_pages = client_pages
        report.save(update_fields=['client_pages', 'updated_at'])

        competitor_data = []
        for comp in competitors:
            comp_pages = crawl_site_for_pages(
                comp.domain, max_pages=25)
            competitor_data.append({
                'competitor_name': comp.name,
                'competitor_domain': comp.domain,
                'pages': comp_pages,
            })
        report.competitor_data = competitor_data
        report.save(update_fields=[
            'competitor_data', 'updated_at'])

        result, tokens = analyze_competitor_gaps(
            client, client_pages, competitor_data)
        gaps = result.get('gaps', []) or []

        report.gaps = gaps
        report.overall_assessment = result.get(
            'overall_assessment', '') or ''
        report.total_gaps_found = len(gaps)
        report.high_priority_gaps = sum(
            1 for g in gaps if g.get('priority') == 'high')
        report.total_tokens_used = int(tokens or 0)
        report.status = 'complete'
        report.save(update_fields=[
            'gaps', 'overall_assessment', 'total_gaps_found',
            'high_priority_gaps', 'total_tokens_used', 'status',
            'updated_at',
        ])

        if report.high_priority_gaps > 0:
            _notify_competitor_gaps(client, report)

        return (f'{client.firm_name}: {report.total_gaps_found} '
                f'gap(s), {report.high_priority_gaps} high '
                f'priority.')
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'competitor gap analysis failed for %s', client.pk)
        report.status = 'failed'
        report.overall_assessment = (
            f'Analysis failed: {str(exc)[:300]}')
        report.save(update_fields=[
            'status', 'overall_assessment', 'updated_at'])
        return f'FAILED: {client.firm_name}: {exc}'


def _notify_competitor_gaps(client, report):
    """Email the admin a digest of high-priority gaps; idempotent."""
    if report.admin_notified:
        return
    high_gaps = [g for g in report.gaps
                 if g.get('priority') == 'high']
    if not high_gaps:
        return
    gap_list = '\n'.join(f'  - {g["title"]}' for g in high_gaps[:5])
    try:
        send_mail(
            subject=(f'Competitor gaps found: {client.firm_name} '
                     f'— {report.high_priority_gaps} high '
                     f'priority'),
            message=(
                f'Competitor content gap analysis complete for '
                f'{client.firm_name}.\n\n'
                f'High priority gaps:\n{gap_list}\n\n'
                f'Review at:\n'
                f'{settings.SITE_BASE_URL}/admin-dashboard/'
                f'competitor-gaps/{report.id}/\n'),
            from_email=getattr(settings, 'EMAIL_FROM_MAIN',
                               settings.DEFAULT_FROM_EMAIL),
            recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
            fail_silently=True,
        )
        report.admin_notified = True
        report.save(update_fields=[
            'admin_notified', 'updated_at'])
    except Exception:
        logger.exception('competitor-gap admin email failed')


@shared_task
def run_monthly_competitor_gaps():
    """
    Beat: 20th of every month, 10:00. Queues a per-client analysis
    for every active non-tester client that has at least one
    competitor recorded. Staggers calls 60s apart — crawling is
    bandwidth-bound, not API-bound, and we want to be polite to
    competitor sites.
    """
    from clients.models import ClientProfile

    clients = list(
        ClientProfile.objects
        .filter(status='active', is_tester=False,
                competitors__isnull=False)
        .distinct()
        .order_by('firm_name')
    )
    for i, client in enumerate(clients):
        run_competitor_gap_analysis.apply_async(
            args=[str(client.id)],
            countdown=i * 60,
        )
    return f'Queued {len(clients)} competitor analyses.'


@shared_task
def check_annual_report_schedule():
    """
    Monthly beat — on the 1st of each month at 09:00, queue a
    `generate_annual_report` for any client whose current month
    matches the month of their `Project.launch_date` AND the
    launch happened in a prior year. The report always covers the
    previous calendar year.
    """
    from datetime import date

    from clients.models import AnnualReport, ClientProfile

    today = date.today()
    active = ClientProfile.objects.filter(
        status='active', is_tester=False)

    queued = 0
    for client in active:
        project = client.projects.filter(stage='live').first()
        if not project or not project.launch_date:
            continue
        launch = project.launch_date
        if today.month != launch.month:
            continue
        if today.year <= launch.year:
            # First anniversary hasn't arrived yet.
            continue

        report_year = today.year - 1
        if AnnualReport.objects.filter(
                client=client, report_year=report_year).exists():
            continue

        generate_annual_report.delay(str(client.id), report_year)
        queued += 1
    return f'Queued {queued} annual report(s).'


@shared_task
def expire_old_proposals():
    """
    Daily — flip Proposal.status to 'expired' when expires_at has
    passed and the prospect hasn't accepted/declined yet. Keeps the
    proposals table tidy and lets the BI dashboard count active
    proposals accurately.
    """
    from clients.models import Proposal

    today = timezone.now().date()
    qs = Proposal.objects.filter(
        status__in=['draft', 'sent', 'viewed'],
        expires_at__isnull=False,
        expires_at__lt=today,
    )
    n = qs.update(status='expired', updated_at=timezone.now())
    return f'Expired {n} proposal(s).'
