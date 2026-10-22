"""
Live Google reviews for Aspired's own listing.

Once a day (public.tasks.sync_google_reviews_task, also
`manage.py sync_google_reviews`) this asks the Places API (New) for the
listing in SiteContent.google_place_id and stores:

  * the rating and total review count on SiteContent, which every
    "Read our N Google reviews (5.0 stars)" link already reads;
  * the reviews Google returns (at most five) as GoogleReview rows,
    which the home page quotes word for word.

A failed call changes nothing, so the site keeps the last good numbers.
Cost: one Place Details call with reviews per day, well inside the free
monthly credit.

Display rules: review text is never edited. A review whose text names a
discontinued service (the public content gate's forbidden phrases, e.g.
"social media") is stored with displayable=False -- still counted in the
total, just not quoted on the site.
"""

import logging

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

PLACES_URL = 'https://places.googleapis.com/v1/places/{place_id}'
FIELD_MASK = 'rating,userRatingCount,reviews,googleMapsUri'


class GoogleReviewsError(Exception):
    pass


def _displayable(text):
    from public.content_gate import _PHRASES
    for label, phrases in _PHRASES.items():
        if label.startswith(('B ', 'C ')):
            if any(p in text for p in phrases):
                return False
    return True


def fetch(place_id):
    key = getattr(settings, 'GOOGLE_PLACES_API_KEY', '')
    if not key:
        raise GoogleReviewsError('GOOGLE_PLACES_API_KEY is not set')
    try:
        r = requests.get(
            PLACES_URL.format(place_id=place_id),
            headers={'X-Goog-Api-Key': key, 'X-Goog-FieldMask': FIELD_MASK},
            timeout=20)
    except requests.RequestException as err:
        raise GoogleReviewsError(f'Places API unreachable: {err}')
    if r.status_code != 200:
        raise GoogleReviewsError(
            f'Places API returned {r.status_code}: {r.text[:200]}')
    return r.json()


def sync():
    """Refresh SiteContent rating/count and GoogleReview rows.

    Returns a short status string. Raises GoogleReviewsError on failure.
    """
    from public.models import GoogleReview, SiteContent

    sc = SiteContent.get_solo()
    if not sc.google_place_id:
        return 'skipped: no google_place_id in Site Content'

    data = fetch(sc.google_place_id)
    reviews = data.get('reviews') or []

    with transaction.atomic():
        keep = []
        for rv in reviews:
            name = rv.get('name')
            if not name:
                continue
            author = rv.get('authorAttribution') or {}
            text = ((rv.get('originalText') or rv.get('text') or {})
                    .get('text') or '').strip()
            GoogleReview.objects.update_or_create(
                review_name=name,
                defaults={
                    'author_name': (author.get('displayName') or 'A Google user')[:200],
                    'author_uri': (author.get('uri') or '')[:500],
                    'rating': int(rv.get('rating') or 0),
                    'text': text,
                    'relative_time': (rv.get('relativePublishTimeDescription') or '')[:60],
                    'published_at': parse_datetime(rv.get('publishTime') or ''),
                    'displayable': _displayable(text),
                })
            keep.append(name)
        GoogleReview.objects.exclude(review_name__in=keep).delete()

        fields = ['google_reviews_synced_at']
        sc.google_reviews_synced_at = timezone.now()
        if data.get('userRatingCount') is not None:
            sc.google_review_count = int(data['userRatingCount'])
            fields.append('google_review_count')
        if data.get('rating') is not None:
            sc.google_review_rating = round(float(data['rating']), 1)
            fields.append('google_review_rating')
        if not sc.google_reviews_url and data.get('googleMapsUri'):
            sc.google_reviews_url = data['googleMapsUri']
            fields.append('google_reviews_url')
        sc.save(update_fields=fields)

    return (f'ok: rating {sc.google_review_rating}, '
            f'{sc.google_review_count} reviews, {len(keep)} stored')
