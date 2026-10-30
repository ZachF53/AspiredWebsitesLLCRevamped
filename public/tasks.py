"""Celery tasks for the public site."""

import hashlib
import logging
from urllib.parse import urlparse

import requests
import tldextract
from celery import shared_task
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


# ── Audit API (GHL Voice AI agent) ──────────────────────────────────────────
#
# Result TTLs. 24h for a success because Lighthouse scores do not move
# hour to hour -- three consecutive runs of the same site returned
# 72/73/75, which is the tool's own run-to-run noise, not real change. A
# day-old score is as true as a fresh one for a sales conversation.
#
# 1h for a failure so a dead site does not cost ~18s of worker time on
# every call, while still letting a genuinely-fixed site re-audit the
# same day.
AUDIT_SUCCESS_TTL = 60 * 60 * 24
AUDIT_FAILURE_TTL = 60 * 60

# Job records outlive the result they point at so a late status poll
# still resolves instead of 404ing.
AUDIT_JOB_TTL = 60 * 60 * 24

# How long we wait when independently re-fetching the URL PSI reported as
# final. This is the 404-guard probe, not the audit -- it must be short,
# because the whole point is that it is cheap next to the 10-35s audit.
VERIFY_TIMEOUT = 8

# Bundled public-suffix snapshot, no network. The default TLDExtract
# tries to refresh the suffix list over HTTP on first use, which would
# put a live fetch inside a Celery task and inside the test suite.
#
# Deliberately NOT outreach.enricher._base_domain: that one takes the
# last two labels and its own docstring notes it is wrong for .co.uk and
# friends. Here a false "same domain" would wave through the exact
# cross-domain redirect this guard exists to catch, so it needs the real
# answer. tldextract is already in requirements.txt.
_tld = tldextract.TLDExtract(suffix_list_urls=())


def audit_cache_key(url):
    """Cache key for a finished audit payload, or '' for an unusable URL.

    The normalized URL is hashed rather than interpolated. A URL path is
    unbounded in both length and character set, and a cache key has to be
    short and safe -- Django warns past 250 chars or on whitespace, and
    memcached rejects both outright. The readable form still goes to the
    logs and to the rate-limit bucket, where it is useful.
    """
    from public.views import normalized_audit_url
    normalized = normalized_audit_url(url)
    if not normalized:
        return ''
    digest = hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:32]
    return f'audit_api:{digest}'


def job_cache_key(job_id):
    """Cache key for a job record (status polling)."""
    return f'audit_api:job:{job_id}'


def _registrable_domain(url_or_host):
    """eTLD+1 of a URL or bare host, lowercased. '' when unparseable."""
    raw = (url_or_host or '').strip()
    if not raw:
        return ''
    if '://' in raw:
        try:
            raw = urlparse(raw).hostname or ''
        except ValueError:
            return ''
    return (_tld.extract_str(raw).registered_domain or '').lower()


def _verify_url_loads(url):
    """
    Independently fetch ``url`` and report what it really returns.

    Returns ``(status_code, final_url)``, or ``(None, url)`` when the
    request could not be completed at all.

    This exists because PSI happily audits an error page: a 404 path on a
    live domain returns HTTP 200 from PSI with a performance score of
    100, since a lightweight error page is genuinely fast. Measured and
    confirmed. Without this probe a misheard domain yields a confident
    "your site scores 100" on a live sales call.

    HEAD first, GET on the responses that mean "HEAD specifically is
    unwelcome" -- plenty of small-business hosts 403/405 a HEAD while
    serving GET fine, and treating that as a dead site would be its own
    false negative.
    """
    from outreach.enricher import USER_AGENT

    headers = {'User-Agent': USER_AGENT}
    try:
        resp = requests.head(url, timeout=VERIFY_TIMEOUT,
                             allow_redirects=True, headers=headers)
        if resp.status_code in (403, 405, 501):
            resp = requests.get(url, timeout=VERIFY_TIMEOUT,
                                allow_redirects=True, headers=headers,
                                stream=True)
            resp.close()
    except requests.RequestException:
        try:
            resp = requests.get(url, timeout=VERIFY_TIMEOUT,
                                allow_redirects=True, headers=headers,
                                stream=True)
            resp.close()
        except requests.RequestException as exc:
            logger.info('audit verify: %s did not load (%s)',
                        url, type(exc).__name__)
            return None, url
    return resp.status_code, (resp.url or url)


# Voice-safe failure phrasing. _PageSpeedError's own messages are web
# copy -- they cite a phone number and tell the reader to open a browser
# -- so they cannot be read aloud on a call.
_VOICE_REASONS = {
    'timeout': ('The analysis took too long to finish. I can have someone '
                'send the results over shortly.'),
    'bad_status': ('I could not analyze that site. It may be down or '
                   'blocking automated checks.'),
    'unreachable': ('The analysis service did not respond. I can have '
                    'someone send the results over shortly.'),
    'bad_json': ('The analysis service did not respond properly. I can have '
                 'someone send the results over shortly.'),
}
_VOICE_REASON_FALLBACK = ('I could not analyze that site right now. I can '
                          'have someone send the results over shortly.')


