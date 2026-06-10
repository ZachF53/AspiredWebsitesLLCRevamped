"""
Phase 5c — LinkedIn organization post publisher.

LinkedIn's v2 post API (UGC Posts) takes JSON of shape:
    {
      "author": "urn:li:organization:NNN",
      "lifecycleState": "PUBLISHED",
      "specificContent": {
        "com.linkedin.ugc.ShareContent": {
          "shareCommentary": {"text": "..."},
          "shareMediaCategory": "NONE" | "IMAGE",
          "media": [{"status":"READY","media":"urn:li:digitalmediaAsset:..."}]
        }
      },
      "visibility": {
        "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
      }
    }

For images we have to use the multi-step asset upload:
  1. registerUpload → get uploadUrl + asset URN
  2. PUT the image bytes to uploadUrl
  3. Post-create UGC with shareMediaCategory=IMAGE + the asset URN

For 5c MVP we ship text + image-by-URL: we download the image,
register an upload, PUT the bytes, then create the UGC post. Failures
fall back to a text-only post with the image URL appended.
"""

import logging

import requests

from .crypto import decrypt_token

logger = logging.getLogger(__name__)


UGC_URL = 'https://api.linkedin.com/v2/ugcPosts'
ASSET_REGISTER_URL = (
    'https://api.linkedin.com/v2/assets?action=registerUpload')


def _headers(access_token):
    return {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'X-Restli-Protocol-Version': '2.0.0',
    }


def _upload_image(access_token, org_urn, image_url):
    """Register + upload an image, return its asset URN. Raises
    RuntimeError on any step failure."""
    # 1. Register the upload
    try:
        r = requests.post(
            ASSET_REGISTER_URL,
            headers=_headers(access_token),
            json={
                'registerUploadRequest': {
                    'owner': org_urn,
                    'recipes': [
                        'urn:li:digitalmediaRecipe:feedshare-image'
                    ],
                    'serviceRelationships': [{
                        'identifier': 'urn:li:userGeneratedContent',
                        'relationshipType': 'OWNER',
                    }],
                },
            }, timeout=15)
        r.raise_for_status()
        payload = r.json().get('value') or {}
    except requests.HTTPError as exc:
        raise RuntimeError(
            f'LinkedIn registerUpload failed: '
            f'{exc.response.status_code} {exc.response.text}') from exc

    upload_url = (((payload.get('uploadMechanism') or {})
                  .get('com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest') or {})
                  .get('uploadUrl'))
    asset_urn = payload.get('asset')
    if not upload_url or not asset_urn:
        raise RuntimeError(
            'LinkedIn registerUpload returned no uploadUrl / asset.')

    # 2. Fetch the source image
    try:
        img_resp = requests.get(image_url, timeout=20)
        img_resp.raise_for_status()
        image_bytes = img_resp.content
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f'Could not fetch source image at {image_url}: {exc}') from exc

    # 3. PUT bytes to the LinkedIn upload URL
    try:
        r = requests.put(
            upload_url,
            data=image_bytes,
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=30)
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f'LinkedIn image upload failed: '
            f'{exc.response.status_code} {exc.response.text}') from exc

    return asset_urn


def publish_linkedin_post(scheduled_post):
    """Publish a ScheduledPost to its bound LinkedIn organization page.

    Returns {'provider_post_id', 'permalink'}.
    Raises RuntimeError on any failure.
    """
    token_row = getattr(scheduled_post.channel, 'token', None)
    if token_row is None:
        raise RuntimeError('No LinkedIn token bound to this channel.')
    org_urn = token_row.provider_account_id
    if not org_urn:
        raise RuntimeError('Channel has no LinkedIn org URN on file.')
    access_token = decrypt_token(token_row.access_token_encrypted)
    if not access_token:
        raise RuntimeError(
            'Could not decrypt LinkedIn access token.')

    body = scheduled_post.body or ''
    media_url = scheduled_post.media_url or ''

    media_block = []
    share_category = 'NONE'
    if media_url:
        try:
            asset_urn = _upload_image(access_token, org_urn, media_url)
            media_block = [{
                'status': 'READY',
                'description': {'text': ''},
                'media': asset_urn,
                'title': {'text': ''},
            }]
            share_category = 'IMAGE'
        except RuntimeError as exc:
            # Fall back to text-only with the URL appended — better
            # than failing the whole post.
            logger.warning(
                'LinkedIn image upload failed (%s); falling back to '
                'text-only with URL appended.', exc)
            body = f'{body}\n\n{media_url}'.strip()

    ugc_payload = {
        'author': org_urn,
        'lifecycleState': 'PUBLISHED',
        'specificContent': {
            'com.linkedin.ugc.ShareContent': {
                'shareCommentary': {'text': body},
                'shareMediaCategory': share_category,
                'media': media_block,
            },
        },
        'visibility': {
            'com.linkedin.ugc.MemberNetworkVisibility': 'PUBLIC',
        },
    }

    try:
        r = requests.post(
            UGC_URL, headers=_headers(access_token),
            json=ugc_payload, timeout=20)
        r.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f'LinkedIn UGC post failed: '
            f'{exc.response.status_code} {exc.response.text}') from exc

    # The UGC id is returned in the X-RestLi-Id header (urn:li:share:NNN)
    # or in the body for some responses. Use the header when available.
    post_urn = (r.headers.get('X-RestLi-Id')
                or r.headers.get('x-restli-id')
                or (r.json() or {}).get('id', ''))
    if not post_urn:
        raise RuntimeError('LinkedIn UGC post returned no id.')

    # Build a permalink. LinkedIn doesn't return one in the API
    # response, but the standard share URL follows this format.
    permalink = ''
    if post_urn.startswith('urn:li:share:'):
        share_id = post_urn.split(':')[-1]
        permalink = (
            f'https://www.linkedin.com/feed/update/urn:li:share:'
            f'{share_id}/')
    elif post_urn.startswith('urn:li:ugcPost:'):
        permalink = (
            f'https://www.linkedin.com/feed/update/{post_urn}/')

    return {'provider_post_id': post_urn, 'permalink': permalink}
