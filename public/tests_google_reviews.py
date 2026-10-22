"""Live Google reviews: sync stores rating/count/reviews; the home page
quotes displayable ones and falls back to the hand-typed testimonials."""

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from django.urls import reverse

from public import google_reviews
from public.models import GoogleReview, SiteContent


def _api(reviews):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        'rating': 4.9, 'userRatingCount': 7,
        'googleMapsUri': 'https://maps.google.com/?cid=1',
        'reviews': reviews,
    }
    return resp


def _review(name, author, text, rating=5):
    return {
        'name': name, 'rating': rating, 'text': {'text': text},
        'relativePublishTimeDescription': '2 months ago',
        'publishTime': '2026-08-01T12:00:00Z',
        'authorAttribution': {'displayName': author,
                              'uri': 'https://www.google.com/maps/contrib/1'},
    }


@override_settings(GOOGLE_PLACES_API_KEY='k')
class GoogleReviewSyncTests(TestCase):
    def setUp(self):
        sc = SiteContent.get_solo()
        sc.google_place_id = 'PLACE'
        sc.save()

    @patch('public.google_reviews.requests.get')
    def test_sync_stores_reviews_and_totals(self, get):
        get.return_value = _api([
            _review('r1', 'Kat', 'Great site, fast and clean.'),
            _review('r2', 'Hea', 'Helped with our social media too.'),
        ])
        google_reviews.sync()
        sc = SiteContent.get_solo()
        self.assertEqual(sc.google_review_count, 7)
        self.assertEqual(float(sc.google_review_rating), 4.9)
        self.assertTrue(GoogleReview.objects.get(review_name='r1').displayable)
        self.assertFalse(GoogleReview.objects.get(review_name='r2').displayable)

        get.return_value = _api([_review('r1', 'Kat', 'Great site.')])
        google_reviews.sync()
        self.assertEqual(GoogleReview.objects.count(), 1)

    @patch('public.google_reviews.requests.get')
    def test_failed_call_changes_nothing(self, get):
        sc = SiteContent.get_solo()
        sc.google_review_count = 3
        sc.save()
        get.return_value = MagicMock(status_code=403, text='denied')
        with self.assertRaises(google_reviews.GoogleReviewsError):
            google_reviews.sync()
        self.assertEqual(SiteContent.get_solo().google_review_count, 3)

    @patch('public.google_reviews.requests.get')
    def test_home_shows_live_reviews_not_hidden_ones(self, get):
        get.return_value = _api([
            _review('r1', 'Kathryn Test', 'Great site, fast and clean.'),
            _review('r2', 'Hidden Person', 'Helped with our social media.'),
        ])
        google_reviews.sync()
        r = self.client.get(reverse('public:home'))
        self.assertContains(r, 'Kathryn Test')
        self.assertNotContains(r, 'Hidden Person')
        self.assertContains(r, 'Reviews from Google.')
        self.assertNotContains(r, 'Christopher Chilton')

    def test_home_falls_back_without_reviews(self):
        r = self.client.get(reverse('public:home'))
        self.assertContains(r, 'Christopher Chilton')
