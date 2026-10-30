"""
Machine surface for the website audit: the endpoints a GoHighLevel Voice
AI agent calls mid-call.

**Async only. There is deliberately no synchronous PageSpeed path here.**
A cold PSI run was measured at 10.5s minimum, 14.5s median and 35.5s
worst case against a 45s timeout (see AUDIT_API_INVESTIGATION.md). A
voice agent has ~3-5s before the silence is unbearable, so a synchronous
audit is not a slow endpoint, it is a broken one. Either the answer is
already cached and comes back instantly, or the caller gets a job id and
GHL delivers the result after the call.

Two endpoints:

  POST /api/audit/request/           cache hit -> 200 ready, else 202 pending
  GET  /api/audit/status/<job_id>/   poll a job

Both are gated on the ``X-Audit-Secret`` header, compared in constant
time and failing closed when the setting is empty -- the same posture as
``voice.ghl_webhook`` and for the same reason: an unset secret must not
mean "accept anything" when accepting anything spends PageSpeed quota and
worker time.

No CORS. This is server-to-server only; nothing in a browser calls it.
"""

import hmac
import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from django_ratelimit.decorators import ratelimit

from public.tasks import (
    AUDIT_JOB_TTL, audit_cache_key, job_cache_key, run_audit_for_voice,
)
from public.views import normalized_audit_url

logger = logging.getLogger(__name__)


def _secret_ok(provided):
    """Constant-time compare against AUDIT_API_SECRET.

    Mirrors voice.ghl_webhook._secret_ok exactly, including the
    fail-closed branch: no secret configured means every request is
    rejected.
    """
    expected = (getattr(settings, 'AUDIT_API_SECRET', '') or '').strip()
    if not expected:
        # Fail closed. An unset secret must not mean "accept anything" --
        # here accepting anything means burning PageSpeed quota and
        # worker time on URLs chosen by a stranger.
        return False
    return hmac.compare_digest(str(provided or ''), expected)


