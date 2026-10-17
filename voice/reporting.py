"""
Call stats for the portal and the admin queue.

Mirrors ``reviews/reporting.py`` deliberately — same window shape, same
zero-filled monthly series, same pure-CSS bar scaling — so the two
products' dashboards read as one system rather than two.

What is different is what gets counted. The review system's headline is
a rate (requests sent, reviews landed). Here the headline is **calls the
owner did not have to take**, because that is the thing a sceptical HVAC
owner is actually buying. So the tiles lead with answered, after-hours
and spam-screened, and minutes are shown separately as a budget rather
than mixed in with the value metrics.

``trial_call=True`` rows are excluded from every figure here. They are
the operator's own test calls; including them would inflate a brand-new
client's first week with calls that never happened.
"""

from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone


def _base_queryset(program):
    return program.calls.filter(trial_call=False)


def _window(program, since):
    qs = _base_queryset(program).filter(started_at__gte=since)
    agg = qs.aggregate(
        calls=Count('id'),
        seconds=Sum('duration_seconds'),
        transferred=Count('id', filter=Q(transferred=True)),
        after_hours=Count('id', filter=Q(after_hours=True)),
        booked=Count('id', filter=Q(booked_appointment=True)),
    )
    calls = agg['calls'] or 0
    seconds = agg['seconds'] or 0
    transferred = agg['transferred'] or 0
    return {
        'calls': calls,
        'minutes': round(seconds / 60) if seconds else 0,
        'transferred': transferred,
        'after_hours': agg['after_hours'] or 0,
        'booked': agg['booked'] or 0,
        # The number that sells the product at renewal: calls it finished
        # without the owner's phone ever ringing.
        'handled_alone': max(0, calls - transferred),
        'transfer_rate': (
            round(100 * transferred / calls) if calls else 0),
        'after_hours_rate': (
            round(100 * (agg['after_hours'] or 0) / calls) if calls else 0),
        'avg_seconds': round(seconds / calls) if calls else 0,
    }


def window_stats(program):
    """30 / 60 / 90 day and year-to-date tiles.

    YTD is 1 January, not a trailing 365 days — a client asking "how many
    calls this year" means the calendar year. Same choice as
    ``reviews.reporting``.
    """
    now = timezone.now()
    jan_first = now.replace(month=1, day=1, hour=0, minute=0, second=0,
                            microsecond=0)
    return [
        {'label': 'Last 30 days', 'key': '30',
         **_window(program, now - timedelta(days=30))},
        {'label': 'Last 60 days', 'key': '60',
         **_window(program, now - timedelta(days=60))},
        {'label': 'Last 90 days', 'key': '90',
         **_window(program, now - timedelta(days=90))},
        {'label': 'This year', 'key': 'ytd',
         **_window(program, jan_first)},
    ]


def monthly_series(program, months=12):
    """Zero-filled calendar months, oldest first.

    Zero-filled on purpose: a gap in the chart reads as "no data" and a
    zero reads as "no calls", and for a product whose value is answering
    the phone those are very different claims.
    """
    now = timezone.now()
    cursor = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    buckets = []
    for _ in range(months):
        buckets.append(cursor)
        cursor = (cursor.replace(year=cursor.year - 1, month=12)
                  if cursor.month == 1
                  else cursor.replace(month=cursor.month - 1))
    buckets.reverse()

    series = []
    for index, start in enumerate(buckets):
        end = (buckets[index + 1] if index + 1 < len(buckets)
               else start.replace(year=start.year + 1, month=1)
               if start.month == 12 else start.replace(month=start.month + 1))
        agg = (_base_queryset(program)
               .filter(started_at__gte=start, started_at__lt=end)
               .aggregate(
                   calls=Count('id'),
                   after_hours=Count('id', filter=Q(after_hours=True)),
                   seconds=Sum('duration_seconds')))
        series.append({
            'label': f'{start:%b}',
            'year': start.year,
            'calls': agg['calls'] or 0,
            'after_hours': agg['after_hours'] or 0,
            'minutes': round((agg['seconds'] or 0) / 60),
        })
    return series


def chart_bars(series):
    """Add percentage heights so the chart is pure CSS.

    No charting library — same constraint as the review chart, and the
    same reason: this project ships hand-written CSS and a 90KB chart
    dependency for twelve bars is not a trade worth making.
    """
    peak = max([row['calls'] for row in series] or [0]) or 1
    for row in series:
        row['calls_pct'] = round(100 * row['calls'] / peak)
        row['after_hours_pct'] = round(100 * row['after_hours'] / peak)
    return series


def recent_calls(program, limit=25):
    return _base_queryset(program).select_related(None)[:limit]


def lifetime_totals(program):
    """Headline numbers for the admin queue row."""
    agg = _base_queryset(program).aggregate(
        calls=Count('id'),
        seconds=Sum('duration_seconds'),
        transferred=Count('id', filter=Q(transferred=True)),
        after_hours=Count('id', filter=Q(after_hours=True)))
    return {
        'calls': agg['calls'] or 0,
        'minutes': round((agg['seconds'] or 0) / 60),
        'transferred': agg['transferred'] or 0,
        'after_hours': agg['after_hours'] or 0,
    }
