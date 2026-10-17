"""
Business hours, and turning them into a GHL Voice AI working-hours spec.

GHL's model is "All Hours" or a custom schedule of windows in which the
agent **is active**. That is a different question from the one the client
answers at intake, and the mismatch is the reason this module exists:

  * ``business_hours_only`` — agent active == business hours. Direct.
  * ``after_hours_only``    — agent active == the COMPLEMENT of business
                              hours. Has to be computed.
  * ``all_hours``           — no schedule at all.

The complement is where the bugs live. Monday 08:00-17:00 open means the
agent covers Monday 00:00-08:00 and 17:00-24:00, and a day with no
business hours at all (Sunday, for most HVAC shops) means the agent
covers the whole day. Getting that backwards would point the receptionist
at exactly the hours the owner is already answering the phone.

## Timezone

GHL's working-hours UI has no timezone field, so the times we push are
interpreted in whatever zone the sub-account is set to. We therefore keep
the client's zone on ``VoiceProgram.timezone`` and compute against it.
Texas (Central) and Georgia (Eastern) clients both exist, so a
project-default assumption would be wrong for one of them.
"""

import logging
from datetime import time, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MIDNIGHT = time(0, 0)
END_OF_DAY = time(23, 59, 59)

_DAY_NAMES = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday',
              'saturday', 'sunday']


def program_tzinfo(program):
    """The client's zone, falling back to Central rather than raising.

    A bad zone string should degrade to a plausible schedule, not take
    the agent offline.
    """
    name = (getattr(program, 'timezone', '') or 'America/Chicago').strip()
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning('voice.schedule: unknown timezone %r on program %s',
                       name, getattr(program, 'pk', None))
        return ZoneInfo('America/Chicago')


def business_windows(program):
    """``{day_index: [(start, end), ...]}`` from the stored hours."""
    out = {}
    for window in program.hours.all():
        out.setdefault(window.day_of_week, []).append(
            (window.start_time, window.end_time))
    for day in out:
        out[day].sort()
    return out


def _invert_day(windows):
    """The gaps around a day's open windows.

    An empty day inverts to the whole day — a shop closed on Sunday wants
    the receptionist covering all of Sunday, which is the single most
    valuable slot for a no-heat call.
    """
    if not windows:
        return [(MIDNIGHT, END_OF_DAY)]
    gaps = []
    cursor = MIDNIGHT
    for start, end in sorted(windows):
        if start > cursor:
            gaps.append((cursor, start))
        if end > cursor:
            cursor = end
    if cursor < END_OF_DAY:
        gaps.append((cursor, END_OF_DAY))
    return gaps


def agent_windows(program):
    """When the agent should be ACTIVE, as ``{day_index: [(start, end)]}``.

    This is what gets pushed to GHL. ``all_hours`` returns {} meaning
    "use All Hours", which is distinct from {} meaning "never" — callers
    must check ``coverage_mode`` rather than treating an empty dict as
    silence. ``ghl_working_hours`` below does that for them.
    """
    mode = program.coverage_mode
    if mode == 'all_hours':
        return {}
    business = business_windows(program)
    if mode == 'business_hours_only':
        return {d: list(w) for d, w in business.items()}
    if mode == 'after_hours_only':
        return {day: _invert_day(business.get(day, []))
                for day in range(7)}
    # 'custom' — the stored windows already describe agent coverage
    # directly rather than business hours.
    return {d: list(w) for d, w in business.items()}


def ghl_working_hours(program):
    """The payload for GHL's Phone & Availability tab.

    ``{'mode': 'all_hours'}`` or ``{'mode': 'custom', 'days': {...}}``
    with ``HH:MM`` strings, plus the timezone so whoever applies it (API
    or operator) can set the sub-account correctly.

    Windows that cross midnight are split rather than emitted with
    end <= start. GHL tolerates a crossing window in its own UI, but a
    computed one is almost always a bug in our inversion, and a split
    pair is unambiguous either way.
    """
    mode = program.coverage_mode
    tz_name = (getattr(program, 'timezone', '') or 'America/Chicago')
    if mode == 'all_hours':
        return {'mode': 'all_hours', 'timezone': tz_name}

    days = {}
    for day, windows in agent_windows(program).items():
        emitted = []
        for start, end in windows:
            if end <= start:
                emitted.append((start.strftime('%H:%M'), '23:59'))
                emitted.append(('00:00', end.strftime('%H:%M')))
            else:
                emitted.append((start.strftime('%H:%M'),
                                end.strftime('%H:%M')))
        if emitted:
            days[_DAY_NAMES[day]] = [
                {'start': s, 'end': e} for s, e in emitted]
    return {'mode': 'custom', 'timezone': tz_name, 'days': days}


def is_after_hours(program, moment):
    """Was `moment` outside the client's business hours?

    Used to label call records, so the portal can show "14 of your 31
    calls came in after you closed" — which is the single clearest way to
    show a sceptical owner what they were missing.

    A program with no business hours recorded reports False rather than
    True: with nothing to compare against, claiming every call was
    after-hours would overstate the product's value.
    """
    business = business_windows(program)
    if not business:
        return False
    local = moment.astimezone(program_tzinfo(program))
    for start, end in business.get(local.weekday(), []):
        if end <= start:  # crosses midnight
            if local.time() >= start or local.time() < end:
                return False
        elif start <= local.time() < end:
            return False
    return True


def default_windows_weekdays(start=time(8, 0), end=time(17, 0)):
    """Mon-Fri 08:00-17:00 — the shape most HVAC shops confirm unchanged.

    Offered as the intake pre-fill so a client who just wants after-hours
    cover does not have to type ten times to tell us something ordinary.
    """
    return [(day, start, end) for day in range(5)]


def next_transition(program, moment):
    """When the agent's active state next changes, or None for 24/7.

    Powers the portal's "answering until 8:00am" line. Scans forward a
    week in 15-minute steps — coarse enough to be cheap, fine enough that
    no realistic business-hours boundary is missed, and bounded so a
    pathological schedule cannot loop forever.
    """
    if program.coverage_mode == 'all_hours':
        return None
    current = is_after_hours(program, moment)
    step = timedelta(minutes=15)
    cursor = moment
    for _ in range(4 * 24 * 7):
        cursor += step
        if is_after_hours(program, cursor) != current:
            return cursor
    return None
