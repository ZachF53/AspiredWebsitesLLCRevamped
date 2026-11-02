"""
The reviews endpoint against GHL's standard Webhook action.

As of 2026-11-02 the custom (raw-JSON) webhooks are DELETED in GHL, so
everything arrives in the standard shape: contact fields and location at
the top level, and every value the operator set under ``customData`` --
as a STRING, always.

Four things that bite in that shape, all pinned here:

1. ``trialCall`` arrives as the string "false". ``bool("false")`` is
   True, so a naive read records every one of Hank's real calls as a
   trial and bills nobody. (Voice-side, but the same class of bug.)
2. ``rating`` arrives as "5", and as "" for a review with no stars.
3. A STOP body must still be reclassified to opt_out even though the
   reply workflow sends ``event_type=reply`` explicitly.
4. A STOP from a review contact now arrives TWICE -- once from the reply
   workflow, once from the DND workflow, with different messageIds.
"""
from django.test import TestCase, override_settings
from django.urls import reverse

from reviews.models import ReviewEvent, ReviewProgram

SECRET = 'test-review-secret'
LOC = 'elhJFAz0ZPa2EmAqthti'


def _url(secret=SECRET):
    return reverse('review_ghl_events', args=[secret])


def _program():
    from clients.account_models import Account
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.create_user(
        'o@example.com', 'o@example.com', 'pw')
    account = Account.objects.create(user=user, name='Test Co')
    return ReviewProgram.objects.create(
        account=account, ghl_location_id=LOC, setup_status='live')