def _payload_from_request(request):
    """
    Request body as a dict, accepting either JSON or form encoding.

    Parsed once and stashed on the request, because the rate-limit key
    function below needs the URL *before* the view body runs. Exactly one
    of ``request.body`` / ``request.POST`` is ever touched for a given
    request -- mixing them can consume the stream out from under the
    other.
    """
    cached = getattr(request, '_audit_api_payload', None)
    if cached is not None:
        return cached

    data = {}
    content_type = (request.content_type or '').lower()
    if 'json' in content_type:
        try:
            parsed = json.loads((request.body or b'').decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            data = parsed
    else:
        data = dict(request.POST.items())

    request._audit_api_payload = data
    return data


def _ratelimit_url_key(group, request):
    """
    Rate-limit bucket: the normalized URL, never the client IP.

    Every call arrives from one GoHighLevel IP, so an IP key would let
    the first few prospects of the hour exhaust the budget for everyone
    else. Keying on the URL caps repeat lookups of the *same* site --
    which is the only abuse shape that costs us anything, since a second
    distinct URL is a second real prospect.

    A callable rather than the string ``'post:url'``: that shorthand
    reads ``request.POST``, which is empty when GHL posts a JSON body, so
    every request would collapse into one shared bucket keyed on the
    empty string. This keys on the URL for both encodings.
    """
    url = str(_payload_from_request(request).get('url') or '').strip()
    return normalized_audit_url(url) or 'audit-api:no-url'


def _log_request(raw_url, cache_key, cache_outcome, status, http_status):
    """
    One line per request to /api/audit/request/, always the same shape so
    it greps cleanly: what the caller sent, what we keyed it to, whether
    that was a hit, and what we returned.

    ``%r`` on the raw URL deliberately — it may be whatever a voice agent
    transcribed, including spaces and quotes, and the repr keeps the
    field boundaries readable when it is garbage.
    """
    logger.info(
        'audit api: raw=%r key=%s cache=%s status=%s http=%s',
        raw_url, cache_key or '-', cache_outcome, status, http_status,
    )


@csrf_exempt
@ratelimit(key=_ratelimit_url_key, rate='30/h', method='POST', block=False)
def audit_request(request):
    """POST /api/audit/request/

    Body (JSON or form): ``{"url": ..., "contact_id": ..., "phone": ...}``
    ``contact_id`` and ``phone`` are optional and only travel as far as
    the GHL callback.

    Cache hit  -> 200 ``{"status": "ready", "cached": true, ...}``
    Cache miss -> 202 ``{"status": "pending", "job_id": ...}``

    Returns in well under 100ms either way; it never waits on PageSpeed.

    One known ordering wrinkle: ``@ratelimit`` evaluates its key before
    the view body, so an unauthenticated request still increments the
    bucket for whatever URL it names. Tolerated rather than fixed with a
    bespoke decorator -- the budget is per-URL and generous, and the
    attacker has to guess a URL we care about to waste anything.
    """
    if request.method != 'POST':
        _log_request('', '', 'n/a', 'failed', 405)
        return JsonResponse(
            {'status': 'failed', 'reason': 'This endpoint accepts POST only.'},
            status=405,
        )

    data = _payload_from_request(request)
    url = str(data.get('url') or '').strip()
    contact_id = str(data.get('contact_id') or '').strip() or None
    phone = str(data.get('phone') or '').strip() or None

    if not _secret_ok(request.headers.get('X-Audit-Secret')):
        # The URL is logged but never the header value.
        _log_request(url, '', 'n/a', 'unauthorized', 403)
        return JsonResponse(
            {'status': 'failed', 'reason': 'Not authorized.'}, status=403)

    if getattr(request, 'limited', False):
        _log_request(url, audit_cache_key(url), 'n/a', 'rate_limited', 429)
        return JsonResponse(
            {'status': 'failed',
             'reason': ('That site has been checked several times already. '
                        'Please try again later.')},
            status=429,
        )

    if not url:
        _log_request(url, '', 'n/a', 'no_url', 400)
        return JsonResponse(
            {'status': 'failed', 'reason': 'No website address was supplied.'},
            status=400,
        )

    result_key = audit_cache_key(url)
    if not result_key:
        _log_request(url, '', 'n/a', 'unparseable_url', 400)
        return JsonResponse(
            {'status': 'failed',
             'reason': ('That does not look like a website address. Could you '
                        'read it out once more?')},
            status=400,
        )

    cached = cache.get(result_key)
    if cached is not None:
        _log_request(url, result_key, 'hit',
                     cached.get('status', 'ready'), 200)
        return JsonResponse({**cached, 'cached': True}, status=200)

    job = run_audit_for_voice.delay(url, contact_id=contact_id, phone=phone)
    job_id = getattr(job, 'id', '') or ''

    # Record the job as pending immediately so a status poll that races
    # the worker resolves to "pending" instead of a 404. In eager mode
    # (tests) the task has already finished and written its own record by
    # the time we get here, so never overwrite an existing one.
    if job_id and cache.get(job_cache_key(job_id)) is None:
        cache.set(job_cache_key(job_id),
                  {'status': 'pending', 'url': url}, AUDIT_JOB_TTL)

    _log_request(url, result_key, 'miss', 'pending', 202)
    logger.info('audit api: queued %s as job %s', url, job_id)
    return JsonResponse({'status': 'pending', 'job_id': job_id}, status=202)


@require_GET
@never_cache
def audit_status(request, job_id):
    """GET /api/audit/status/<job_id>/

    ``{"status": "pending"}`` while the worker is running, then the full
    payload once it has finished -- ``ready`` with the scores, or
    ``failed`` / ``invalid_url`` / ``redirected`` with a ``reason``
    string that is safe to read aloud.

    Shaped after ``voice.views_public.program_status``: ``require_GET``,
    ``never_cache``, 404 for an id we have no record of. It differs in
    one respect -- it checks the shared secret. The voice endpoint can
    skip auth because its key grants nothing but "may the agent answer";
    this one returns audit data and is called by a client that is already
    sending the header, so there is no reason to leave it open.

    ``never_cache`` matters here for the same reason it does there: a
    cached "pending" would strand the agent on a job that has finished.
    """
    if not _secret_ok(request.headers.get('X-Audit-Secret')):
        return JsonResponse(
            {'status': 'failed', 'reason': 'Not authorized.'}, status=403)

    record = cache.get(job_cache_key(job_id))
    if record is None:
        raise Http404('no such audit job')
    return JsonResponse(record, status=200)
