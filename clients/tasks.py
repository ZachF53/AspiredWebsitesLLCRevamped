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

from clients.website_helpers import primary_website

logger = logging.getLogger(__name__)


@shared_task
def calculate_all_health_scores():
    """
    Recalculate health for every active non-tester client. Returns the
    count of scores written (handy for monitoring the cron run).
    """
    from clients.canonical_iteration import profiles_with_coverage_report
    from clients.health import calculate_client_health

    # Reports any Account with no legacy profile instead of skipping it
    # silently — see clients/canonical_iteration.py.
    qs = profiles_with_coverage_report(
        'calculate_all_health_scores',
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
    from clients.account_models import Website

    thirty_days_ago = (timezone.now() - timedelta(days=30)).date()

    # Per website, not per account. An account with two live sites has two
    # case studies to write; prompting once per account meant the second
    # site never got one.
    candidates = (
        Website.objects
        .filter(
            stage='live',
            launch_date__lte=thirty_days_ago,
            account__is_tester=False,
        )
        .exclude(case_studies_new__isnull=False)
        .select_related('account')
    )

    sent = 0
    for website in candidates:
        # 7-day dedupe key - settings cache works across workers.
        cache_key = f'cs_prompt:{website.id}'
        from django.core.cache import cache
        if cache.get(cache_key):
            continue

        label = website.name
        subject = f'Case study needed: {label}'
        url = (f'{settings.SITE_BASE_URL}'
               f'/admin-dashboard/case-studies/new/?website={website.id}')
        body = (
            f'{label} launched 30+ days ago and still has no '
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
                'case-study prompt email failed for %s', website.pk)
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

    # The intelligence admin pages are per-website — stamp both the report
    # and every suggestion hanging off it, or they render as empty.
    site = primary_website(client)

    report = IntelligenceReport.objects.create(
        client=client,
        website_new=site,
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
            website_new=site,
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
    from clients.canonical_iteration import profiles_with_coverage_report

    clients = list(
        profiles_with_coverage_report(
            'run_monthly_intelligence', status='active', is_tester=False)
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

    # Keyed on the website, matching the unique constraint. `client` is
    # nullable and new rows leave it NULL, so a lookup including it would
    # miss the existing row, create a second, and violate
    # (website_new, report_year).
    site = primary_website(client)
    key = ({'website_new': site} if site is not None
           else {'client': client})

    existing = AnnualReport.objects.filter(
        report_year=year, **key).first()
    if existing and existing.status in ('ready', 'sent'):
        return (f'Already ready: {client.firm_name} {year} '
                f'(status={existing.status})')

    report, _ = AnnualReport.objects.get_or_create(
        report_year=year, **key,
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

    # Gap-report pages filter by website_new — stamp every exit path,
    # including the early no-competitors / no-URL rows.
    site = primary_website(client)

    competitors = list(
        ClientCompetitor.objects.filter(client=client)[:3])
    if not competitors:
        CompetitorGapReport.objects.create(
            client=client, website_new=site, report_month=report_month,
            status='no_competitors',
        )
        return f'{client.firm_name}: no competitors set.'

    # client.website is the canonical live URL (2026-05-25 refactor).
    client_url = client.website or ''
    if not client_url:
        CompetitorGapReport.objects.create(
            client=client, website_new=site, report_month=report_month,
            status='failed',
            overall_assessment='Client has no live URL set.',
        )
        return (f'{client.firm_name}: skipped — no live URL.')

    report = CompetitorGapReport.objects.create(
        client=client, website_new=site, report_month=report_month,
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
    from clients.canonical_iteration import profiles_with_coverage_report

    clients = list(
        profiles_with_coverage_report(
            'run_monthly_competitor_gaps', status='active', is_tester=False,
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

    from clients.canonical_iteration import profiles_with_coverage_report

    today = date.today()
    active = profiles_with_coverage_report(
        'check_annual_report_schedule', status='active', is_tester=False)

    queued = 0
    for client in active:
        if client.stage != 'live' or not client.launch_date:
            continue
        launch = client.launch_date
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


# ── Onboarding reminders (Part 7) ───────────────────────────────────────────

@shared_task
def send_onboarding_reminders():
    """
    Beat-every-12h sweep — emails clients who haven't finished onboarding:

      - pending_setup  → 24h between nudges (the link is in their inbox;
        a daily reminder is enough)
      - pending_intake → 48h between nudges (they're logged in; this is
        a softer ask)

    Throttle is checked against the per-token timestamp so a slow Celery
    run or a manually triggered task doesn't double-send.
    """
    from clients.account_models import Account
    from clients.models import OnboardingToken

    now = timezone.now()
    DAY = 86400      # seconds
    TWO_DAYS = 172800

    # Account setup is account-level: one login, one password, one PIN.
    # Keyed on ClientProfile, this skipped every account created after the
    # cutover — the shape all new accounts take — so those clients were
    # never reminded to set up the account they had just paid for.
    setup_qs = (
        Account.objects
        .filter(
            onboarding_status='pending_setup',
            status='active',
            user__is_active=False,
        )
        .select_related('user')
    )

    setup_sent = 0
    for client in setup_qs:
        token = OnboardingToken.objects.filter(
            account_new=client).first()
        if token is None or token.used:
            continue
        last = token.last_setup_reminder_at
        if last and (now - last).total_seconds() < DAY:
            continue
        try:
            _send_setup_reminder(client, token)
            token.setup_reminders_sent += 1
            token.last_setup_reminder_at = now
            token.save(update_fields=[
                'setup_reminders_sent',
                'last_setup_reminder_at',
                'updated_at',
            ])
            setup_sent += 1
        except Exception:
            logger.exception(
                'setup reminder failed for %s', client.pk)

    # The intake is per WEBSITE — it describes a build. A client with two
    # builds owes two intakes and is chased for each.
    from clients.account_models import Website

    intake_qs = (
        Website.objects
        .filter(
            onboarding_status='pending_intake',
            status='active',
            account__status='active',
            account__user__is_active=True,
        )
        .select_related('account', 'account__user')
    )

    intake_sent = 0
    for client in intake_qs:
        token = OnboardingToken.objects.filter(
            account_new=client.account).first()
        if token is None:
            continue
        last = token.last_intake_reminder_at
        if last and (now - last).total_seconds() < TWO_DAYS:
            continue
        try:
            _send_intake_reminder(client, token)
            token.intake_reminders_sent += 1
            token.last_intake_reminder_at = now
            token.save(update_fields=[
                'intake_reminders_sent',
                'last_intake_reminder_at',
                'updated_at',
            ])
            intake_sent += 1
        except Exception:
            logger.exception(
                'intake reminder failed for %s', client.pk)

    return (f'Setup reminders sent: {setup_sent}, '
            f'intake reminders sent: {intake_sent}')


def _setup_first_name(owner):
    """First name for the nudge. `owner` is an Account or a Website."""
    from clients.display import owner_recipient

    _email, name = owner_recipient(owner)
    name = (name or '').strip()
    return name.split(' ')[0] if name else 'there'


def _recipient_email(owner):
    """Best-effort recipient — the user record holds the canonical email."""
    from clients.display import owner_recipient

    email, _name = owner_recipient(owner)
    return email


def _send_setup_reminder(client, token):
    """Account-setup nudge — sent once per 24h until the token is consumed."""
    from clients.emails import send_branded

    name = _setup_first_name(client)
    recipient = _recipient_email(client)
    if not recipient:
        return
    setup_url = token.get_setup_url()
    text_body = (
        f'Hi {name},\n\n'
        f'Just a reminder — your Aspired Websites account is waiting '
        f'to be set up.\n\n'
        f'Click the link below to create your password and access your '
        f'portal:\n\n{setup_url}\n\n'
        f'Work on your website cannot begin until your account is set '
        f'up and your intake form is submitted.\n\n'
        f'— Zachery Long\nAspired Websites LLC\n'
    )
    # SECURITY-SENSITIVE — contains the one-time setup token URL.
    send_branded(
        subject='Reminder: Set up your Aspired Websites account',
        template='setup_reminder',
        context={
            'first_name': name,
            'setup_url': setup_url,
            'preheader': (
                'Your account is still waiting to be set up.'),
        },
        recipient_list=[recipient],
        text_body=text_body,
        secure=True,
    )


def _send_intake_reminder(client, token):
    """Intake-form nudge — sent once per 48h until the intake is submitted."""
    from clients.emails import send_branded

    name = _setup_first_name(client)
    recipient = _recipient_email(client)
    if not recipient:
        return
    intake_url = 'https://aspiredwebsites.com/portal/intake/'
    text_body = (
        f'Hi {name},\n\n'
        f'Your account is set up — great. We still need your intake '
        f'form before we can begin building your website. It takes '
        f'about 10 minutes.\n\n'
        f'Complete your intake form:\n{intake_url}\n\n'
        f'Work on your website will not begin until this is submitted.\n\n'
        f'— Zachery Long\nAspired Websites LLC\n'
    )
    send_branded(
        subject=(
            'Action needed: Complete your intake form '
            'to start your website'),
        template='intake_reminder',
        context={
            'first_name': name,
            'intake_url': intake_url,
            'preheader': (
                'Submit your intake form so we can start building.'),
        },
        recipient_list=[recipient],
        text_body=text_body,
    )


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


@shared_task
def check_portfolio_screenshots(stale_after_days=180):
    """
    Weekly — flag portfolio screenshots that have gone stale or whose
    client site has stopped responding.

    Deliberately does NOT re-capture. Capturing needs Playwright and a
    Chromium install, which the servers do not have (and should not —
    it is ~400MB for a job that runs four times a year). Capture stays
    a workstation command; this is the part that notices it is due.

    Two failure modes, and the second is the serious one:

      * Stale — the screenshot is older than `stale_after_days`. A
        client may have redesigned since; the portfolio would be
        showing work that is no longer what is live.
      * Dead — the client's site no longer returns 200. The portfolio
        is then linking a prospect to a broken page as proof of our
        work, which is worse than showing nothing.
    """
    import requests

    from clients.models import CaseStudy
    from core.system_alerts import record_alert

    cutoff = timezone.now() - timedelta(days=stale_after_days)
    stale, dead = [], []

    for study in CaseStudy.objects.filter(
            is_published=True).exclude(live_url=''):
        try:
            resp = requests.get(
                study.live_url, timeout=20, allow_redirects=True,
                headers={'User-Agent': 'AspiredWebsites-PortfolioCheck/1.0'})
            if resp.status_code != 200:
                dead.append(f'{study.title} → HTTP {resp.status_code}')
        except requests.RequestException as exc:
            dead.append(f'{study.title} → {type(exc).__name__}')

        if not study.screenshot:
            continue
        # updated_at is the closest thing to "when the image was last
        # written" without adding a column for it; the capture command
        # saves the field, which touches the row.
        if study.updated_at and study.updated_at < cutoff:
            stale.append(study.slug)

    if dead:
        record_alert(
            'warning', 'clients.portfolio',
            f'{len(dead)} portfolio client site(s) not returning 200',
            detail=('These are linked from /portfolio/ as proof of our '
                    'work:\n' + '\n'.join(dead)))
    if stale:
        record_alert(
            'info', 'clients.portfolio',
            f'{len(stale)} portfolio screenshot(s) older than '
            f'{stale_after_days} days',
            detail=('Re-capture on a workstation:\n'
                    '  python manage.py capture_case_study_screenshots '
                    '--force\n'
                    'then copy media/portfolio/ up and run '
                    'attach_case_study_screenshots.\n\n'
                    'Stale: ' + ', '.join(stale)))

    return f'portfolio check — {len(dead)} dead, {len(stale)} stale'


@shared_task
def check_unconnected_social_channels(grace_hours=24):
    """
    Daily — alert when a client is being BILLED for social management
    but their channels have no OAuth token, so nothing can be posted.

    The launch plan framed this as a client-facing "connect your
    channels" banner. That is misaimed: `social.views.connect_page` is
    @admin_required, so the client cannot connect anything even if
    prompted. Connecting is operator work, which makes this an operator
    alert — the client's only visible symptom is silence.

    Scope, and why each bound is here:
      * status='active' only. A plan awaiting payment or already
        cancelled is not owed any posting.
      * WIRED platforms only. A channel on a platform with no OAuth
        integration built yet cannot be connected by anyone, so
        flagging it is noise the operator can do nothing about.
      * grace_hours after the plan starts. A plan bought ten minutes
        ago is not yet a problem, and alerting immediately would train
        the alert to be ignored.
    """
    from clients.service_models import SocialChannel
    from core.system_alerts import record_alert
    from social.views import WIRED_PLATFORMS

    cutoff = timezone.now() - timedelta(hours=grace_hours)
    offenders = {}

    channels = (SocialChannel.objects
                .filter(plan__status='active',
                        platform__in=WIRED_PLATFORMS)
                .select_related('plan', 'plan__account'))

    for channel in channels:
        plan = channel.plan
        started = plan.started_at or plan.created_at
        if started and started > cutoff:
            continue    # still inside the grace window
        token = getattr(channel, 'token', None)
        if token is not None and token.access_token_encrypted:
            continue    # connected
        name = getattr(plan.account, 'name', None) or str(plan.account_id)
        offenders.setdefault(name, []).append(
            f'{channel.get_platform_display()}'
            f'{" @" + channel.handle if channel.handle else ""}')

    if offenders:
        lines = [f'  {name}: {", ".join(ch)}'
                 for name, ch in sorted(offenders.items())]
        record_alert(
            'warning', 'social.oauth',
            f'{len(offenders)} paying social client(s) have unconnected '
            f'channels',
            detail=('These plans are active and billing, but the channels '
                    'have no OAuth token — nothing can be posted for '
                    'them:\n' + '\n'.join(lines) +
                    '\n\nConnect at /admin-dashboard/social/.'))

    return (f'social oauth check — {len(offenders)} client(s) with '
            f'unconnected channels')