def _post_audit_callback(payload, contact_id, phone):
    """
    POST the finished payload to GHL. No retries: the repo has no retry
    infrastructure and a silently-retried audit would arrive after the
    call it was for. An empty setting is a skip, not an error -- a
    missing webhook must not fail an audit that otherwise succeeded.
    """
    endpoint = (getattr(settings, 'GHL_AUDIT_WEBHOOK_URL', '') or '').strip()
    if not endpoint:
        logger.info('audit callback skipped: GHL_AUDIT_WEBHOOK_URL unset')
        return
    body = dict(payload)
    if contact_id:
        body['contact_id'] = contact_id
    if phone:
        body['phone'] = phone
    try:
        resp = requests.post(endpoint, json=body, timeout=10)
        logger.info('audit callback -> %s returned %s',
                    endpoint, resp.status_code)
    except requests.RequestException as exc:
        logger.warning('audit callback to %s failed: %s', endpoint, exc)


@shared_task
def send_audit_followups_task():
    """Daily — send follow-up 1 and 2 to people who ran an audit.

    Warm, not cold: these people typed their own URL in and handed over
    an email to get the results. They asked.

    Runs once a day rather than hourly because the schedule is measured
    in days and a follow-up arriving at 09:00 reads better than one
    arriving at 14:37. Every send is guarded by its own timestamp field,
    so a double-run inside the same day sends nothing twice.
    """
    from datetime import timedelta

    from django.utils import timezone

    from public.audit_sequence import (
        FOLLOWUP_1_DAYS, FOLLOWUP_2_DAYS, FOLLOWUP_2_MIN_GAP_DAYS,
        send_followup,
    )
    from public.models import AuditLead

    now = timezone.now()
    sent_1 = sent_2 = skipped = failed = 0

    # Follow-up 1: report sent at least FOLLOWUP_1_DAYS ago, none sent yet.
    due_1 = AuditLead.objects.filter(
        report_sent_at__isnull=False,
        report_sent_at__lte=now - timedelta(days=FOLLOWUP_1_DAYS),
        followup_1_sent_at__isnull=True,
        unsubscribed=False,
    ).exclude(email='')

    # Follow-up 2: both windows must be satisfied. The gap after
    # follow-up 1 is what stops a record whose report sat unsent for ten
    # days from receiving both follow-ups in the same batch, minutes
    # apart -- which is what happened before this second condition
    # existed, and reads exactly like the broken system it is.
    due_2 = AuditLead.objects.filter(
        report_sent_at__isnull=False,
        report_sent_at__lte=now - timedelta(days=FOLLOWUP_2_DAYS),
        followup_1_sent_at__isnull=False,
        followup_1_sent_at__lte=now - timedelta(
            days=FOLLOWUP_2_MIN_GAP_DAYS),
        followup_2_sent_at__isnull=True,
        unsubscribed=False,
    ).exclude(email='')

    for lead in due_1:
        try:
            if send_followup(lead, 1):
                sent_1 += 1
            else:
                skipped += 1
        except Exception:  # noqa: BLE001 - one bad address must not stop the batch
            logger.exception('audit follow-up 1 failed for %s', lead.pk)
            failed += 1

    for lead in due_2:
        try:
            if send_followup(lead, 2):
                sent_2 += 1
            else:
                skipped += 1
        except Exception:  # noqa: BLE001
            logger.exception('audit follow-up 2 failed for %s', lead.pk)
            failed += 1

    result = (f'followup_1={sent_1} followup_2={sent_2} '
              f'skipped={skipped} failed={failed}')
    logger.info('send_audit_followups_task: %s', result)
    return result


@shared_task
def sync_google_reviews_task():
    """Daily — refresh Aspired's own Google rating, count and reviews."""
    from public import google_reviews
    try:
        result = google_reviews.sync()
    except google_reviews.GoogleReviewsError as err:
        logger.warning('sync_google_reviews_task failed: %s', err)
        return f'failed: {err}'
    logger.info('sync_google_reviews_task: %s', result)
    return result


