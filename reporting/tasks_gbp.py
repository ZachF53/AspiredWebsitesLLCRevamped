"""
Phase 5a-pivot — GBP Celery tasks.

Four scheduled tasks (see CELERY_BEAT_SCHEDULE):
    sync_gbp_reviews_task                    every 4 h
    check_gbp_nap_task                       daily 04:11
    snapshot_gbp_performance_task            monthly day 1, 05:33
    refresh_operator_tokens_task             hourly :17

All loop over eligible Website rows (maintenance tier ≥ Growth,
gbp_location_name set), using the single operator GbpOperatorToken.
Per-site try/except so one bad listing doesn't poison the batch.

Scoped per website because a Google listing describes one business
location. An account running two brands has two listings; iterating
accounts meant the second was never synced.
"""

import datetime as _dt
import logging

from celery import shared_task
from django.utils import timezone

from clients.account_models import Website

logger = logging.getLogger(__name__)


def _operator_token():
    from reporting.models import GbpOperatorToken
    return GbpOperatorToken.objects.order_by('created_at').first()


def _eligible_websites():
    """Maintenance tier ≥ Growth AND a GBP location bound."""
    return (Website.objects
            .filter(package__in=[
                'maintenance_growth', 'maintenance_dominant'])
            .exclude(gbp_location_name='')
            .select_related('account'))


# ─────────────────────────────────────────────────────────────────────────────
# Review sync
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def sync_gbp_reviews_task():
    """Pull reviews for every eligible client, upsert into GbpReview,
    flag low-star or unreplied as needs_attention."""
    from reporting.google_gbp import list_reviews
    from reporting.models import GbpReview

    token = _operator_token()
    if token is None:
        logger.info('sync_gbp_reviews: no operator token — skipping')
        return 0

    processed = 0
    for site in _eligible_websites():
        try:
            reviews = list_reviews(token, site.gbp_location_name)
        except Exception:
            logger.exception(
                'sync_gbp_reviews: list_reviews failed for %s',
                site.pk)
            continue

        for raw in reviews:
            review_id = (raw.get('reviewId') or '').strip()
            if not review_id:
                continue
            comment = (raw.get('comment') or '').strip()
            star_rating = _star_to_int(raw.get('starRating'))
            reply = raw.get('reviewReply') or {}
            reply_text = (reply.get('comment') or '').strip()
            reply_at = _parse_dt(reply.get('updateTime'))
            created_at = _parse_dt(raw.get('createTime'))

            reviewer = raw.get('reviewer') or {}

            need_reason = ''
            if star_rating and star_rating <= 3:
                need_reason = 'low_star'
            elif comment and not reply_text:
                need_reason = 'unreplied'

            GbpReview.objects.update_or_create(
                website_new=site,
                provider_review_id=review_id,
                defaults={
                    'reviewer_name':              (
                        reviewer.get('displayName') or '')[:255],
                    'reviewer_profile_photo_url': (
                        reviewer.get('profilePhotoUrl') or '')[:500],
                    'star_rating':           star_rating,
                    'comment':               comment,
                    'review_created_at':     created_at,
                    'operator_reply_text':   reply_text,
                    'operator_reply_at':     reply_at,
                    'needs_attention':       bool(need_reason),
                    'needs_attention_reason': need_reason,
                },
            )
            processed += 1
    logger.info('sync_gbp_reviews: processed %s review(s)', processed)
    return processed


def _star_to_int(raw):
    """Google returns 'ONE' / 'TWO' / 'THREE' / 'FOUR' / 'FIVE'."""
    if isinstance(raw, int):
        return raw
    mapping = {'ONE': 1, 'TWO': 2, 'THREE': 3, 'FOUR': 4, 'FIVE': 5}
    return mapping.get((raw or '').upper(), 0)


