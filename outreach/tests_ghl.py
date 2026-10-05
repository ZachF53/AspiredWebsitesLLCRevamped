"""
Tests for outreach/ghl.py — the GoHighLevel contact-upsert client.

No test here makes a real network call; `requests.post` is always
mocked. Live verification against the real sub-account is manual (see
claude-code-website-spec.md Job 1) — the automated suite must never
depend on a third-party API being reachable.
"""

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from outreach import ghl


class ToE164Tests(TestCase):

    def test_ten_digit_us_number(self):
        self.assertEqual(ghl.to_e164('2105550100'), '+12105550100')

    def test_formatted_number(self):
        self.assertEqual(ghl.to_e164('(210) 555-0100'), '+12105550100')

    def test_leading_country_code(self):
        self.assertEqual(ghl.to_e164('12105550100'), '+12105550100')

    def test_already_e164(self):
        self.assertEqual(ghl.to_e164('+12105550100'), '+12105550100')

    def test_blank_returns_none(self):
        self.assertIsNone(ghl.to_e164(''))
        self.assertIsNone(ghl.to_e164(None))

    def test_unparseable_returns_none_not_empty_string(self):
        # Too short to be a real number — must be None, distinguishable
        # from "parsed to an empty value", so callers know to preserve
        # the raw input rather than silently drop the phone.
        self.assertIsNone(ghl.to_e164('123'))


class BuildPayloadTests(TestCase):

    def test_tags_is_always_exactly_website_lead(self):
        """tags OVERWRITES the whole set on a GHL update — sending
        anything but ['website-lead'] here would clobber tags a human
        added in the GHL UI on the next form resubmission."""
        payload = ghl.build_payload(first_name='Jane')
        self.assertEqual(payload['tags'], ['website-lead'])

    def test_valid_phone_becomes_e164_field(self):
        payload = ghl.build_payload(
            first_name='Jane', phone_raw='(210) 555-0100')
        self.assertEqual(payload['phone'], '+12105550100')
        self.assertNotIn(
            'phone_raw',
            {f.get('key') for f in payload['customFields']},
        )

    def test_unparseable_phone_falls_back_to_custom_field(self):
        """Spec: 'keep the raw value in a custom field and send the
        contact without a phone rather than dropping the lead.'"""
        payload = ghl.build_payload(first_name='Jane', phone_raw='not-a-phone')
        self.assertNotIn('phone', payload)
        custom = {f['key']: f['fieldValue'] for f in payload['customFields']}
        self.assertEqual(custom['phone_raw'], 'not-a-phone')

    def test_blank_custom_field_values_are_omitted(self):
        payload = ghl.build_payload(
            first_name='Jane',
            custom_fields={'utm_source': 'google', 'utm_medium': ''},
        )
        keys = {f['key'] for f in payload['customFields']}
        self.assertIn('utm_source', keys)
        self.assertNotIn('utm_medium', keys)

    @override_settings(GHL_CUSTOM_FIELD_IDS={'utm_source': 'abc123'})
    def test_custom_field_id_used_when_configured(self):
        """Once Zachery supplies real GHL field ids, the id route
        (documented-reliable) must be used instead of key."""
        payload = ghl.build_payload(
            first_name='Jane', custom_fields={'utm_source': 'google'})
        entry = next(
            f for f in payload['customFields'] if f.get('id') == 'abc123')
        self.assertEqual(entry['fieldValue'], 'google')
        self.assertNotIn('key', entry)

    def test_locationid_present(self):
        payload = ghl.build_payload(first_name='Jane')
        self.assertIn('locationId', payload)


@override_settings(GHL_API_KEY='test-token')
class PushContactTests(TestCase):

    @patch('outreach.ghl.requests.post')
    def test_success_returns_json_body(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200, content=b'{"ok": true}',
            json=lambda: {'ok': True})
        result = ghl.push_contact({'firstName': 'Jane'})
        self.assertEqual(result, {'ok': True})

    @patch('outreach.ghl.requests.post')
    def test_4xx_raises_ghlerror(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=400, text='bad request', content=b'bad request')
        with self.assertRaises(ghl.GHLError):
            ghl.push_contact({'firstName': 'Jane'})

    @patch('outreach.ghl.time.sleep')
    @patch('outreach.ghl.requests.post')
    def test_429_retries_then_succeeds(self, mock_post, mock_sleep):
        rate_limited = MagicMock(
            status_code=429, headers={'Retry-After': '1'})
        ok = MagicMock(
            status_code=200, content=b'{"ok": true}', json=lambda: {'ok': True})
        mock_post.side_effect = [rate_limited, ok]
        result = ghl.push_contact({'firstName': 'Jane'})
        self.assertEqual(result, {'ok': True})
        mock_sleep.assert_called_once()

    @patch('outreach.ghl.requests.post')
    def test_network_error_raises_ghlerror(self, mock_post):
        import requests
        mock_post.side_effect = requests.ConnectionError('refused')
        with self.assertRaises(ghl.GHLError):
            ghl.push_contact({'firstName': 'Jane'})

    @override_settings(GHL_API_KEY='')
    def test_missing_token_raises_not_configured(self):
        with self.assertRaises(ghl.GHLNotConfigured):
            ghl.push_contact({'firstName': 'Jane'})


@override_settings(GHL_API_KEY='test-token')
class SyncLeadToGhlTests(TestCase):

    @patch('outreach.ghl.push_contact')
    def test_success_returns_contact_id_and_trace_id(self, mock_push):
        mock_push.return_value = {
            'contact': {'id': 'abc123'}, 'traceId': 'trace-1'}
        contact_id, trace_id = ghl.sync_lead_to_ghl(
            first_name='Jane', email='jane@example.com')
        self.assertEqual(contact_id, 'abc123')
        self.assertEqual(trace_id, 'trace-1')

    @patch('outreach.ghl.push_contact')
    def test_failure_attaches_payload_to_exception(self, mock_push):
        """Callers (public.views._push_lead_to_ghl) rely on exc.payload
        to populate FailedLeadSubmission without rebuilding the body."""
        mock_push.side_effect = ghl.GHLError('boom')
        with self.assertRaises(ghl.GHLError) as ctx:
            ghl.sync_lead_to_ghl(first_name='Jane', email='jane@example.com')
        self.assertTrue(hasattr(ctx.exception, 'payload'))
        self.assertEqual(ctx.exception.payload['firstName'], 'Jane')
