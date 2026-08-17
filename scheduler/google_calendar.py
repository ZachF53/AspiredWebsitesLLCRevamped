"""
Google Calendar push — REST integration using `requests`.

Called from ``scheduler/views.py:confirm_slot`` when a ScheduledCall
flips to ``confirmed``. We:

    1. Look up an admin's GoogleCalendarToken
    2. Refresh the access token if expired
    3. POST to calendars/{calendar_id}/events with the call details
    4. Store the returned event id on ScheduledCall.google_event_id

Errors are logged + written to SystemAlert so they surface on the
admin dashboard. The confirm_slot view catches our exceptions so a
Calendar API failure never blocks the customer's "booked" response.
"""

import datetime as _dt
import logging

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


TOKEN_REFRESH_URL = 'https://oauth2.googleapis.com/token'
CALENDAR_API_BASE = 'https://www.googleapis.com/calendar/v3'


def _alert(severity, source, message, detail=''):
    """Best-effort SystemAlert write."""
    try:
        from core.system_alerts import record_alert
        record_alert(severity=severity, source=source,
                     message=message, detail=detail)
    except Exception:
        pass


def _refresh_if_needed(token):
    """If the access token is expired (or about to be), refresh it
    in place. Updates the GoogleCalendarToken row."""
    if token.expires_at and token.expires_at > timezone.now() + _dt.timedelta(seconds=30):
        return token  # still valid
    if not token.refresh_token:
        raise RuntimeError('no refresh_token on file — admin must re-connect')

    r = requests.post(TOKEN_REFRESH_URL, data={
        'client_id':     getattr(settings, 'GOOGLE_CLIENT_ID', ''),
        'client_secret': getattr(settings, 'GOOGLE_CLIENT_SECRET', ''),
        'refresh_token': token.refresh_token,
        'grant_type':    'refresh_token',
    }, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(
            f'token refresh failed: {r.status_code} {r.text[:300]}')
    payload = r.json()
    token.access_token = payload['access_token']
    expires_in = int(payload.get('expires_in') or 3600)
    token.expires_at = timezone.now() + _dt.timedelta(seconds=expires_in - 60)
    token.save(update_fields=['access_token', 'expires_at', 'updated_at'])
    return token


def push_event_for_call(call):
    """Create a Google Calendar event for `call`. Returns event id or None."""
    from .models import GoogleCalendarToken

    token = GoogleCalendarToken.objects.first()
    if token is None:
        logger.info(
            'google_calendar: no admin token connected; skipping push '
            'for call %s', call.pk)
        return None

    try:
        token = _refresh_if_needed(token)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'google_calendar: token refresh failed for call %s', call.pk)
        _alert(
            severity='error',
            source='scheduler.google_calendar',
            message='Google Calendar token refresh failed — re-connect required',
            detail=str(exc)[:1000],
        )
        return None

    body = {
        'summary': f'Strategy call — {call.customer_name or "(no name)"}',
        'description': (
            f'Customer: {call.customer_name or ""}\n'
            f'Email: {call.customer_email or ""}\n\n'
            f'What they want to build:\n{call.notes or "(none provided)"}'
        ),
        'start': {'dateTime': call.starts_at.isoformat()},
        'end':   {'dateTime': call.ends_at.isoformat()},
    }
    if call.customer_email:
        body['attendees'] = [{'email': call.customer_email}]

    try:
        r = requests.post(
            f'{CALENDAR_API_BASE}/calendars/{token.calendar_id}/events'
            f'?sendUpdates=all',
            headers={
                'Authorization': f'Bearer {token.access_token}',
                'Content-Type': 'application/json',
            },
            json=body,
            timeout=15,
        )
        if r.status_code not in (200, 201):
            raise RuntimeError(
                f'event create failed: {r.status_code} {r.text[:300]}')
        data = r.json()
        event_id = data.get('id') or ''
        if event_id:
            call.google_event_id = event_id
            call.save(update_fields=['google_event_id'])
        logger.info(
            'google_calendar: pushed event %s for call %s',
            event_id, call.pk)
        return event_id
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'google_calendar: push failed for call %s', call.pk)
        _alert(
            severity='error',
            source='scheduler.google_calendar',
            message=f'Google Calendar event create failed for call {call.pk}',
            detail=str(exc)[:1000],
        )
        return None


def cancel_event_for_call(call):
    """Delete the Google Calendar event for `call`. Best-effort."""
    if not call.google_event_id:
        return
    from .models import GoogleCalendarToken
    token = GoogleCalendarToken.objects.first()
    if token is None:
        return
    try:
        token = _refresh_if_needed(token)
    except Exception:
        return
    try:
        r = requests.delete(
            f'{CALENDAR_API_BASE}/calendars/{token.calendar_id}/'
            f'events/{call.google_event_id}?sendUpdates=all',
            headers={
                'Authorization': f'Bearer {token.access_token}',
            },
            timeout=15,
        )
        if r.status_code in (200, 204, 404, 410):
            call.google_event_id = ''
            call.save(update_fields=['google_event_id'])
    except Exception:
        logger.exception(
            'google_calendar: cancel failed for call %s', call.pk)