@shared_task(bind=True)
def run_audit_for_voice(self, url, contact_id=None, phone=None):
    """Run one audit for the voice API, then cache it and call GHL back.

    Always returns a payload dict and never raises: the caller is a
    phone call that has already hung up on this question, so a failure
    has to come back as a readable sentence rather than a traceback.

    ``bind=True`` because the job record is keyed on this task's own id,
    which only exists once Celery has assigned it. The result backend is
    not used for this -- settings.py sets CELERY_TASK_IGNORE_RESULT =
    True, so AsyncResult would carry no payload. The cache is the
    authority.
    """
    from public.views import (
        _PageSpeedError, _audit_summary, _run_pagespeed_audit,
    )

    job_id = getattr(self.request, 'id', None) or ''
    result_key = audit_cache_key(url)

    def finish(payload, ttl):
        """Cache, then call back. Cache first, on purpose -- a second
        caller or a status poll arriving mid-callback must find the
        result already warm."""
        payload = {'url': url, **payload}
        if result_key:
            cache.set(result_key, payload, ttl)
        if job_id:
            cache.set(job_cache_key(job_id), payload, AUDIT_JOB_TTL)
        _post_audit_callback(payload, contact_id, phone)
        return payload

    # ── 1. The audit itself ────────────────────────────────────────────
    try:
        result = _run_pagespeed_audit(url, include_raw=True)
    except _PageSpeedError as err:
        code = getattr(err, 'code', '')
        logger.info('audit %s failed (code=%s)', url, code)
        return finish(
            {'status': 'failed',
             'reason': _VOICE_REASONS.get(code, _VOICE_REASON_FALLBACK)},
            AUDIT_FAILURE_TTL,
        )

    raw = result.get('raw') or {}
    lighthouse = raw.get('lighthouseResult') or {}
    requested_url = lighthouse.get('requestedUrl') or url
    final_url = (lighthouse.get('finalUrl')
                 or lighthouse.get('finalDisplayedUrl')
                 or requested_url)

    # ── 2. Validity guard ─────────────────────────────────────────────
    # PSI returning 200 is not evidence the page exists. Re-fetch what it
    # says it audited and look at the real status.
    status_code, verified_url = _verify_url_loads(final_url)
    logger.info(
        'audit %s: requested=%s final=%s verified=%s http=%s',
        url, requested_url, final_url, verified_url, status_code,
    )

    if status_code is None:
        logger.warning(
            'audit guard REJECTED %s as invalid_url: requestedUrl=%s '
            'finalUrl=%s probe_http=unreachable',
            url, requested_url, final_url,
        )
        return finish(
            {'status': 'invalid_url',
             'final_url': final_url,
             'reason': ('That address did not load, so I could not analyze '
                        'it. Could you double-check the website address?')},
            AUDIT_FAILURE_TTL,
        )

    if status_code >= 400:
        logger.warning(
            'audit guard REJECTED %s as invalid_url: requestedUrl=%s '
            'finalUrl=%s probe_http=%s (PSI reported success for this page)',
            url, requested_url, verified_url, status_code,
        )
        return finish(
            {'status': 'invalid_url',
             'final_url': verified_url,
             'http_status': status_code,
             'reason': (f'That address returned a {status_code} error, so '
                        'there was nothing to analyze. Could you '
                        'double-check the website address?')},
            AUDIT_FAILURE_TTL,
        )

    # A scheme upgrade or a www./trailing-slash change is normal and
    # expected; both compare equal here because only the registrable
    # domain is compared. A genuinely different domain is not rejected
    # outright -- it is handed back for the agent to confirm, since it is
    # just as likely to be a legitimate rebrand as a mis-heard name.
    # Scores are deliberately omitted: they describe the OTHER site, and
    # reading them out as the caller's would be the same class of lie the
    # 404 guard above exists to prevent.
    submitted_domain = _registrable_domain(url)
    landed_domain = _registrable_domain(verified_url)
    if submitted_domain and landed_domain and submitted_domain != landed_domain:
        logger.warning(
            'audit guard REJECTED %s as redirected: requestedUrl=%s '
            'finalUrl=%s probe_http=%s (%s -> %s)',
            url, requested_url, verified_url, status_code,
            submitted_domain, landed_domain,
        )
        return finish(
            {'status': 'redirected',
             'final_url': verified_url,
             'requested_url': requested_url,
             'reason': (f'That address redirects to {landed_domain}. I can '
                        'analyze that site instead if you confirm it is '
                        'the right one.')},
            AUDIT_FAILURE_TTL,
        )

    # ── 3. Extract ────────────────────────────────────────────────────
    scores = result.get('scores') or {}
    audits = lighthouse.get('audits') or {}
    lcp = audits.get('largest-contentful-paint') or {}

    lcp_ms = lcp.get('numericValue')
    try:
        lcp_ms = int(round(float(lcp_ms))) if lcp_ms is not None else None
    except (TypeError, ValueError):
        lcp_ms = None
    lcp_seconds = round(lcp_ms / 1000, 1) if lcp_ms is not None else None

    # Field data, not lab data, and only present for sites with enough
    # real traffic -- most small contractors will have none.
    crux = (raw.get('loadingExperience') or {}).get('overall_category') or None

    return finish(
        {'status': 'ready',
         'final_url': verified_url,
         'performance_score': scores.get('performance'),
         'lcp_ms': lcp_ms,
         'lcp_seconds': lcp_seconds,
         'crux': crux,
         # The existing summary generator, not a new one: it already
         # returns one clean sentence with no markup and takes no request.
         'summary': _audit_summary(url, scores)['text']},
        AUDIT_SUCCESS_TTL,
    )
