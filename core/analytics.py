"""
Server-queued analytics events (Master Plan §10, MEASUREMENT_SPEC §5).

Three of the required conversions — a contact-form submit, an audit run,
and the audit email capture — are only *true* on the server. The forms
all use POST-redirect-GET, so firing them from a client-side `submit`
listener would be wrong in both directions: it would count submissions
that failed validation or were silently binned as spam, and it would
race the redirect that follows.

So the view queues the event on the session, and the next full page
render (the thanks / results page the redirect lands on) emits it. That
makes "a conversion happened" a server-side fact.

Events are popped when read, so a refresh or a back-button cannot
double-count a single conversion.

PII rule (§5.3 / MEASUREMENT_SPEC): never queue an email address, name,
or phone number. Category values and page paths only. `queue_event`
refuses to carry a value that looks like an email or a phone number
rather than trusting every call site to remember.
"""

import re

SESSION_KEY = 'pending_analytics_events'

# Hard cap — a session that somehow accumulates events (a redirect loop,
# a bot replaying POSTs) must not grow unbounded or blow up the page it
# eventually renders into.
MAX_QUEUED = 10

_EMAIL_RE = re.compile(r'[^@\s]+@[^@\s]+\.[^@\s]+')
_PHONE_RE = re.compile(r'(?:\+?\d[\s().-]*){9,}')


class PIIInEventError(ValueError):
    """Raised when a param value looks like personal data."""


def _check(name, value):
    if not isinstance(value, str):
        return value
    if _EMAIL_RE.search(value) or _PHONE_RE.search(value):
        raise PIIInEventError(
            f'Analytics param {name!r} looks like PII and was refused. '
            f'Send a category or a path, never a visitor identifier.')
    return value


def queue_event(request, name, **params):
    """
    Queue one GA4 event to be emitted on the next full page render.

    Silently does nothing if there is no session (a rare path, but this
    must never be the reason a form submit 500s).
    """
    session = getattr(request, 'session', None)
    if session is None:
        return
    clean = {k: _check(k, v) for k, v in params.items() if v not in (None, '')}
    queued = session.get(SESSION_KEY) or []
    if len(queued) >= MAX_QUEUED:
        return
    queued.append({'name': name, 'params': clean})
    session[SESSION_KEY] = queued
    # The queue lives in the session dict, mutated in place — Django
    # only auto-saves on assignment, which the line above provides, but
    # be explicit so a session backend with change-detection off still
    # persists it across the redirect.
    session.modified = True


def pop_events(request):
    """Return and clear the queued events. Safe to call on any request."""
    session = getattr(request, 'session', None)
    if session is None:
        return []
    events = session.get(SESSION_KEY)
    if not events:
        return []
    del session[SESSION_KEY]
    session.modified = True
    return events
