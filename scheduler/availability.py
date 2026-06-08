"""
Slot enumeration — given a date range, produce all 30-minute slots
that fall within an AvailabilityWindow AND are not already booked.

Honours the timezone on each AvailabilityWindow row (defaults to
America/New_York per spec).
"""

import datetime as _dt

from django.utils import timezone

from .models import AvailabilityWindow, ScheduledCall


SLOT_MINUTES = 30


def _to_local_dt(d, t, tz_name):
    """Combine a date+time with a tz name → tz-aware datetime."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return _dt.datetime.combine(d, t).replace(tzinfo=tz)


def enumerate_slots(start_date, end_date):
    """
    Yield (utc_start, utc_end) tuples for every 30-minute slot within
    every active AvailabilityWindow between start_date and end_date,
    excluding any slot that overlaps a non-cancelled ScheduledCall.
    """
    windows = list(AvailabilityWindow.objects.filter(active=True))
    if not windows:
        return

    busy = set()
    for sc in ScheduledCall.objects.filter(
            starts_at__date__gte=start_date,
            starts_at__date__lte=end_date,
        ).exclude(status='cancelled'):
        busy.add(sc.starts_at)

    d = start_date
    while d <= end_date:
        dow = d.weekday()
        for w in windows:
            if w.day_of_week != dow:
                continue
            cursor = _to_local_dt(d, w.start_time, w.timezone)
            window_end = _to_local_dt(d, w.end_time, w.timezone)
            while cursor + _dt.timedelta(minutes=SLOT_MINUTES) <= window_end:
                if cursor not in busy and cursor > timezone.now():
                    yield (
                        cursor.astimezone(_dt.timezone.utc),
                        (cursor + _dt.timedelta(minutes=SLOT_MINUTES))
                        .astimezone(_dt.timezone.utc),
                    )
                cursor += _dt.timedelta(minutes=SLOT_MINUTES)
        d += _dt.timedelta(days=1)
