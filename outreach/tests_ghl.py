"""
Tests for outreach/ghl.py — the GoHighLevel API client (contact
upsert + additive tagging).

No test here makes a real network call; `requests.post` is always
mocked. Live verification against the real sub-account is manual (see
claude-code-website-spec.md Job 1 and the 2026-10-06 callback-form
extension) — the automated suite must never depend on a third-party
API being reachable.

BuildPayloadTests runs with GHL_CUSTOM_FIELD_IDS cleared so the
payload-shape assertions don't depend on the specific field ids
settings.py happens to have configured for the live sub-account.
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


class SplitNameTests(TestCase):
    """GHL's {{contact.first_name}} merge field needs an actual first
    name, not "Dave Moreno" shoved whole into firstName."""

    def test_two_word_name(self):
        self.assertEqual(ghl.split_name('Dave Moreno'), ('Dave', 'Moreno'))

    def test_single_word_name(self):
        self.assertEqual(ghl.split_name('Cher'), ('Cher', ''))

    def test_three_word_name_remainder_is_last_name(self):
        self.assertEqual(
            ghl.split_name('Mary Jane Watson'), ('Mary', 'Jane Watson'))

    def test_extra_whitespace_is_collapsed(self):
        self.assertEqual(
            ghl.split_name('  Dave   Moreno  '), ('Dave', 'Moreno'))

    def test_blank_returns_two_empty_strings(self):
        self.assertEqual(ghl.split_name(''), ('', ''))
        self.assertEqual(ghl.split_name(None), ('', ''))


@override_settings(GHL_CUSTOM_FIELD_IDS={})
class BuildPayloadTests(TestCase):

    def test_tags_is_never_in_the_upsert_payload(self):
        """GHL REPLACES the entire tag array on upsert — an existing
        contact tagged ["booked","client","hvac"] resubmitting a form
        would come back as just the one tag sent here. Tagging is a
        separate, additive call (add_tags) made AFTER the upsert, so
        'tags' must never appear in this payload at all."""
        payload = ghl.build_payload(first_name='Jane')
        self.assertNotIn('tags', payload)

    def test_source_defaults_to_contact_form(self):
        payload = ghl.build_payload(first_name='Jane')
        self.assertEqual(payload['source'], 'website-contact-form')

    def test_source_is_overridable(self):
        payload = ghl.build_payload(
            first_name='Jane', source='website-callback-form')
        self.assertEqual(payload['source'], 'website-callback-form')

    def test_valid_phone_becomes_e164_field(self):
        payload = ghl.build_payload(
            first_name='Jane', phone_raw='(210) 555-0100')
        self.assertEqual(payload['phone'], '+12105550100')
        values = [f['fieldValue'] for f in payload['customFields']]
        self.assertNotIn('(210) 555-0100', values)

    def test_unparseable_phone_falls_back_to_custom_field(self):
        """Spec: 'keep the raw value in a custom field and send the
        contact without a phone rather than dropping the lead.'"""
        payload = ghl.build_payload(first_name='Jane', phone_raw='not-a-phone')
        self.assertNotIn('phone', payload)
        values = [f['fieldValue'] for f in payload['customFields']]
        self.assertIn('not-a-phone', values)

    def test_blank_custom_field_values_are_omitted(self):
        payload = ghl.build_payload(
            first_name='Jane',
            custom_fields={'utm_source': 'google', 'utm_medium': ''},
        )
        keys = {f['key'] for f in payload['customFields']}
        self.assertIn('utm_source', keys)
        self.assertNotIn('utm_medium', keys)

    def test_custom_field_sent_bare_key_by_default(self):
        """Spec: 'Send keys BARE — "best_time", never "contact.best_time".'"""
        payload = ghl.build_payload(
            first_name='Jane', custom_fields={'best_time': 'morning'})
        entry = next(f for f in payload['customFields'])
        self.assertEqual(entry['key'], 'best_time')
        self.assertNotIn('contact.', entry['key'])

    @override_settings(GHL_CUSTOM_FIELD_IDS={'utm_source': 'abc123'})
    def test_custom_field_id_used_when_configured(self):
        """Once a GHL field id is known, the id route (documented-
        reliable) is used instead of key."""
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
class AddTagsTests(TestCase):

    @patch('outreach.ghl.requests.post')
    def test_success_posts_to_contact_tags_endpoint(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=201, content=b'{"tagsAdded": ["callback-request"]}',
            json=lambda: {'tagsAdded': ['callback-request']})
        result = ghl.add_tags('contact123', ['callback-request'])
        self.assertEqual(result['tagsAdded'], ['callback-request'])
        called_url = mock_post.call_args.args[0]
        self.assertIn('contacts/contact123/tags', called_url)
        called_body = mock_post.call_args.kwargs['json']
        self.assertEqual(called_body, {'tags': ['callback-request']})

    @patch('outreach.ghl.requests.post')
    def test_empty_tags_added_means_already_tagged(self, mock_post):
        """Spec: empty tagsAdded = contact already had the tag — a
        repeat request from someone already mid-flow. Not an error."""
        mock_post.return_value = MagicMock(
            status_code=201, content=b'{"tagsAdded": []}',
            json=lambda: {'tagsAdded': []})
        result = ghl.add_tags('contact123', ['callback-request'])
        self.assertEqual(result['tagsAdded'], [])

    @patch('outreach.ghl.requests.post')
    def test_4xx_raises_ghlerror(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=404, text='not found', content=b'not found')
        with self.assertRaises(ghl.GHLError):
            ghl.add_tags('contact123', ['callback-request'])


