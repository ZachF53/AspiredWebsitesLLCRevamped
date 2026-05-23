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
