"""
Outbound GHL calls for the receptionist: the Voice AI call-logs API.

Shaped after ``outreach/ghl.py`` — bearer auth, ``Version`` header, retry
on 429 honouring ``Retry-After``, error bodies truncated so no PII reaches
the logs. Separate module because the auth differs in an important way:
``outreach/ghl.py`` talks to the agency's own sub-account with a single
settings token, while every call here is scoped to a CLIENT's sub-account
using that program's own stored token.

Only one endpoint is needed, and it is read-only:

    GET /voice-ai/dashboard/call-logs?locationId=&Version=v3

Agent creation and configuration are deliberately not here. Those are
done by the operator in the GHL UI and recorded on the program, the same
division of labour as the Review System's per-client workflow build. An
automated create-agent flow is a bigger piece of work and would not
remove the human step for A2P and number provisioning anyway.
"""

import logging
import random
import time

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
MAX_ATTEMPTS = 3
# GHL caps pageSize at 50. Asking for more is rejected outright rather
# than silently truncated, so this is a hard ceiling, not a preference.
MAX_PAGE_SIZE = 50
API_VERSION = 'v3'


class VoiceGHLError(Exception):
    """Any failure talking to GHL's Voice AI API."""


class VoiceGHLNotConfigured(VoiceGHLError):
    """The program has no usable GHL credentials."""


def _headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Version': getattr(settings, 'GHL_API_VERSION', '2021-07-28'),
        'Accept': 'application/json',
    }


def _program_token(program):
    """The client's own token, falling back to the agency token.

    The fallback exists because a single-tenant setup (everything under
    the agency sub-account) is a legitimate early configuration, and
    refusing to reconcile in that case would mean no reconciliation at
    all until per-client tokens are issued.
    """
    token = ''
    try:
        token = program.ghl_token() or ''
    except Exception:
        logger.warning('voice.ghl: could not decrypt token for program %s',
                       program.pk, exc_info=True)
    return token or (getattr(settings, 'GHL_API_KEY', '') or '')


def _get(path, params, token):
    import requests

    base = (getattr(settings, 'GHL_API_BASE', '')
            or 'https://services.leadconnectorhq.com').rstrip('/')
    url = f'{base}{path}'

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(
                url, params=params, headers=_headers(token),
                timeout=DEFAULT_TIMEOUT)
        except Exception as exc:
            last_error = str(exc)
            if attempt == MAX_ATTEMPTS:
                raise VoiceGHLError(f'request failed: {last_error}') from exc
            time.sleep(attempt + random.random())
            continue

        if response.status_code == 200:
            try:
                return response.json()
            except Exception as exc:
                raise VoiceGHLError('non-JSON response') from exc

        if response.status_code == 429 and attempt < MAX_ATTEMPTS:
            retry_after = response.headers.get('Retry-After')
            try:
                delay = float(retry_after) if retry_after else attempt * 2.0
            except (TypeError, ValueError):
                delay = attempt * 2.0
            time.sleep(min(delay, 30) + random.random())
            continue

        # Truncated: GHL echoes request context into errors and a call
        # log can contain a caller's phone number and transcript.
        body = (response.text or '')[:300]
        raise VoiceGHLError(f'HTTP {response.status_code}: {body}')

    raise VoiceGHLError(last_error or 'exhausted retries')


def fetch_call_logs(program, *, start, end, page_size=MAX_PAGE_SIZE):
    """Every LIVE call log for `program` between `start` and `end`.

    `start`/`end` are datetimes; GHL wants Unix timestamps and requires
    both or neither. Pages until the reported total is covered, with a
    page ceiling so a pagination bug on either side cannot spin forever.

    Returns a list of raw dicts. Interpretation stays in
    ``voice/tasks.py`` alongside the webhook's mapping, so there is one
    place where a GHL field name maps to one of our columns.
    """
    token = _program_token(program)
    if not token or not program.ghl_location_id:
        raise VoiceGHLNotConfigured(
            f'program {program.pk} has no token or location id')

    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    collected = []
    page = 1
    max_pages = 200

    while page <= max_pages:
        payload = _get(
            '/voice-ai/dashboard/call-logs',
            {
                'locationId': program.ghl_location_id,
                'Version': API_VERSION,
                'startDate': int(start.timestamp()),
                'endDate': int(end.timestamp()),
                # LIVE excludes GHL's own trial calls at the source. The
                # webhook path still has to check `trialCall` because it
                # receives everything, but there is no reason to pull
                # trial rows here.
                'callType': 'LIVE',
                'page': page,
                'pageSize': page_size,
                'sortBy': 'createdAt',
                'sort': 'ascend',
            },
            token,
        )
        logs = payload.get('callLogs') or []
        collected.extend(logs)

        total = payload.get('total')
        if not logs or total is None or len(collected) >= int(total):
            break
        page += 1

    if page > max_pages:
        logger.warning(
            'voice.ghl: hit the %s-page ceiling for program %s — some '
            'calls were not reconciled', max_pages, program.pk)
    return collected
