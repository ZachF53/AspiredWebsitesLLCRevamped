"""
Google Calendar push — minimal HTTP-based integration.

The full OAuth flow + token refresh is out of scope for the initial
build (no Google API client library is required at this layer; we use
direct REST so the dependency surface stays narrow). For now this
module provides the shape of the integration:

    - push_event_for_call(call) → creates a Google Calendar event for
      the confirmed ScheduledCall, stores the event id back.
    - cancel_event_for_call(call) → deletes the event when the call
      cancels.

If no GoogleCalendarToken row exists for an admin yet, both functions
no-op safely. Wiring the OAuth dance is a follow-up task.
"""

import logging

logger = logging.getLogger(__name__)


def push_event_for_call(call):
    """Create a Google Calendar event for `call`. Best-effort."""
    from .models import GoogleCalendarToken

    token = GoogleCalendarToken.objects.first()
    if token is None:
        logger.info(
            'google_calendar: no admin token connected; skipping push '
            'for call %s', call.pk)
        return

    # Real implementation goes here. The shape would be:
    #   import requests
    #   r = requests.post(
    #       f'https://www.googleapis.com/calendar/v3/calendars/'
    #       f'{token.calendar_id}/events',
    #       headers={'Authorization': f'Bearer {token.access_token}'},
    #       json={
    #           'summary': f'Kickoff call — {call.customer_name}',
    #           'description': call.notes,
    #           'start': {'dateTime': call.starts_at.isoformat()},
    #           'end':   {'dateTime': call.ends_at.isoformat()},
    #           'attendees': [{'email': call.customer_email}],
    #       },
    #   )
    # Then store r.json()['id'] in call.google_event_id.
    logger.info(
        'google_calendar: would push event for call %s '
        '(implementation pending)', call.pk)


def cancel_event_for_call(call):
    """Delete the Google Calendar event for `call`. Best-effort."""
    if not call.google_event_id:
        return
    from .models import GoogleCalendarToken

    token = GoogleCalendarToken.objects.first()
    if token is None:
        return
    logger.info(
        'google_calendar: would delete event %s for call %s',
        call.google_event_id, call.pk)
