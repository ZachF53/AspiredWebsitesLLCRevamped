"""
Slot enumeration — given a date range, produce all 30-minute slots
that fall within an AvailabilityWindow AND are not already booked.

Honours the timezone on each AvailabilityWindow row (defaults to
America/New_York per spec).
"""

import datetime as _dt

from django.utils import timezone

from .models import AvailabilityWindow, ScheduledCall


# Customer-facing slot granularity — what they pick on /design/schedule/.
SLOT_MINUTES = 30
# How much time a single booking actually consumes on the calendar.
# We block 2 hours so the operator gets buffer either side of the
# call and isn't booked back-to-back.
BLOCK_MINUTES = 120
# Minimum lead time between "now" and the earliest slot we offer.
# Keeps last-minute bookings out — if someone hits the page at 3:00 PM
# and the lead time is 2 hours, the earliest visible slot is 5:00 PM.
MIN_LEAD_MINUTES = 120


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

    "Overlap" — not "exact start match" — because each ScheduledCall
    holds a 2-hour block on the calendar (see BLOCK_MINUTES). A 30-min
    slot starting at 4:30 PM overlaps a 4:00-6:00 PM block and must be
    excluded.
    """
    windows = list(AvailabilityWindow.objects.filter(active=True))
    if not windows:
        return

    # Pull busy intervals once. Widen the date filter by a day each
    # side — a block that starts late in the local day can end on the
    # next UTC day.
    busy_intervals = []
    for sc in ScheduledCall.objects.filter(
            starts_at__date__gte=start_date - _dt.timedelta(days=1),
            starts_at__date__lte=end_date + _dt.timedelta(days=1),
        ).exclude(status='cancelled'):
        if sc.starts_at and sc.ends_at:
            busy_intervals.append(
                (sc.starts_at.astimezone(_dt.timezone.utc),
                 sc.ends_at.astimezone(_dt.timezone.utc)))

    def _overlaps_busy(slot_start_utc, slot_end_utc):
        for b_start, b_end in busy_intervals:
            if slot_start_utc < b_end and slot_end_utc > b_start:
                return True
        return False

    earliest = timezone.now() + _dt.timedelta(minutes=MIN_LEAD_MINUTES)
    d = start_date
    while d <= end_date:
        dow = d.weekday()
        for w in windows:
            if w.day_of_week != dow:
                continue
            cursor = _to_local_dt(d, w.start_time, w.timezone)
            window_end = _to_local_dt(d, w.end_time, w.timezone)
            while cursor + _dt.timedelta(minutes=SLOT_MINUTES) <= window_end:
                slot_start_utc = cursor.astimezone(_dt.timezone.utc)
                slot_end_utc = (
                    cursor + _dt.timedelta(minutes=SLOT_MINUTES)
                ).astimezone(_dt.timezone.utc)
                if slot_start_utc >= earliest and not _overlaps_busy(
                        slot_start_utc, slot_end_utc):
                    yield (slot_start_utc, slot_end_utc)
                cursor += _dt.timedelta(minutes=SLOT_MINUTES)
        d += _dt.timedelta(days=1)
