"""
Phase 5a — Celery tasks for the auto-publisher + token refresher.

Two beat-scheduled tasks (settings.py CELERY_BEAT_SCHEDULE):

    publish_due_posts            every  5 min
        Pick up ScheduledPosts where status='scheduled' AND
        scheduled_for <= now. Race-safe lock: atomic UPDATE flips
        status to 'publishing' WHERE status='scheduled'; only the
        winner sees an affected_rows == 1 and proceeds. Writes a
        PostResult for every attempt, success OR failure. Hard
        failures also raise a SystemAlert at error severity so the
        operator sees it without SSH.

    refresh_expiring_tokens      hourly (offset minute=17)
        Pro-actively refresh any SocialToken whose expires_at is
        within the next 10 minutes. The publisher refreshes on
        demand anyway, but doing it ahead of time keeps the publish
        path latency-stable.

Both tasks isolate per-row failures — one bad token / post doesn't
block the others.
"""

import datetime as _dt
import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Auto-publisher
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def publish_due_posts():
    """Find scheduled posts whose time has come and publish them."""
    from social.models import ScheduledPost
    due = ScheduledPost.objects.filter(
        status='scheduled',
        scheduled_for__lte=timezone.now(),
    ).only('id', 'channel_id')

    handled = 0
    for stub in due[:200]:
        # Per-row try so one bad post doesn't poison the queue.
        try:
            if _claim_and_publish(stub.id):
                handled += 1
        except Exception:
            logger.exception(
                'publish_due_posts: unexpected error on post %s',
                stub.id)
    logger.info('publish_due_posts: handled %s post(s)', handled)
    return handled


def _claim_and_publish(post_id):
    """Atomic claim + publish for one ScheduledPost. Returns True if we
    successfully ran the publish path (success OR failure), False if
    another worker had already taken the row."""
    from social.models import PostResult, ScheduledPost
    # Race-safe claim: only one worker can flip a given row from
    # 'scheduled' → 'publishing'. The UPDATE returns the affected row
    # count; if it's 0 the row is gone from under us.
    claimed = ScheduledPost.objects.filter(
        id=post_id, status='scheduled',
    ).update(status='publishing')
    if claimed != 1:
        return False

    # Hot reload after the claim so we have the current row state.
    post = ScheduledPost.objects.select_related('channel').filter(
        id=post_id).first()
    if post is None:
        return True  # vanished between claim + reload — treat as handled

    try:
        provider_id, permalink, error_detail = _dispatch_publish(post)
        if error_detail:
            _record_result(post, success=False, error=error_detail)
            post.status = 'failed'
            post.save(update_fields=['status', 'updated_at'])
            _alert(
                'social.publish.failed',
                f'Publish failed for post {post.id}',
                error_detail)
        else:
            _record_result(post, success=True,
                           provider_id=provider_id, permalink=permalink)
            post.status = 'published'
            post.published_at = timezone.now()
            post.save(update_fields=[
                'status', 'published_at', 'updated_at'])
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            '_claim_and_publish: unhandled exception for post %s',
            post_id)
        _record_result(post, success=False, error=str(exc)[:2000])
        post.status = 'failed'
        post.save(update_fields=['status', 'updated_at'])
        _alert(
            'social.publish.exception',
            f'Publish exception for post {post.id}',
            str(exc)[:2000])
    return True


def _dispatch_publish(post):
    """Run the platform-specific publish. Returns
    (provider_id: str, permalink: str, error_detail: str).
    If error_detail is non-empty, caller treats as failure."""
    channel = post.channel
    platform = channel.platform

    if platform == 'gbp':
        return _publish_gbp(post)

    # 5b/5c will add meta + linkedin branches here.
    return ('', '', f'Platform "{platform}" not yet supported in 5a.')


def _publish_gbp(post):
    """GBP-specific publish path. Looks up the SocialToken for the
    channel, decrypts, calls google_gbp.create_local_post."""
    from social.google_gbp import create_local_post
    from social.models import SocialToken

    token = SocialToken.objects.filter(channel=post.channel).first()
    if token is None:
        return ('', '',
                'No SocialToken on file for this channel — operator '
                'must connect Google Business Profile.')

    # The location_name (accounts/.../locations/...) lives on
    # provider_account_id when we set it during connect — but our
    # current connect_callback stores the operator's Google account
    # email there for display. For Phase 5a we accept the GBP location
    # name via the channel's `handle` field IF it looks like a location
    # resource, else surface an error so the operator picks a location.
    handle = (post.channel.handle or '').strip()
    if not (handle.startswith('accounts/') and '/locations/' in handle):
        return ('', '',
                'SocialChannel.handle must be a GBP location resource '
                '(accounts/<acc>/locations/<loc>). Set this on the '
                'connect page using the location picker.')

    try:
        provider_id, permalink = create_local_post(
            token, handle, post.body)
    except (ValueError, RuntimeError) as exc:
        return ('', '', str(exc))
    return (provider_id, permalink, '')


def _record_result(post, *, success, provider_id='', permalink='', error=''):
    from social.models import PostResult
    PostResult.objects.create(
        scheduled_post=post,
        provider_post_id=provider_id,
        permalink=permalink,
        success=success,
        error_detail=error,
        attempted_at=timezone.now(),
    )


def _alert(source, message, detail):
    """Best-effort SystemAlert write so failures surface on the
    admin dashboard without an SSH dive."""
    try:
        from core.system_alerts import record_alert
        record_alert(
            severity='error', source=source,
            message=message, detail=detail,
        )
    except Exception:
        logger.exception('_alert: record_alert failed')


# ─────────────────────────────────────────────────────────────────────────────
# Pro-active token refresh
# ─────────────────────────────────────────────────────────────────────────────

@shared_task
def refresh_expiring_tokens():
    """Refresh any SocialToken expiring within 10 minutes. The
    publisher refreshes on-demand anyway, but doing it ahead of time
    keeps publish latency stable + surfaces refresh errors before they
    cause a publish failure."""
    from social.google_gbp import _refresh_if_needed
    from social.models import SocialToken

    cutoff = timezone.now() + _dt.timedelta(minutes=10)
    qs = SocialToken.objects.filter(expires_at__lte=cutoff)

    refreshed = 0
    failed = 0
    for token in qs[:200]:
        try:
            _refresh_if_needed(token)
            refreshed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception(
                'refresh_expiring_tokens: refresh failed for token %s',
                token.id)
            # Only alert on hard refresh failures — a missing refresh
            # token means the operator needs to re-connect.
            _alert(
                'social.token_refresh.failed',
                f'Token refresh failed for channel {token.channel_id}',
                str(exc)[:2000])
    logger.info(
        'refresh_expiring_tokens: refreshed=%s failed=%s',
        refreshed, failed)
    return {'refreshed': refreshed, 'failed': failed}
