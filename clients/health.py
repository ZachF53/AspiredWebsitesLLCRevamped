"""
Client health-score calculator.

Score weights:
    Payment      30%   — maintenance lapses + overdue billable tickets
    Engagement   20%   — portal login recency
    NPS          20%   — latest survey score
    Uptime       20%   — 30-day uptime percentage
    Support      10%   — open ticket count
                ----
    Total       100%

`calculate_client_health(client)` returns an unsaved
`ClientHealthScore` so the Celery task can `bulk_create` later if it
ever wants to; `get_latest_health_score(client)` is a read-through
cache that recalculates if the latest stored row is older than 24h.
"""

from datetime import timedelta

from django.utils import timezone

# Maintenance package codes — kept in sync with
# ClientProfile.PACKAGE_CHOICES. When any of these is set on a
# client whose maintenance_active is False, we assume billing
# failure and zero out the payment score.
_MAINT_PACKAGES = {
    'maintenance_essentials',
    'maintenance_growth',
    'maintenance_dominant',
}


def _payment_score(client):
    """30% — penalises maintenance lapses + overdue billable tickets."""
    score = 100
    if client.package in _MAINT_PACKAGES and not client.maintenance_active:
        # Was on maintenance, isn't any more — likely Stripe failure.
        score = 0

    # Overdue billable support tickets. SupportTicket doesn't have a
    # `paid` field, so we proxy with billable=True + status in
    # (open/in_progress) older than 14 days — i.e. work the operator
    # has agreed is billable but hasn't invoiced + collected for yet.
    fortnight_ago = timezone.now() - timedelta(days=14)
    overdue = client.tickets.filter(
        billable=True,
        status__in=('open', 'in_progress'),
        created_at__lt=fortnight_ago,
    ).count()
    if overdue:
        score = max(0, score - overdue * 25)
    return score


def _engagement_score(client):
    """20% — portal login recency."""
    if not client.user:
        # No login account = no portal access. Don't penalise; the
        # client may be email-only by design (legacy seed flow).
        return 50
    last_login = client.user.last_login
    if not last_login:
        return 0
    days = (timezone.now() - last_login).days
    if days <= 30:
        return 100
    if days <= 60:
        return 75
    if days <= 90:
        return 50
    if days <= 180:
        return 25
    return 0


def _nps_score(client):
    """20% — latest NPS survey response. 50 (neutral) when no data."""
    latest = client.nps_surveys.filter(
        score__isnull=False).order_by('-sent_at').first()
    if latest is None or latest.score is None:
        return 50
    n = latest.score
    if n >= 9:
        return 100
    if n >= 7:
        return 75
    if n >= 4:
        return 25
    return 0


def _uptime_score(client):
    """20% — 30-day uptime percentage. Optimistic 75 when no data."""
    from reporting.uptime_helpers import get_uptime_percentage
    pct = get_uptime_percentage(client, days=30)
    if pct is None:
        return 75
    if pct >= 99:
        return 100
    if pct >= 95:
        return 75
    if pct >= 90:
        return 50
    return 0


def _support_score(client):
    """10% — open ticket pressure. 3+ open is critical for one client."""
    open_count = client.tickets.filter(
        status__in=('open', 'in_progress')).count()
    if open_count == 0:
        return 100
    if open_count <= 2:
        return 50
    return 0


def calculate_client_health(client):
    """
    Calculate a fresh `ClientHealthScore` for `client` (unsaved).

    Components are bounded 0-100; the weighted total is also 0-100.
    `health_status` falls out of the score band, and `churn_risk` fires
    when either the band is critical OR payments are entirely broken.
    """
    payment = _payment_score(client)
    engagement = _engagement_score(client)
    nps = _nps_score(client)
    uptime = _uptime_score(client)
    support = _support_score(client)

    score = int(
        payment * 0.30
        + engagement * 0.20
        + nps * 0.20
        + uptime * 0.20
        + support * 0.10
    )

    if score >= 70:
        health_status = 'healthy'
    elif score >= 40:
        health_status = 'at_risk'
    else:
        health_status = 'critical'

    churn_risk = (health_status == 'critical') or (payment == 0)

    from clients.models import ClientHealthScore
    return ClientHealthScore(
        client=client,
        score=score,
        payment_score=payment,
        engagement_score=engagement,
        nps_score_component=nps,
        uptime_score=uptime,
        support_score=support,
        health_status=health_status,
        churn_risk=churn_risk,
    )


def get_latest_health_score(client):
    """
    Return the most recent stored ClientHealthScore for `client`, or
    calculate + save a fresh one if none exists (or the last one is
    > 24h old). Cheap enough to call inline from the dashboard view —
    the daily Celery beat is the steady-state source of writes.
    """
    from clients.models import ClientHealthScore

    latest = ClientHealthScore.objects.filter(client=client).first()
    if latest is not None and (
            timezone.now() - latest.calculated_at) < timedelta(days=1):
        return latest

    fresh = calculate_client_health(client)
    fresh.save()
    return fresh
