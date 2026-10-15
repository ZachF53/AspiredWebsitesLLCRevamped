"""
Dashboard numbers: how many requests went out, how many reviews came back.

Computed live from ``ReviewEvent`` rather than kept in a rollup table.
Volumes are in the hundreds per month per client, the composite index
``(program, event_type, occurred_at)`` covers every query here, and a
snapshot table would introduce a staleness bug for no measurable gain.

Two shapes:

* **Monthly series** — one row per calendar month, for the table and the
  bar chart.
* **Windows** — last 30, 60 and 90 days, plus year-to-date from 1
  January, as requested.
"""

from datetime import timedelta

from django.db.models import Count, Q
from django.db.models.functions import TruncMonth
from django.utils import timezone

# Events that mean "a request was actually sent". 'delivered' is a
# stronger signal but not every workflow reports it, so counting both
# would double-count the ones that do. 'sent' is the spine.
SENT_TYPES = ('sent',)
REVIEW_TYPES = ('review_left',)


def _counts(qs):
    """One query, four numbers."""
    return qs.aggregate(
        sent=Count('id', filter=Q(event_type__in=SENT_TYPES)),
        reviews=Count('id', filter=Q(event_type__in=REVIEW_TYPES)),
        clicks=Count('id', filter=Q(event_type='click')),
        replies=Count('id', filter=Q(event_type='reply')),
        opt_outs=Count('id', filter=Q(event_type='opt_out')),
    )


def _rate(part, whole):
    if not whole:
        return None
    return round((part / whole) * 100)


def window_stats(program, *, now=None):
    """``{'d30': {...}, 'd60': {...}, 'd90': {...}, 'ytd': {...}}``.

    Each entry carries sent / reviews / clicks / replies / opt_outs plus
    a ``review_rate`` (reviews as a percentage of requests sent), which
    is the number a contractor actually cares about.
    """
    if program is None:
        return {}
    now = now or timezone.now()
    base = program.events.all()

    spans = {
        'd30': now - timedelta(days=30),
        'd60': now - timedelta(days=60),
        'd90': now - timedelta(days=90),
        # Year-to-date means from 1 January of the current year, not the
        # trailing 365 days.
        'ytd': now.replace(month=1, day=1, hour=0, minute=0, second=0,
                           microsecond=0),
    }
    labels = {
        'd30': 'Last 30 days',
        'd60': 'Last 60 days',
        'd90': 'Last 90 days',
        'ytd': f'{now.year} to date',
    }

    out = {}
    for key, since in spans.items():
        stats = _counts(base.filter(occurred_at__gte=since))
        stats['label'] = labels[key]
        stats['review_rate'] = _rate(stats['reviews'], stats['sent'])
        stats['click_rate'] = _rate(stats['clicks'], stats['sent'])
        out[key] = stats
    return out


def monthly_series(program, *, months=12, now=None):
    """Newest-last list of ``{month, label, sent, reviews, review_rate}``.

    Months with no activity are included as zeros so the chart has no
    gaps and the table reads as a continuous record.
    """
    if program is None:
        return []
    now = now or timezone.now()

    # Walk back `months` calendar months from the first of this month.
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _ in range(months - 1):
        start = (start - timedelta(days=1)).replace(day=1)

    rows = (program.events
            .filter(occurred_at__gte=start,
                    event_type__in=SENT_TYPES + REVIEW_TYPES)
            .annotate(month=TruncMonth('occurred_at'))
            .values('month')
            .annotate(
                sent=Count('id', filter=Q(event_type__in=SENT_TYPES)),
                reviews=Count('id', filter=Q(event_type__in=REVIEW_TYPES)),
            ))
    by_month = {}
    for row in rows:
        month = row['month']
        if month is None:
            continue
        by_month[(month.year, month.month)] = row

    series = []
    cursor = start
    while cursor <= now:
        row = by_month.get((cursor.year, cursor.month), {})
        sent = row.get('sent', 0)
        reviews = row.get('reviews', 0)
        series.append({
            'month': cursor.date(),
            'label': cursor.strftime('%b %Y'),
            'sent': sent,
            'reviews': reviews,
            'review_rate': _rate(reviews, sent),
        })
        # First of the next month.
        cursor = (cursor.replace(day=28) + timedelta(days=7)).replace(day=1)
    return series


def chart_bars(series):
    """Add a 0-100 ``pct`` to each row so the template can draw bars with
    plain CSS widths and no charting library.

    Scaled to the largest ``sent`` in the window, which keeps the two
    series comparable within a month — the question being answered is
    "of the requests that went out, how many became reviews".
    """
    peak = max([row['sent'] for row in series] + [1])
    out = []
    for row in series:
        out.append({
            **row,
            'sent_pct': round(row['sent'] / peak * 100),
            'reviews_pct': round(row['reviews'] / peak * 100),
        })
    return out
