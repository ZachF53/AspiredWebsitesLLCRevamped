"""
Phase 5b/5c — Social media Celery tasks (Meta, LinkedIn).

Two tasks:
  publish_due_posts       Every 5 min. Picks up ScheduledPost rows whose
                          scheduled_for <= now AND status='scheduled',
                          atomically flips them to 'publishing', then
                          dispatches by platform.

  refresh_expiring_tokens Hourly. Re-exchanges Meta long-lived tokens
                          before they hit 60 days. LinkedIn skipped —
                          their refresh-token model is product-tier
                          gated; if we have a refresh_token row, we
                          use it; if not, the operator re-consents.

Race safety: publish_due_posts uses a status-guarded UPDATE that returns
0 rows if another worker already grabbed the post — at-most-once
semantics without a distributed lock.
"""

import datetime as _dt
import logging

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from clients.display import owner_label
from core.system_alerts import record_alert

from .models import PostResult, ScheduledPost, SocialToken

logger = logging.getLogger(__name__)


# Map SocialChannel.platform → publisher callable. Imported lazily so a
# broken publisher module doesn't crash the task module's import.
def _get_publisher(platform):
    if platform == 'facebook':
        from .meta_publisher import publish_facebook_post
        return publish_facebook_post
    if platform == 'instagram':
        from .meta_publisher import publish_instagram_post
        return publish_instagram_post
    if platform == 'linkedin':
        from .linkedin_publisher import publish_linkedin_post
        return publish_linkedin_post
    return None


@shared_task
def publish_due_posts():
    """Publish every ScheduledPost whose scheduled_for <= now."""
    now = timezone.now()
    due = ScheduledPost.objects.filter(
        status='scheduled',
        scheduled_for__lte=now,
    ).values_list('id', flat=True)[:200]

    published = 0
    failed = 0
    for post_id in list(due):
        # Atomic flip: only one worker wins the row.
        rows = ScheduledPost.objects.filter(
            id=post_id, status='scheduled',
        ).update(status='publishing')
        if rows == 0:
            continue  # another worker got it first

        post = ScheduledPost.objects.select_related(
            'channel', 'channel__token', 'account_new',
        ).filter(id=post_id).first()
        if post is None:
            continue

        publisher = _get_publisher(post.channel.platform)
        if publisher is None:
            # No publisher wired for this platform. Mark failed so the
            # row doesn't loop forever.
            post.status = 'failed'
            post.save(update_fields=['status', 'updated_at'])
            PostResult.objects.create(
                scheduled_post=post,
                success=False,
                error_detail=(
                    f'No publisher wired for platform '
                    f'"{post.channel.platform}".'),
                attempted_at=now,
            )
            failed += 1
            continue

        try:
            result = publisher(post)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                'publish failed for ScheduledPost %s', post.id)
            post.status = 'failed'
            post.save(update_fields=['status', 'updated_at'])
            PostResult.objects.create(
                scheduled_post=post,
                success=False,
                error_detail=str(exc)[:4000],
                attempted_at=timezone.now(),
            )
            # `post.client` is the legacy FK and is None for every account
            # created since the cutover, so reading `.firm_name` off it
            # raised AttributeError *inside this except block* — escaping
            # the loop before `continue`, killing the task, and leaving
            # every remaining client's scheduled posts unpublished. One
            # canonical-only account's failed post stopped everyone's.
            record_alert(
                severity='warning',
                source='social.publish_due_posts',
                message=(
                    f'{post.channel.get_platform_display()} publish '
                    f'failed — {owner_label(post)}'),
                detail=str(exc)[:1000],
            )
            failed += 1
            continue

        post.status = 'published'
        post.published_at = timezone.now()
        post.save(update_fields=[
            'status', 'published_at', 'updated_at'])
        PostResult.objects.create(
            scheduled_post=post,
            provider_post_id=result.get('provider_post_id', ''),
            permalink=result.get('permalink', ''),
            success=True,
            error_detail='',
            attempted_at=post.published_at,
        )
        published += 1

    logger.info(
        'publish_due_posts: %s published, %s failed',
        published, failed)
    return {'published': published, 'failed': failed}


@shared_task
def refresh_expiring_tokens():
    """Re-exchange any token expiring in the next 72 hours."""
    horizon = timezone.now() + _dt.timedelta(hours=72)
    rows = SocialToken.objects.filter(
        expires_at__lte=horizon
    ).select_related('channel')[:200]

    refreshed = 0
    failed = 0
    for token in rows:
        platform = token.channel.platform
        try:
            if platform in ('facebook', 'instagram'):
                from .meta_publisher import refresh_long_lived_token
                refresh_long_lived_token(token)
                refreshed += 1
            elif platform == 'linkedin':
                # LinkedIn refresh requires the Marketing Developer
                # Platform product. If we have a refresh_token on the
                # row we attempt it; otherwise we just record that
                # operator action is needed.
                if not token.refresh_token_encrypted:
                    token.last_refresh_error = (
                        'No refresh_token — operator must re-consent.')
                    token.save(update_fields=[
                        'last_refresh_error', 'updated_at'])
                    continue
                # Production note: implement when MDP is approved.
                token.last_refresh_error = (
                    'LinkedIn refresh not yet implemented — '
                    'pending Marketing Developer Platform approval.')
                token.save(update_fields=[
                    'last_refresh_error', 'updated_at'])
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                'refresh failed for SocialToken %s', token.id)
            token.last_refresh_error = str(exc)[:1000]
            token.save(update_fields=[
                'last_refresh_error', 'updated_at'])
            failed += 1

    logger.info(
        'refresh_expiring_tokens: %s refreshed, %s failed',
        refreshed, failed)
    return {'refreshed': refreshed, 'failed': failed}