def _parse_dt(value):
    """RFC3339 → tz-aware datetime, best-effort."""
    if not value:
        return None
    try:
        return _dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# NAP sync
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def check_gbp_nap_task():
    """For each eligible site, compare the live GBP listing's NAP
    fields to what we hold. Write a GBPSyncCheck row per field.

    Name, phone and address are account-level facts (the business); the
    website URL is the site's own. Comparing the site's URL against the
    listing is the whole point of the check on a multi-site account —
    the wrong URL on a listing sends the client's callers to the wrong
    brand.
    """
    from reporting.google_gbp import fetch_location
    from reporting.models import GBPSyncCheck

    token = _operator_token()
    if token is None:
        logger.info('check_gbp_nap: no operator token — skipping')
        return 0

    processed = 0
    for site in _eligible_websites():
        try:
            data = fetch_location(token, site.gbp_location_name)
        except Exception:
            logger.exception(
                'check_gbp_nap: fetch_location raised for %s', site.pk)
            continue
        if data is None:
            continue

        gbp_name = (data.get('title') or '').strip()
        phones = data.get('phoneNumbers') or {}
        gbp_phone = (phones.get('primaryPhone') or '').strip()
        sa = data.get('storefrontAddress') or {}
        gbp_address = ' '.join(sa.get('addressLines') or []).strip()
        gbp_website = (data.get('websiteUri') or '').strip()

        account = site.account
        fields = [
            ('business_name',
             ((account.name if account else site.name) or '').strip(),
             gbp_name),
            ('phone', ((account.phone if account else '') or '').strip(),
             gbp_phone),
            ('address', ((account.address if account else '') or '').strip(),
             gbp_address),
            ('website', (site.url or '').strip(), gbp_website),
        ]
        for field_name, web_val, gbp_val in fields:
            mismatch = bool(web_val) and bool(gbp_val) and (
                _norm(web_val) != _norm(gbp_val))
            GBPSyncCheck.objects.create(
                website_new=site,
                field_name=field_name,
                website_value=web_val,
                gbp_value=gbp_val,
                is_mismatch=mismatch,
            )
            processed += 1
    logger.info('check_gbp_nap: %s field comparisons written', processed)
    return processed


def _norm(s):
    """Cheap normalisation for NAP comparisons — collapse whitespace,
    lowercase, strip punctuation that doesn't matter."""
    if not s:
        return ''
    return ' '.join(s.lower().split())


# ─────────────────────────────────────────────────────────────────────────────
# Performance snapshot (monthly)
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def snapshot_gbp_performance_task():
    """First of the month, snapshot the previous month's GBP metrics."""
    from reporting.google_gbp import fetch_monthly_performance
    from reporting.models import GbpPerformanceSnapshot

    token = _operator_token()
    if token is None:
        logger.info('snapshot_gbp_performance: no operator token — skipping')
        return 0

    today = timezone.localdate()
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - _dt.timedelta(days=1)
    year = last_month_end.year
    month = last_month_end.month
    snapshot_month = _dt.date(year, month, 1)

    processed = 0
    for site in _eligible_websites():
        try:
            totals = fetch_monthly_performance(
                token, site.gbp_location_name, year, month)
        except Exception:
            logger.exception(
                'snapshot_gbp_performance: fetch failed for %s', site.pk)
            continue
        if not totals:
            continue

        GbpPerformanceSnapshot.objects.update_or_create(
            website_new=site,
            snapshot_month=snapshot_month,
            defaults={
                'profile_views_search': (
                    totals.get('BUSINESS_IMPRESSIONS_DESKTOP_SEARCH', 0)
                    + totals.get('BUSINESS_IMPRESSIONS_MOBILE_SEARCH', 0)),
                'profile_views_maps': (
                    totals.get('BUSINESS_IMPRESSIONS_DESKTOP_MAPS', 0)
                    + totals.get('BUSINESS_IMPRESSIONS_MOBILE_MAPS', 0)),
                'call_clicks':         totals.get('CALL_CLICKS', 0),
                'direction_requests':  totals.get(
                    'BUSINESS_DIRECTION_REQUESTS', 0),
                'website_clicks':      totals.get('WEBSITE_CLICKS', 0),
                'raw_payload':         totals.get('_raw_payload') or {},
            },
        )
        processed += 1
    logger.info(
        'snapshot_gbp_performance: %s snapshot(s) written', processed)
    return processed


# ─────────────────────────────────────────────────────────────────────────────
# Token refresh sweep
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def refresh_operator_tokens_task():
    """Pro-actively refresh any operator token expiring within 10 min."""
    from reporting.google_gbp import refresh_if_needed
    from reporting.models import GbpOperatorToken

    cutoff = timezone.now() + _dt.timedelta(minutes=10)
    qs = GbpOperatorToken.objects.filter(expires_at__lte=cutoff)
    refreshed = 0
    failed = 0
    for token in qs[:50]:
        try:
            refresh_if_needed(token)
            refreshed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception(
                'refresh_operator_tokens: refresh failed for token %s',
                token.id)
            try:
                from core.system_alerts import record_alert
                record_alert(
                    severity='error',
                    source='reporting.gbp.token_refresh',
                    message=(
                        f'GBP operator token refresh failed for user '
                        f'{token.user_id}'),
                    detail=str(exc)[:2000],
                )
            except Exception:
                pass
    return {'refreshed': refreshed, 'failed': failed}