@override_settings(GHL_API_KEY='test-token')
class SyncLeadToGhlTests(TestCase):

    def _mock_upsert(self, mock_push, contact_id='abc123', trace_id='trace-1'):
        mock_push.return_value = {
            'contact': {'id': contact_id}, 'traceId': trace_id}

    @patch('outreach.ghl.add_tags')
    @patch('outreach.ghl.push_contact')
    def test_success_returns_contact_id_and_trace_id(
            self, mock_push, mock_add_tags):
        self._mock_upsert(mock_push)
        mock_add_tags.return_value = {'tagsAdded': ['website-lead']}
        contact_id, trace_id = ghl.sync_lead_to_ghl(
            first_name='Jane', email='jane@example.com')
        self.assertEqual(contact_id, 'abc123')
        self.assertEqual(trace_id, 'trace-1')

    @patch('outreach.ghl.add_tags')
    @patch('outreach.ghl.push_contact')
    def test_tag_param_is_applied_additively_after_upsert(
            self, mock_push, mock_add_tags):
        self._mock_upsert(mock_push)
        mock_add_tags.return_value = {'tagsAdded': ['callback-request']}
        ghl.sync_lead_to_ghl(
            first_name='Jane', tag='callback-request',
            source='website-callback-form')
        mock_add_tags.assert_called_once_with('abc123', ['callback-request'])

    @patch('outreach.ghl.add_tags')
    @patch('outreach.ghl.push_contact')
    def test_source_forwarded_to_upsert_payload(
            self, mock_push, mock_add_tags):
        self._mock_upsert(mock_push)
        mock_add_tags.return_value = {'tagsAdded': ['callback-request']}
        ghl.sync_lead_to_ghl(
            first_name='Jane', source='website-callback-form')
        sent_payload = mock_push.call_args.args[0]
        self.assertEqual(sent_payload['source'], 'website-callback-form')

    @patch('outreach.ghl.add_tags')
    @patch('outreach.ghl.push_contact')
    def test_already_tagged_does_not_raise(self, mock_push, mock_add_tags):
        """Empty tagsAdded (repeat submission from someone already
        mid-flow) is a successful, quiet no-op — not a failure."""
        self._mock_upsert(mock_push)
        mock_add_tags.return_value = {'tagsAdded': []}
        contact_id, trace_id = ghl.sync_lead_to_ghl(first_name='Jane')
        self.assertEqual(contact_id, 'abc123')

    @patch('outreach.ghl.push_contact')
    def test_upsert_failure_attaches_payload_to_exception(self, mock_push):
        """Callers (public.views._push_lead_to_ghl) rely on exc.payload
        to populate FailedLeadSubmission without rebuilding the body."""
        mock_push.side_effect = ghl.GHLError('boom')
        with self.assertRaises(ghl.GHLError) as ctx:
            ghl.sync_lead_to_ghl(first_name='Jane', email='jane@example.com')
        self.assertTrue(hasattr(ctx.exception, 'payload'))
        self.assertEqual(ctx.exception.payload['firstName'], 'Jane')

    @patch('outreach.ghl.add_tags')
    @patch('outreach.ghl.push_contact')
    def test_tag_failure_also_raises_with_contact_id_in_payload(
            self, mock_push, mock_add_tags):
        """The tag IS the automation trigger — a contact that exists
        but didn't get tagged must be treated as a full failure, not a
        partial success, so a human finds out via FailedLeadSubmission.
        The attached payload carries the contact_id so the tag can be
        applied by hand from the GHL UI."""
        self._mock_upsert(mock_push, contact_id='abc123')
        mock_add_tags.side_effect = ghl.GHLError('tag endpoint down')
        with self.assertRaises(ghl.GHLError) as ctx:
            ghl.sync_lead_to_ghl(first_name='Jane', tag='callback-request')
        self.assertEqual(ctx.exception.payload['contact_id'], 'abc123')
        self.assertEqual(ctx.exception.payload['tags'], ['callback-request'])
