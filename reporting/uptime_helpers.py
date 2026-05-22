"""Uptime aggregation helpers — uptime %, response times, daily chart data."""

from datetime import timedelta

from django.db.models import Avg
from django.utils import timezone


def get_uptime_percentage(client, days=30):
    """Uptime % over the last N days, or None if there are no checks yet."""
    from clients.models import UptimeRecord
    since = timezone.now() - timedelta(days=days)
    records = UptimeRecord.objects.filter(client=client, checked_at__gte=since)
    total = records.count()
    if total == 0:
        return None
    up = records.filter(is_up=True).count()
    return round((up / total) * 100, 2)


def get_avg_response_time(client, days=30):
    """Average response time (ms) over the last N days, or None."""
    from clients.models import UptimeRecord
    since = timezone.now() - timedelta(days=days)
    result = UptimeRecord.objects.filter(
        client=client, checked_at__gte=since, is_up=True,
    ).aggregate(avg=Avg('response_time_ms'))
    avg = result['avg']
    return round(avg) if avg is not None else None


def get_uptime_chart_data(client, days=30):
    """
    Daily uptime % + avg response time for the last N days.
    Returns a list of {date, uptime_pct, avg_response_ms}, oldest first.
    """
    from clients.models import UptimeRecord
    data = []
    for i in range(days):
        day = (timezone.now() - timedelta(days=i)).date()
        records = UptimeRecord.objects.filter(client=client, checked_at__date=day)
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


def get_current_status(client):
    """The most recent check's up/down state — True, False, or None if no data."""
    from clients.models import UptimeRecord
    latest = UptimeRecord.objects.filter(client=client).first()
    return latest.is_up if latest else None
