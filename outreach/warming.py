"""
Outreach send-gate.

NOTE: the original domain-warming ramp (10 → 25 → 50/day until
July 1, 2026) was REMOVED on 2026-05-30 after SendGrid's 30-day
reputation analysis came back clean (92.4% delivery, 38.6% open
rate, 0% spam). The only cap that matters now is
``OutreachSettings.daily_send_cap`` — flip it in admin Settings.

This module is kept as a thin shim so the sender code doesn't need
to change. ``outreach_blocked_today`` still gates on the master
``outreach_active`` switch — that's the kill-switch if anything
ever needs to be paused fast.

To re-introduce a date-based ramp later, populate ``_RAMP_SCHEDULE``
with ``(start_date, cap)`` pairs and ``warming_cap_for`` will start
returning the active cap again.
"""

import datetime

# Empty by design — leave this list empty for "no ramp, no override".
# To resurrect a ramp, append (date, cap) tuples in chronological
# order; see git history for the original schedule.
_RAMP_SCHEDULE: list[tuple[datetime.date, int]] = []


def warming_cap_for(date):
    """
    Returns ``None`` when no ramp is configured (the current state) —
    callers interpret None as 'defer to OutreachSettings.daily_send_cap'.
    """
    if not _RAMP_SCHEDULE:
        return None
    cap = None
    for start, n in _RAMP_SCHEDULE:
        if date >= start:
            cap = n
    return cap


def effective_cap_for(date, configured_cap):
    """The lower of (ramp cap, configured cap). With ramp empty, == configured."""
    ramp = warming_cap_for(date)
    if ramp is None:
        return configured_cap
    return min(ramp, configured_cap)


def outreach_blocked_today(date=None, settings_obj=None):
    """
    Master kill-switch gate. Returns ``(blocked, reason)``.

    Blocks only when ``OutreachSettings.outreach_active=False``. The
    date-based ramp is no longer enforced (see module docstring).
    """
    from outreach.models import OutreachSettings

    if settings_obj is None:
        settings_obj = OutreachSettings.load()

    if not settings_obj.outreach_active:
        return True, 'Master switch off — flip OutreachSettings.outreach_active to enable.'

    return False, ''
