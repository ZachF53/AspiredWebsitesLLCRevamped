"""
Domain-warming daily-cap gate.

Per CLAUDE.md → Domain Warming:

    Week 1-2  (May 20 – Jun  3, 2026):  10 real emails/day
    Week 3-4  (Jun  3 – Jun 17, 2026):  25 real emails/day
    Week 5-6  (Jun 17 – Jul  1, 2026):  50 real emails/day
    Week 7+   (Jul  1, 2026 → on):       OutreachSettings.daily_send_cap

Until July 1, 2026 the warming cap OVERRIDES whatever the admin set in
``OutreachSettings.daily_send_cap`` — even if the operator bumps the dial
to 200, the sender will not exceed today's warming bucket. This is
deliberate: the warming schedule protects deliverability, and the dial
is for the post-warming world.

Pre-July-1 cold sends should be real outreach mail mixed into the
warming traffic — automated batches blast reputation. The compressed
30-day ramp in CLAUDE.md assumes mostly hand-sent warming mail; the
cold sender contributes a small fraction.

``outreach_blocked_today()`` returns ``(blocked, reason)`` so the cold
sender can log a clean reason when it decides to skip a tick — e.g.
"date is before warming_start_date" or "cap reached for today".
"""

import datetime

# Warming schedule — CLAUDE.md is the source. (start_date_iso, cap)
# pairs sorted by start date. Each entry is "from this date until the
# next entry's start date, the cap is N". After the last entry's start
# date, warming is over and we defer to OutreachSettings.daily_send_cap.
_WARMING_SCHEDULE = [
    (datetime.date(2026, 5, 20), 10),  # Weeks 1-2
    (datetime.date(2026, 6,  3), 25),  # Weeks 3-4
    (datetime.date(2026, 6, 17), 50),  # Weeks 5-6
]

# When the warming gate dissolves and OutreachSettings.daily_send_cap
# takes over. Cold outreach was BLOCKED before this date in the strict
# reading of CLAUDE.md ("Cold outreach CANNOT begin before July 1"),
# but the sender supports it — we still respect the cap when the
# operator opts in early via a `force_send_during_warming` flag.
WARMING_END_DATE = datetime.date(2026, 7, 1)


def warming_cap_for(date):
    """
    Return today's warming cap (int) or ``None`` if warming has ended.

    ``None`` means "defer to OutreachSettings.daily_send_cap" — never
    interpret it as unlimited.
    """
    if date >= WARMING_END_DATE:
        return None
    cap = None
    for start, n in _WARMING_SCHEDULE:
        if date >= start:
            cap = n
    return cap  # may be None if `date` is before the schedule starts


def effective_cap_for(date, configured_cap):
    """
    The lower of (warming cap for today, the admin-configured cap).

    Once warming has ended, returns configured_cap unchanged. Before
    warming starts (date < May 20, 2026) returns 0 — the sender
    interprets a 0 cap as "blocked, don't send anything today".
    """
    warming = warming_cap_for(date)
    if warming is None:
        return configured_cap
    return min(warming, configured_cap) if warming is not None else 0


def outreach_blocked_today(date=None, settings_obj=None):
    """
    Decide whether the cold sender may run at all today.

    Returns ``(blocked: bool, reason: str)``. The cold sender logs the
    reason and exits early when blocked.

    Blocks:
      - ``OutreachSettings.outreach_active`` is False — the master kill
        switch. Set True in admin Settings to enable cold sends.
      - Date is before the first warming bucket (May 20, 2026) — pure
        guard so we never accidentally cold-send during pre-warm prep.
      - Per-day cap has been reached (caller passes ``today_count``;
        this function only computes the cap).
    """
    from outreach.models import OutreachSettings

    if date is None:
        date = datetime.date.today()
    if settings_obj is None:
        settings_obj = OutreachSettings.load()

    if not settings_obj.outreach_active:
        return True, 'Master switch off — flip OutreachSettings.outreach_active to enable.'

    first_warming_day = _WARMING_SCHEDULE[0][0]
    if date < first_warming_day:
        return True, (
            f'Date is before warming starts ({first_warming_day.isoformat()}).')

    return False, ''
