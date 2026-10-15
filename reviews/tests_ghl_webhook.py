"""
The GHL events webhook — the one way anything gets into the dashboard.

Two properties matter more than the happy path:

1. **It fails closed.** An unset ``REVIEW_GHL_EVENTS_SECRET`` must 403
   everything rather than accept anything, and a bad secret must write
   nothing at all — an unauthenticated POST that still creates a row is
   a free write primitive.
2. **It is idempotent and always 200 once stored.** GHL retries non-2xx,
   so a duplicate or an interpretation failure that answered 4xx/5xx
   would become an infinite redelivery loop.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from clients.account_models import Account
from reviews.entitlement import program_for
from reviews.models import ReviewEvent

User = get_user_model()

SECRET = 'test-ghl-events-secret-value'

_seq = 0


def _program(**kwargs):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'rvhook{_seq}', email=f'rvhook{_seq}@example.com',
        password='test-pass-123')
    account = Account.objects.create(
        user=user, name=f'Hook Co {_seq}', onboarding_status='complete')
    program = program_for(account, create=True)
    for field, value in kwargs.items():
        setattr(program, field, value)
    if kwargs:
        program.save()
    return program


def _url(secret=SECRET):
    return reverse('review_ghl_events', args=[secret])


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
                   REVIEW_GHL_EVENTS_SECRET=SECRET)
class AuthTests(TestCase):

    def test_valid_secret_stores_one_event(self):
        program = _program(ghl_location_id='loc_abc')
        resp = self.client.post(
            _url(),
            data=json.dumps({'event_type': 'sent', 'locationId': 'loc_abc',
                             'messageId': 'm1', 'phone': '2105550134'}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ReviewEvent.objects.count(), 1)
        event = ReviewEvent.objects.get()
        self.assertEqual(event.program_id, program.pk)
        self.assertEqual(event.event_type, 'sent')
        self.assertEqual(event.customer_phone_e164, '+12105550134')

    def test_bad_secret_is_403_and_writes_nothing(self):
        _program(ghl_location_id='loc_abc')
        resp = self.client.post(
            _url('wrong-secret'),
            data=json.dumps({'event_type': 'sent', 'locationId': 'loc_abc'}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(ReviewEvent.objects.count(), 0, (
            'an unauthenticated POST created a row — that is a free write '
            'primitive'))

    def test_malformed_json_is_400(self):
        resp = self.client.post(
            _url(), data='not json at all',
            content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(ReviewEvent.objects.count(), 0)

    def test_a_json_list_is_rejected(self):
        resp = self.client.post(
            _url(), data=json.dumps([1, 2, 3]),
            content_type='application/json')
        self.assertEqual(resp.status_code, 400)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(_url()).status_code, 405)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
                   REVIEW_GHL_EVENTS_SECRET='')
class FailsClosedTests(TestCase):

    def test_unset_secret_rejects_everything(self):
        """Including an empty path segment, which would otherwise
        compare equal to an empty expected secret."""
        for attempt in ('anything', SECRET):
            resp = self.client.post(
                _url(attempt), data=json.dumps({'event_type': 'sent'}),
                content_type='application/json')
            self.assertEqual(resp.status_code, 403, attempt)
        self.assertEqual(ReviewEvent.objects.count(), 0)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
                   REVIEW_GHL_EVENTS_SECRET=SECRET)
class IdempotencyAndRoutingTests(TestCase):

    def _post(self, payload):
        return self.client.post(
            _url(), data=json.dumps(payload),
            content_type='application/json')

    def test_replay_is_200_duplicate_and_stores_one_row(self):
        _program(ghl_location_id='loc_abc')
        payload = {'event_type': 'sent', 'locationId': 'loc_abc',
                   'messageId': 'msg-1'}
        first = self._post(payload)
        second = self._post(payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()['status'], 'duplicate')
        self.assertEqual(ReviewEvent.objects.count(), 1)

    def test_payloads_without_an_id_still_dedupe_on_content(self):
        _program(ghl_location_id='loc_abc')
        payload = {'event_type': 'click', 'locationId': 'loc_abc',
                   'phone': '2105550134', 'timestamp': '2026-10-14T10:00:00Z'}
        self._post(payload)
        self._post(payload)
        self.assertEqual(ReviewEvent.objects.count(), 1)

    def test_unresolvable_location_is_stored_with_an_error_and_200(self):
        resp = self._post({'event_type': 'sent', 'locationId': 'nobody'})
        self.assertEqual(resp.status_code, 200)
        event = ReviewEvent.objects.get()
        self.assertIsNone(event.program_id)
        self.assertIn('nobody', event.error)

    def test_falls_back_to_matching_the_sending_number(self):
        program = _program(sending_number='+14785550134')
        self._post({'event_type': 'sent', 'to': '(478) 555-0134',
                    'messageId': 'm9'})
        self.assertEqual(ReviewEvent.objects.get().program_id, program.pk)

    def test_nested_customer_keys_are_read(self):
        _program(ghl_location_id='loc_abc')
        self._post({
            'event_type': 'reply', 'locationId': 'loc_abc', 'messageId': 'm2',
            'customer': {'name': 'Dana Ruiz', 'phone': '2105550134',
                         'email': 'dana@example.com'},
            'body': 'my AC is still broken',
        })
        event = ReviewEvent.objects.get()
        self.assertEqual(event.customer_name, 'Dana Ruiz')
        self.assertEqual(event.customer_email, 'dana@example.com')

    def test_an_unmapped_event_type_is_stored_not_dropped(self):
        _program(ghl_location_id='loc_abc')
        resp = self._post({'event_type': 'something_new',
                           'locationId': 'loc_abc', 'messageId': 'm3'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ReviewEvent.objects.count(), 1)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
                   REVIEW_GHL_EVENTS_SECRET=SECRET)
class ReplyClassificationTests(TestCase):
    """GHL and the carrier own STOP. Our job is only to avoid telling the
    client a keyword is a conversation that needs answering."""

    def _reply(self, body, index):
        _program(ghl_location_id=f'loc_{index}')
        self.client.post(
            _url(),
            data=json.dumps({'event_type': 'reply',
                             'locationId': f'loc_{index}',
                             'messageId': f'm{index}', 'body': body}),
            content_type='application/json')
        return ReviewEvent.objects.get(dedupe_key=f'reply:m{index}')

    def test_a_real_message_needs_a_human(self):
        self.assertTrue(self._reply('my AC is still broken', 1).needs_human)

    def test_stop_does_not_need_a_human(self):
        self.assertFalse(self._reply('STOP', 2).needs_human)

    def test_stop_with_punctuation_and_case_still_matches(self):
        self.assertFalse(self._reply(' stop. ', 3).needs_human)

    def test_help_does_not_need_a_human(self):
        self.assertFalse(self._reply('HELP', 4).needs_human)

    def test_an_empty_body_needs_nobody(self):
        self.assertFalse(self._reply('', 5).needs_human)

    def test_a_sent_event_never_needs_a_human(self):
        _program(ghl_location_id='loc_s')
        self.client.post(
            _url(),
            data=json.dumps({'event_type': 'sent', 'locationId': 'loc_s',
                             'messageId': 'ms', 'body': 'anything'}),
            content_type='application/json')
        self.assertFalse(ReviewEvent.objects.get().needs_human)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
                   REVIEW_GHL_EVENTS_SECRET=SECRET)
class ReviewRatingTests(TestCase):

    def test_a_rating_is_recorded_for_reporting(self):
        _program(ghl_location_id='loc_r')
        self.client.post(
            _url(),
            data=json.dumps({'event_type': 'review_left',
                             'locationId': 'loc_r', 'messageId': 'mr',
                             'rating': 5}),
            content_type='application/json')
        self.assertEqual(ReviewEvent.objects.get().review_rating, 5)

    def test_an_out_of_range_rating_is_discarded_rather_than_stored(self):
        _program(ghl_location_id='loc_r2')
        self.client.post(
            _url(),
            data=json.dumps({'event_type': 'review_left',
                             'locationId': 'loc_r2', 'messageId': 'mr2',
                             'rating': 99}),
            content_type='application/json')
        self.assertIsNone(ReviewEvent.objects.get().review_rating)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class StatusEndpointTests(TestCase):
    """The endpoint that makes the portal Pause button real — the GHL
    workflow calls it before sending."""

    def test_reports_paused_and_live(self):
        program = _program(setup_status='live')
        url = reverse('review_program_status', args=[program.public_key])
        body = self.client.get(url).json()
        self.assertFalse(body['paused'])
        self.assertTrue(body['live'])

        program.sending_paused = True
        program.save(update_fields=['sending_paused'])
        self.assertTrue(self.client.get(url).json()['paused'])

    def test_unknown_key_is_404_not_500(self):
        import uuid
        resp = self.client.get(
            reverse('review_program_status', args=[uuid.uuid4()]))
        self.assertEqual(resp.status_code, 404)

    def test_response_is_not_cacheable(self):
        """A cached 'not paused' would keep messages going out after the
        client pressed Pause — the exact failure this prevents."""
        program = _program(setup_status='live')
        resp = self.client.get(
            reverse('review_program_status', args=[program.public_key]))
        self.assertIn('no-cache', resp.headers.get('Cache-Control', ''))


class RedactionTests(TestCase):
    """This app stores our clients' customers' phone numbers and emails.
    None of it may reach a log line, an alert, or an export."""

    def test_phones_and_emails_are_masked(self):
        from reviews.redaction import redact_payload
        out = redact_payload({
            'phone': '+12105550134',
            'customer': {'email': 'dana@example.com', 'name': 'Dana'},
            'notes': ['nothing sensitive'],
        })
        self.assertNotIn('5550134', str(out['phone']))
        self.assertIn('0134', out['phone'])
        self.assertEqual(out['customer']['email'], 'd***@example.com')
        self.assertEqual(out['customer']['name'], 'Dana')
        self.assertEqual(out['notes'], ['nothing sensitive'])

    def test_secrets_are_masked_too(self):
        from reviews.redaction import redact_payload
        out = redact_payload({'token': 'pit-abc123', 'ein': '123456789'})
        self.assertNotIn('abc123', str(out['token']))
        self.assertNotIn('12345', str(out['ein']))

    def test_deeply_nested_payloads_do_not_recurse_forever(self):
        from reviews.redaction import redact_payload
        payload = current = {}
        for _ in range(40):
            current['next'] = {}
            current = current['next']
        self.assertIsNotNone(redact_payload(payload))
