"""
Opt-out classification and the standard-Webhook-action shape.

GHL has no opt-out step wired yet, so a STOP reply currently arrives as
``event_type=reply``. Left alone it would sit in the portal as a reply
needing a human answer, and consent state would be invisible -- which
for SMS is a compliance question, not a display one.

So a reply whose WHOLE body is a carrier stop keyword is recorded as
``opt_out``. Whole-body only: "stop by tomorrow with the quote" is a
customer talking.
"""
from django.test import TestCase, override_settings
from django.urls import reverse

from reviews.models import ReviewEvent

SECRET = 'test-review-secret'


def _url(secret=SECRET):
    return reverse('review_ghl_events', args=[secret])


@override_settings(REVIEW_GHL_EVENTS_SECRET=SECRET,
                   ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class OptOutClassificationTests(TestCase):

    def _post(self, payload):
        return self.client.post(_url(), data=payload,
                                content_type='application/json')

    def _reply(self, body):
        self._post({
            'event_type': 'reply',
            'locationId': 'elhJFAz0ZPa2EmAqthti',
            'messageId': f'msg-{body[:12]}',
            'phone': '+15125551234',
            'body': body,
        })
        return ReviewEvent.objects.order_by('-id').first()

    def test_a_bare_stop_becomes_an_opt_out(self):
        for body in ('STOP', 'stop', ' Stop ', 'UNSUBSCRIBE', 'CANCEL',
                     'quit', 'END', 'STOPALL'):
            ReviewEvent.objects.all().delete()
            event = self._reply(body)
            self.assertEqual(event.event_type, 'opt_out', f'{body!r}')

    def test_a_sentence_mentioning_stop_stays_a_reply(self):
        for body in ('stop by tomorrow with the quote',
                     'please cancel my 2pm',
                     'can you stop the service call'):
            ReviewEvent.objects.all().delete()
            event = self._reply(body)
            self.assertEqual(event.event_type, 'reply', f'{body!r}')

    def test_an_explicit_opt_out_event_type_needs_no_body(self):
        """So a real GHL DND trigger works without the text arriving."""
        self._post({
            'event_type': 'opt_out',
            'locationId': 'elhJFAz0ZPa2EmAqthti',
            'messageId': 'dnd-1',
            'phone': '+15125551234',
        })
        self.assertEqual(
            ReviewEvent.objects.get().event_type, 'opt_out')

    def test_a_review_comment_is_never_reclassified(self):
        """A 1-star review reading 'cancel' is not an opt-out."""
        self._post({
            'event_type': 'review_left',
            'locationId': 'elhJFAz0ZPa2EmAqthti',
            'messageId': 'rev-1',
            'rating': '1',
            'review_text': 'cancel',
            'body': 'cancel',
        })
        self.assertEqual(ReviewEvent.objects.get().event_type, 'review_left')

    def test_delivered_and_failed_are_accepted(self):
        for raw, want in (('delivered', 'delivered'), ('failed', 'failed'),
                          ('send_failed', 'failed')):
            ReviewEvent.objects.all().delete()
            self._post({
                'event_type': raw,
                'locationId': 'elhJFAz0ZPa2EmAqthti',
                'messageId': f'id-{raw}',
                'phone': '+15125551234',
            })
            self.assertEqual(ReviewEvent.objects.get().event_type, want,
                             f'{raw!r}')


@override_settings(REVIEW_GHL_EVENTS_SECRET=SECRET,
                   ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class StandardWebhookShapeTests(TestCase):
    """GHL's free standard action, which serialises JSON properly."""

    def test_nested_location_and_message_body_are_read(self):
        resp = self.client.post(_url(), data={
            'contact_id': 'c-1',
            'full_name': 'Dale Gribble',
            'phone': '+15125551234',
            'email': 'dale@example.com',
            'location': {'id': 'elhJFAz0ZPa2EmAqthti'},
            'message': {'body': 'STOP'},
            'customData': {'event_type': 'reply', 'messageId': 'std-1'},
        }, content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        event = ReviewEvent.objects.get()
        # message.body read, so the STOP is seen and reclassified.
        self.assertEqual(event.event_type, 'opt_out')
        self.assertEqual(event.ghl_location_id, 'elhJFAz0ZPa2EmAqthti')
        self.assertEqual(event.customer_name, 'Dale Gribble')

    def test_text_with_quotes_and_newlines_survives(self):
        """The whole point of moving off the custom webhook: a review
        comment containing a quote or a newline must not 400."""
        resp = self.client.post(_url(), data={
            'location': {'id': 'elhJFAz0ZPa2EmAqthti'},
            'customData': {
                'event_type': 'review_left', 'messageId': 'quoted-1',
                'rating': '5',
                'review_text': 'He said "best in town".\nShowed up early!',
            },
        }, content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        event = ReviewEvent.objects.get()
        self.assertEqual(event.event_type, 'review_left')
        self.assertIn('best in town', event.review_text)
        self.assertEqual(event.review_rating, 5)

    def test_the_existing_custom_body_still_works(self):
        """Six custom webhooks are live; they must not break."""
        resp = self.client.post(_url(), data={
            'event_type': 'sent',
            'locationId': 'elhJFAz0ZPa2EmAqthti',
            'messageId': 'review-request-sms-c1-2026112',
            'phone': '+15125551234',
            'name': 'Dale Gribble',
            'step': 'sms_review_request',
        }, content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ReviewEvent.objects.get().event_type, 'sent')

    def test_malformed_json_is_400_not_a_retry_loop(self):
        resp = self.client.post(_url(), data=b'{"broken": "unescaped " quote"}',
                                content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(ReviewEvent.objects.count(), 0)

    @override_settings(REVIEW_GHL_EVENTS_SECRET='')
    def test_unset_secret_fails_closed(self):
        resp = self.client.post(_url(), data={'event_type': 'sent'},
                                content_type='application/json')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(ReviewEvent.objects.count(), 0)
