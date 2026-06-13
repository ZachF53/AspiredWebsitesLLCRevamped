"""Uptime aggregation helpers — uptime %, response times, daily chart data.

`scope` is either a Website (per-site, new) or a ClientProfile (legacy).
"""

from datetime import timedelta

from django.db.models import Avg
from django.utils import timezone

from .scope import scope_filter


def get_uptime_percentage(scope, days=30):
    """Uptime % over the last N days, or None if there are no checks yet."""
    from clients.models import UptimeRecord
    since = timezone.now() - timedelta(days=days)
    records = UptimeRecord.objects.filter(
        **scope_filter(scope), checked_at__gte=since)
    total = records.count()
    if total == 0:
        return None
    up = records.filter(is_up=True).count()
    return round((up / total) * 100, 2)


def get_avg_response_time(scope, days=30):
    """Average response time (ms) over the last N days, or None."""
    from clients.models import UptimeRecord
    since = timezone.now() - timedelta(days=days)
    result = UptimeRecord.objects.filter(
        **scope_filter(scope), checked_at__gte=since, is_up=True,
    ).aggregate(avg=Avg('response_time_ms'))
    avg = result['avg']
    return round(avg) if avg is not None else None


def get_uptime_chart_data(scope, days=30):
    """
    Daily uptime % + avg response time for the last N days.
    Returns a list of {date, uptime_pct, avg_response_ms}, oldest first.
    """
    from clients.models import UptimeRecord
    flt = scope_filter(scope)
    data = []
    for i in range(days):
        day = (timezone.now() - timedelta(days=i)).date()
        records = UptimeRecord.objects.filter(**flt, checked_at__date=day)
        total = records.count()
        if total == 0:
            continue
        up = records.filter(is_up=True).count()
        avg_ms = records.filter(is_up=True).aggregate(
            avg=Avg('response_time_ms'))['avg']
        data.append({
            'date': day.isoformat(),
            'uptime_pct': round((up / total) * 100, 1),
            'avg_response_ms': round(avg_ms) if avg_ms else None,
        })
    return list(reversed(data))


def get_current_status(scope):
    """The most recent check's up/down state — True, False, or None if no data."""
    from clients.models import UptimeRecord
    latest = UptimeRecord.objects.filter(**scope_filter(scope)).first()
    return latest.is_up if latest else None
