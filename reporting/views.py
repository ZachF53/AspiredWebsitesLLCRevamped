"""
Reporting — the public conversion-tracking endpoint.

POST /api/track/ receives events from the on-site aspired-tracker.js snippet.
It is unauthenticated and CSRF-exempt (external sites post here), rate limited
per IP, and always answers 200 so it never leaks whether a client_id is real.
"""

import hashlib
import json
import uuid

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from clients.models import ClientProfile

from .models import ConversionEvent

VALID_EVENT_TYPES = {'form_submit', 'phone_click', 'cta_click'}


def _ok():
    """A 200 response, CORS-open so cross-origin beacons never error."""
    resp = JsonResponse({'status': 'ok'})
    resp['Access-Control-Allow-Origin'] = '*'
    return resp


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _hash_ip(request):
    """A salted SHA-256 of the visitor IP — for dedup, never stored raw."""
    ip = request.META.get('REMOTE_ADDR', '')
    if not ip:
        return ''
    return hashlib.sha256(
        (ip + settings.SECRET_KEY).encode('utf-8')).hexdigest()


@csrf_exempt
@require_POST
@ratelimit(key='ip', rate='100/m', block=True)
def track_conversion_event(request):
    """Record one conversion event. Always returns 200 — never leaks info."""
    try:
        data = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return _ok()
    if not isinstance(data, dict):
        return _ok()

    event_type = data.get('event_type', '')
    if event_type not in VALID_EVENT_TYPES:
        return _ok()

    client_id = data.get('client_id', '')
    if not _is_uuid(client_id):
        return _ok()
    client = ClientProfile.objects.filter(id=client_id).first()
    if client is None:
        return _ok()

    event_ts = parse_datetime(str(data.get('timestamp') or '')) or timezone.now()

    ConversionEvent.objects.create(
        client=client,
        event_type=event_type,
        element_id=str(data.get('element_id') or '')[:100],
        element_text=str(data.get('element_text') or '')[:100],
        page_url=str(data.get('page_url') or '')[:200],
        page_title=str(data.get('page_title') or '')[:200],
        event_timestamp=event_ts,
        ip_hash=_hash_ip(request),
    )
    return _ok()