@override_settings(REVIEW_GHL_EVENTS_SECRET=SECRET,
                   ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class StandardActionShapeTests(TestCase):

    def setUp(self):
        self.program = _program()

    def _post(self, custom, **top):
        body = {'location': {'id': LOC}, 'customData': custom}
        body.update(top)
        return self.client.post(_url(), data=body,
                                content_type='application/json')

    def test_review_sent_step(self):
        self._post({'event_type': 'sent', 'locationId': LOC,
                    'messageId': 'review-request-sms-c1-2026112',
                    'step': 'sms_review_request', 'source': 'ghl'},
                   phone='+15125551234', full_name='Dale Gribble')
        event = ReviewEvent.objects.get()
        self.assertEqual(event.event_type, 'sent')
        self.assertEqual(event.program_id, self.program.pk)
        self.assertEqual(event.customer_name, 'Dale Gribble')
        self.assertEqual(event.customer_phone_e164, '+15125551234')

    def test_rating_arrives_as_a_string(self):
        self._post({'event_type': 'review_left', 'locationId': LOC,
                    'messageId': 'rev-1', 'rating': '5',
                    'name': 'Peggy H', 'review_text': 'Great work'})
        self.assertEqual(ReviewEvent.objects.get().review_rating, 5)

    def test_an_empty_rating_is_null_not_zero(self):
        """A review with no stars must not read as a 0-star review."""
        self._post({'event_type': 'review_left', 'locationId': LOC,
                    'messageId': 'rev-2', 'rating': '',
                    'review_text': 'No stars given'})
        self.assertIsNone(ReviewEvent.objects.get().review_rating)

    def test_an_out_of_range_rating_is_null(self):
        self._post({'event_type': 'review_left', 'locationId': LOC,
                    'messageId': 'rev-3', 'rating': '0'})
        self.assertIsNone(ReviewEvent.objects.get().review_rating)

    def test_review_text_with_quotes_and_newlines(self):
        """The reason for moving off the custom webhook at all."""
        self._post({'event_type': 'review_left', 'locationId': LOC,
                    'messageId': 'rev-4', 'rating': '5',
                    'review_text': 'He said "best in town".\nEarly too!'})
        event = ReviewEvent.objects.get()
        self.assertIn('best in town', event.review_text)
        self.assertIn('\n', event.review_text)


@override_settings(REVIEW_GHL_EVENTS_SECRET=SECRET,
                   ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class StopReplyReclassificationTests(TestCase):
    """(b) The reply workflow says event_type=reply explicitly."""

    def setUp(self):
        self.program = _program()

    def _reply(self, body, mid='r1', phone='+15125551234'):
        self.client.post(_url(), data={
            'location': {'id': LOC}, 'phone': phone,
            'customData': {'event_type': 'reply', 'locationId': LOC,
                           'messageId': mid, 'body': body},
        }, content_type='application/json')
        return ReviewEvent.objects.order_by('-id').first()

    def test_stop_overrides_an_explicit_reply_event_type(self):
        self.assertEqual(self._reply('STOP').event_type, 'opt_out')

    def test_lowercase_and_padded_stop(self):
        self.assertEqual(self._reply(' stop. ', 'r2').event_type, 'opt_out')

    def test_other_stop_keywords(self):
        for i, word in enumerate(('STOPALL', 'UNSUBSCRIBE', 'CANCEL',
                                  'END', 'QUIT')):
            event = self._reply(word, f'kw{i}', phone=f'+1512555{i:04d}')
            self.assertEqual(event.event_type, 'opt_out', word)

    def test_a_real_reply_stays_a_reply(self):
        event = self._reply('my AC is still broken', 'r3')
        self.assertEqual(event.event_type, 'reply')
        self.assertTrue(event.needs_human)

    def test_stop_inside_a_sentence_stays_a_reply(self):
        self.assertEqual(
            self._reply('stop by tomorrow with the quote', 'r4').event_type,
            'reply')


@override_settings(REVIEW_GHL_EVENTS_SECRET=SECRET,
                   ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class DoubleOptOutTests(TestCase):
    """(c) reply-workflow STOP + DND-workflow opt_out = one person."""

    def setUp(self):
        self.program = _program()

    def _stop_reply(self):
        return self.client.post(_url(), data={
            'location': {'id': LOC}, 'phone': '+15125551234',
            'customData': {'event_type': 'reply', 'locationId': LOC,
                           'messageId': 'review-reply-c1-123',
                           'body': 'STOP'},
        }, content_type='application/json')

    def _dnd(self, mid='dnd-c1-456'):
        return self.client.post(_url(), data={
            'location': {'id': LOC}, 'phone': '+15125551234',
            'customData': {'event_type': 'opt_out', 'locationId': LOC,
                           'messageId': mid},
        }, content_type='application/json')

    def test_both_workflows_produce_one_opt_out(self):
        self._stop_reply()
        resp = self._dnd()
        self.assertEqual(resp.json()['status'], 'duplicate')
        self.assertEqual(
            ReviewEvent.objects.filter(event_type='opt_out').count(), 1)

    def test_order_does_not_matter(self):
        self._dnd()
        self._stop_reply()
        self.assertEqual(
            ReviewEvent.objects.filter(event_type='opt_out').count(), 1)

    def test_a_different_person_is_not_deduped(self):
        self._dnd()
        self.client.post(_url(), data={
            'location': {'id': LOC}, 'phone': '+15125559999',
            'customData': {'event_type': 'opt_out', 'locationId': LOC,
                           'messageId': 'dnd-other'},
        }, content_type='application/json')
        self.assertEqual(
            ReviewEvent.objects.filter(event_type='opt_out').count(), 2)

    def test_an_opt_out_with_no_phone_is_still_recorded(self):
        """Better a duplicate we can see than a suppression we lost."""
        self.client.post(_url(), data={
            'location': {'id': LOC},
            'customData': {'event_type': 'opt_out', 'locationId': LOC,
                           'messageId': 'dnd-nophone'},
        }, content_type='application/json')
        self.assertEqual(
            ReviewEvent.objects.filter(event_type='opt_out').count(), 1)


@override_settings(REVIEW_GHL_EVENTS_SECRET=SECRET,
                   ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ContentlessAndUnknownTests(TestCase):

    def setUp(self):
        self.program = _program()

    def test_an_empty_post_stores_nothing(self):
        """GHL's test button. This used to mint a fake 'sent'."""
        resp = self.client.post(_url(), data={},
                                content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'ignored')
        self.assertEqual(ReviewEvent.objects.count(), 0)

    def test_an_unmapped_type_is_unknown_not_sent(self):
        self.client.post(_url(), data={
            'location': {'id': LOC},
            'customData': {'event_type': 'some_new_step',
                           'locationId': LOC, 'messageId': 'x1'},
        }, content_type='application/json')
        self.assertEqual(ReviewEvent.objects.get().event_type, 'unknown')

    def test_a_payload_with_only_a_location_is_still_stored(self):
        """Has content, just no type. Diagnosable, not discarded."""
        self.client.post(_url(), data={'location': {'id': LOC}},
                         content_type='application/json')
        self.assertEqual(ReviewEvent.objects.get().event_type, 'unknown')

    def test_failed_still_maps_to_failed(self):
        self.client.post(_url(), data={
            'location': {'id': LOC},
            'customData': {'event_type': 'send_failed', 'locationId': LOC,
                           'messageId': 'f1'},
        }, content_type='application/json')
        self.assertEqual(ReviewEvent.objects.get().event_type, 'failed')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class OptOutStatsScopeTests(TestCase):
    """(d) DND fires account-wide; review stats must not absorb it."""

    def setUp(self):
        self.program = _program()

    def _event(self, event_type, phone, **kw):
        from django.utils import timezone
        return ReviewEvent.objects.create(
            program=self.program, event_type=event_type,
            customer_phone_e164=phone, occurred_at=timezone.now(),
            dedupe_key=f'{event_type}:{phone}:{kw.get("n", "")}', **{})

    def test_only_opt_outs_from_people_we_messaged_are_counted(self):
        from reviews.reporting import window_stats
        # A review contact: sent, then opted out.
        self._event('sent', '+15125551111')
        self._event('opt_out', '+15125551111')
        # Someone who never got a review request, opting out of texts
        # account-wide via the untaggable DND trigger.
        self._event('opt_out', '+15125559999')

        stats = window_stats(self.program)['d30']
        self.assertEqual(stats['sent'], 1)
        self.assertEqual(stats['opt_outs'], 1,
                         'the account-wide opt-out must not be counted '
                         'against review requests')
        self.assertEqual(
            ReviewEvent.objects.filter(event_type='opt_out').count(), 2,
            'both are still stored -- consent applies to every message')

    def test_unknown_events_are_counted_in_nothing(self):
        from reviews.reporting import window_stats
        self._event('unknown', '+15125552222')
        stats = window_stats(self.program)['d30']
        for key in ('sent', 'reviews', 'clicks', 'replies', 'opt_outs'):
            self.assertEqual(stats[key], 0, key)
