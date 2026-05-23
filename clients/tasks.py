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
