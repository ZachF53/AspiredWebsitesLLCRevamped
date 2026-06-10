"""
Phase 5b — Meta (Facebook + Instagram) post publisher.

Two entry points, called by social.tasks.publish_due_posts based on
SocialChannel.platform:

  publish_facebook_post(scheduled_post)
      Page feed post — text + optional image. Uses the Page Access
      Token stored on SocialToken.access_token_encrypted.

  publish_instagram_post(scheduled_post)
      Two-step container API: POST a media container with image_url +
      caption, then POST .../media_publish to finalize. Returns the
      IG media id; permalink is fetched in a follow-up call.

Both return a PostResult-shaped dict:
    {'provider_post_id': str, 'permalink': str}

On failure they raise RuntimeError(err_message) so the caller can
record the error on PostResult and flip ScheduledPost.status='failed'.
"""

import logging
import time

import requests

from .crypto import decrypt_token

logger = logging.getLogger(__name__)


PAGE_FEED_URL = 'https://graph.facebook.com/v19.0/{page_id}/feed'
PAGE_PHOTOS_URL = 'https://graph.facebook.com/v19.0/{page_id}/photos'
PAGE_POST_URL = 'https://graph.facebook.com/v19.0/{post_id}'

IG_MEDIA_URL = 'https://graph.facebook.com/v19.0/{ig_id}/media'
IG_PUBLISH_URL = 'https://graph.facebook.com/v19.0/{ig_id}/media_publish'
IG_OBJECT_URL = 'https://graph.facebook.com/v19.0/{media_id}'


def publish_facebook_post(scheduled_post):
    """Publish a ScheduledPost to its bound Facebook Page.

    Returns {'provider_post_id', 'permalink'}.
    Raises RuntimeError on any failure (caller records on PostResult).
    """
    token_row = getattr(scheduled_post.channel, 'token', None)
    if token_row is None:
        raise RuntimeError('No Meta token bound to this channel.')
    page_id = token_row.provider_account_id
    if not page_id:
        raise RuntimeError('Channel has no Facebook Page id on file.')
    access_token = decrypt_token(token_row.access_token_encrypted)
    if not access_token:
        raise RuntimeError(
            'Could not decrypt Page Access Token. VAULT_SERVER_SECRET '
            'may have been rotated since the channel was connected.')

    body = scheduled_post.body or ''
    media_url = scheduled_post.media_url or ''

    if media_url:
        # Photo posts go to /photos with the image URL + caption.
        url = PAGE_PHOTOS_URL.format(page_id=page_id)
        try:
            r = requests.post(url, data={
                'url': media_url,
                'caption': body,
                'access_token': access_token,
            }, timeout=20)
            r.raise_for_status()
            payload = r.json()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f'Facebook photo publish failed: '
                f'{exc.response.status_code} {exc.response.text}') from exc
        provider_id = payload.get('post_id') or payload.get('id') or ''
    else:
        # Text-only goes to /feed with the message body.
        url = PAGE_FEED_URL.format(page_id=page_id)
        try:
            r = requests.post(url, data={
                'message': body,
                'access_token': access_token,
            }, timeout=20)
            r.raise_for_status()
            payload = r.json()
        except requests.HTTPError as exc:
            raise RuntimeError(
                f'Facebook feed publish failed: '
                f'{exc.response.status_code} {exc.response.text}') from exc
        provider_id = payload.get('id') or ''

    if not provider_id:
        raise RuntimeError('Facebook accepted the post but did not '
                           'return an id.')

    # Resolve permalink_url (best effort — non-fatal if it fails)
    permalink = ''
    try:
        r = requests.get(
            PAGE_POST_URL.format(post_id=provider_id),
            params={
                'fields': 'permalink_url',
                'access_token': access_token,
            }, timeout=10)
        r.raise_for_status()
        permalink = r.json().get('permalink_url') or ''
    except Exception:  # noqa: BLE001
        logger.warning(
            'Permalink fetch failed for FB post %s — non-fatal',
            provider_id)

    return {'provider_post_id': provider_id, 'permalink': permalink}


