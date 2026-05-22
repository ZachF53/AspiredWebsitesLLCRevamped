"""Conversion-event aggregation helpers — shared by admin and portal views."""

import calendar

from django.utils import timezone


def _month_bounds(now):
    """(this_month_start, last_month_start) as aware datetimes."""
    this_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_start = this_start.replace(year=this_start.year - 1, month=12) \
        if this_start.month == 1 \
        else this_start.replace(month=this_start.month - 1)
    return this_start, last_start


def conversion_counts(client):
    """
    Per event type: this-month vs last-month counts and the delta.
    Returns a list of {type, label, this_month, last_month, delta}.
    """
    from .models import ConversionEvent

    now = timezone.now()
    this_start, last_start = _month_bounds(now)

    rows = []
    for value, label in ConversionEvent.EVENT_TYPE_CHOICES:
        base = ConversionEvent.objects.filter(client=client, event_type=value)
        this_n = base.filter(event_timestamp__gte=this_start).count()
        last_n = base.filter(
            event_timestamp__gte=last_start,
            event_timestamp__lt=this_start,
        ).count()
        rows.append({
            'type': value,
            'label': label,
            'this_month': this_n,
            'last_month': last_n,
            'delta': this_n - last_n,
        })
    return rows


def conversion_6month_chart(client):
    """
    Form-submission counts for the last 6 calendar months (oldest first),
    each with a 0-100 bar height for the CSS chart.
    """
    from .models import ConversionEvent

    now = timezone.now()
    ranges = []
    year, month = now.year, now.month
    for i in range(5, -1, -1):
        m, y = month - i, year
        while m <= 0:
            m += 12
            y -= 1
        ranges.append((y, m))

    counts = []
    for (y, m) in ranges:
        n = ConversionEvent.objects.filter(
            client=client, event_type='form_submit',
            event_timestamp__year=y, event_timestamp__month=m,
        ).count()
        counts.append({'year': y, 'month': m, 'count': n})

    max_n = max((c['count'] for c in counts), default=0) or 1
    for c in counts:
        c['label'] = calendar.month_abbr[c['month']]
        c['bar_h'] = round(c['count'] / max_n * 100)
    return counts