def publish_instagram_post(scheduled_post):
    """Publish a ScheduledPost to its bound IG Business account.

    IG REQUIRES a media url — text-only posts aren't supported.
    Returns {'provider_post_id', 'permalink'}.
    Raises RuntimeError on any failure.
    """
    token_row = getattr(scheduled_post.channel, 'token', None)
    if token_row is None:
        raise RuntimeError('No Meta token bound to this channel.')
    ig_id = token_row.provider_account_id
    if not ig_id:
        raise RuntimeError(
            'Channel has no Instagram Business account id on file.')
    access_token = decrypt_token(token_row.access_token_encrypted)
    if not access_token:
        raise RuntimeError(
            'Could not decrypt Page Access Token (used for IG too).')

    media_url = scheduled_post.media_url or ''
    if not media_url:
        raise RuntimeError(
            'Instagram posts require an image URL — text-only is not '
            'supported by the Graph API.')

    # 1. Create the media container
    try:
        r = requests.post(
            IG_MEDIA_URL.format(ig_id=ig_id),
            data={
                'image_url': media_url,
                'caption': scheduled_post.body or '',
                'access_token': access_token,
            }, timeout=20)
        r.raise_for_status()
        container_id = r.json().get('id') or ''
    except requests.HTTPError as exc:
        raise RuntimeError(
            f'IG media container creation failed: '
            f'{exc.response.status_code} {exc.response.text}') from exc

    if not container_id:
        raise RuntimeError('IG accepted the container request but did '
                           'not return an id.')

    # 2. Publish the container. IG processes the media asynchronously
    # so we may need to retry the publish call a couple of times if
    # the container isn't ready yet.
    last_err = None
    for attempt in range(5):
        try:
            r = requests.post(
                IG_PUBLISH_URL.format(ig_id=ig_id),
                data={
                    'creation_id': container_id,
                    'access_token': access_token,
                }, timeout=20)
            r.raise_for_status()
            provider_id = r.json().get('id') or ''
            if provider_id:
                break
        except requests.HTTPError as exc:
            last_err = (f'{exc.response.status_code} '
                        f'{exc.response.text}')
            if exc.response.status_code == 400 and attempt < 4:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s, 8s
                continue
            raise RuntimeError(
                f'IG publish failed: {last_err}') from exc
    else:
        raise RuntimeError(f'IG publish never returned an id: {last_err}')

    # Resolve permalink
    permalink = ''
    try:
        r = requests.get(
            IG_OBJECT_URL.format(media_id=provider_id),
            params={
                'fields': 'permalink',
                'access_token': access_token,
            }, timeout=10)
        r.raise_for_status()
        permalink = r.json().get('permalink') or ''
    except Exception:  # noqa: BLE001
        logger.warning(
            'Permalink fetch failed for IG media %s — non-fatal',
            provider_id)

    return {'provider_post_id': provider_id, 'permalink': permalink}


def refresh_long_lived_token(token_row):
    """Re-exchange the stored long-lived user token for a fresh 60-day
    one. Called by tasks.refresh_expiring_tokens. Updates the row in
    place. Raises RuntimeError on failure (caller writes
    last_refresh_error)."""
    from django.conf import settings
    user_token = decrypt_token(token_row.refresh_token_encrypted)
    if not user_token:
        raise RuntimeError('No stored long-lived user token to refresh.')

    try:
        r = requests.get(
            'https://graph.facebook.com/v19.0/oauth/access_token',
            params={
                'grant_type': 'fb_exchange_token',
                'client_id': getattr(settings, 'META_APP_ID', ''),
                'client_secret': getattr(settings, 'META_APP_SECRET', ''),
                'fb_exchange_token': user_token,
            }, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f'Meta refresh failed: '
            f'{exc.response.status_code} {exc.response.text}') from exc

    new_user_token = payload.get('access_token') or ''
    expires_in = int(payload.get('expires_in') or 60 * 24 * 3600)
    if not new_user_token:
        raise RuntimeError('Meta refresh did not return a new token.')

    # Page Access Token doesn't change — only the user token's lifecycle
    # extends. We do NOT re-fetch the page token here (would require
    # another /me/accounts call); the existing one stays valid as long
    # as the user token does.
    from django.utils import timezone
    import datetime as _dt
    token_row.refresh_token_encrypted = __import__(
        'social.crypto', fromlist=['encrypt_token']
    ).encrypt_token(new_user_token)
    token_row.expires_at = timezone.now() + _dt.timedelta(
        seconds=expires_in - 86400)
    token_row.last_refresh_at = timezone.now()
    token_row.last_refresh_error = ''
    token_row.save(update_fields=[
        'refresh_token_encrypted', 'expires_at',
        'last_refresh_at', 'last_refresh_error', 'updated_at',
    ])
