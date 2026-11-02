"""
Phase 3: one canonical audit field set, written by both audit paths.

The two paths reach GHL by different mechanisms and that is the whole
reason this is delicate:

  * The web form upserts a CONTACT, so its audit data must be written as
    contact custom fields, addressed by field id.
  * The voice task posts to a WEBHOOK-TRIGGER, whose body is consumed by
    a workflow that texts the caller. A workflow's field mapping is a
    snapshot taken when its sample was fetched, so renaming a key in
    that body breaks the mapping SILENTLY -- the text still sends, with
    blanks where the scores should be.

So the voice body keeps its original bare keys forever, and canonical
naming happens only where contact fields are written. These tests pin
both halves of that, because the failure mode on the voice side produces
no error anywhere.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from public import tasks as audit_tasks
from public.views import psi_final_url_and_lcp


def _psi(final_url='https://example.com/', lcp_ms=2400.4,
         requested='https://example.com'):
    lh = {'requestedUrl': requested, 'finalUrl': final_url, 'audits': {}}
    if lcp_ms is not None:
        lh['audits']['largest-contentful-paint'] = {'numericValue': lcp_ms}
    return {'lighthouseResult': lh}


class PsiExtractorTests(TestCase):
    """One extractor, used by both paths, so they cannot disagree."""

    def test_pulls_final_url_and_rounds_lcp(self):
        self.assertEqual(
            psi_final_url_and_lcp(_psi()),
            ('https://example.com/', 2400))

    def test_missing_lcp_is_none_not_zero(self):
        """0 ms would read as instant. Absent must stay absent."""
        _, lcp = psi_final_url_and_lcp(_psi(lcp_ms=None))
        self.assertIsNone(lcp)

    def test_unparseable_lcp_is_none(self):
        _, lcp = psi_final_url_and_lcp(_psi(lcp_ms='not-a-number'))
        self.assertIsNone(lcp)

    def test_falls_back_through_requested_url_then_argument(self):
        payload = {'lighthouseResult': {'requestedUrl': 'https://r.test'}}
        self.assertEqual(psi_final_url_and_lcp(payload)[0], 'https://r.test')
        self.assertEqual(
            psi_final_url_and_lcp({}, requested='https://arg.test')[0],
            'https://arg.test')

    def test_empty_payload_never_raises(self):
        self.assertEqual(psi_final_url_and_lcp(None), ('', None))


@override_settings(GHL_API_KEY='k', GHL_LOCATION_ID='loc')
class VoiceContactWriteTests(TestCase):
    """The voice path persists canonical fields on the contact."""

    PAYLOAD = {
        'status': 'ready',
        'url': 'https://example.com',
        'final_url': 'https://example.com/',
        'performance_score': 41,
        'scores': {'seo': 78, 'accessibility': 64, 'best_practices': 83},
        'lcp_ms': 2400,
        'summary': 'example.com has 3 areas that need attention.',
    }

    @patch('outreach.ghl.update_contact_custom_fields')
    def test_writes_all_ten_canonical_fields(self, upd):
        audit_tasks._write_audit_fields_to_contact('c1', self.PAYLOAD)
        upd.assert_called_once()
        contact_id, fields = upd.call_args[0]
        self.assertEqual(contact_id, 'c1')
        self.assertEqual(fields['audit_url'], 'https://example.com')
        self.assertEqual(fields['audit_final_url'], 'https://example.com/')
        self.assertEqual(fields['audit_performance_score'], '41')
        self.assertEqual(fields['audit_seo_score'], '78')
        self.assertEqual(fields['audit_accessibility_score'], '64')
        self.assertEqual(fields['audit_best_practices_score'], '83')
        self.assertEqual(fields['audit_lcp_ms'], '2400')
        self.assertEqual(fields['audit_status'], 'ready')
        self.assertEqual(fields['audit_summary'], self.PAYLOAD['summary'])
        self.assertRegex(fields['audit_checked_at'], r'^\d{4}-\d{2}-\d{2}$')

    @patch('outreach.ghl.update_contact_custom_fields')
    def test_no_contact_id_is_a_noop(self, upd):
        """A voice audit for an unknown caller has nothing to write to."""
        audit_tasks._write_audit_fields_to_contact(None, self.PAYLOAD)
        audit_tasks._write_audit_fields_to_contact('', self.PAYLOAD)
        upd.assert_not_called()

    @patch('outreach.ghl.update_contact_custom_fields',
           side_effect=Exception('ghl down'))
    def test_a_failed_write_never_raises(self, _upd):
        """The caller has already been texted. A CRM write failing after
        that must not turn a delivered result into an error."""
        audit_tasks._write_audit_fields_to_contact('c1', self.PAYLOAD)

    @patch('outreach.ghl.update_contact_custom_fields')
    def test_missing_metrics_are_dropped_not_zeroed(self, upd):
        payload = dict(self.PAYLOAD, lcp_ms=None, scores={})
        audit_tasks._write_audit_fields_to_contact('c1', payload)
        fields = upd.call_args[0][1]
        self.assertEqual(fields['audit_lcp_ms'], '')
        self.assertEqual(fields['audit_seo_score'], '')


class VoiceWebhookBodyIsFrozenTests(TestCase):
    """The webhook body's keys must not drift.

    GHL snapshots a workflow's field mapping. If these keys are renamed,
    the live SMS keeps sending with blank scores and nothing errors --
    which is exactly why this is a test and not a comment.
    """

    FROZEN = ('url', 'final_url', 'performance_score', 'lcp_ms',
              'lcp_seconds', 'crux', 'summary', 'status')

    def test_the_mapped_keys_are_still_the_bare_names(self):
        import inspect
        src = inspect.getsource(audit_tasks.run_audit_for_voice)
        for key in self.FROZEN:
            self.assertIn(
                f"'{key}'", src,
                f"the voice webhook body no longer sets {key!r}; GHL's "
                f"workflow mapping is a snapshot and will silently send "
                f"blanks for it")

    def test_the_three_extra_scores_are_nested_not_top_level(self):
        """They ride in `scores` so the workflow sample is unchanged."""
        import inspect
        src = inspect.getsource(audit_tasks.run_audit_for_voice)
        self.assertIn("'scores': {", src)
        for bare in ("'seo_score':", "'accessibility_score':",
                     "'best_practices_score':"):
            self.assertNotIn(
                bare, src,
                'adding a top-level score key changes the webhook sample '
                'shape; nest it under "scores" instead')


class AuditCallbackRetryTests(TestCase):

    @override_settings(GHL_AUDIT_WEBHOOK_URL='https://hooks.test/audit')
    @patch('public.tasks.requests.post')
    def test_a_transient_5xx_is_retried_once(self, post):
        post.side_effect = [type('R', (), {'status_code': 502})(),
                            type('R', (), {'status_code': 200})()]
        audit_tasks._post_audit_callback({'status': 'ready'}, 'c1', '+1555')
        self.assertEqual(post.call_count, 2)

    @override_settings(GHL_AUDIT_WEBHOOK_URL='https://hooks.test/audit')
    @patch('public.tasks.requests.post')
    def test_a_4xx_is_not_retried(self, post):
        """A rejected body or a wrong URL gets the same answer twice."""
        post.return_value = type('R', (), {'status_code': 400})()
        audit_tasks._post_audit_callback({'status': 'ready'}, 'c1', '+1555')
        self.assertEqual(post.call_count, 1)

    @override_settings(GHL_AUDIT_WEBHOOK_URL='https://hooks.test/audit')
    @patch('public.tasks.requests.post')
    def test_success_does_not_retry(self, post):
        post.return_value = type('R', (), {'status_code': 200})()
        audit_tasks._post_audit_callback({'status': 'ready'}, None, None)
        self.assertEqual(post.call_count, 1)

    @override_settings(GHL_AUDIT_WEBHOOK_URL='')
    @patch('public.tasks.requests.post')
    def test_unset_webhook_is_a_skip_not_an_error(self, post):
        audit_tasks._post_audit_callback({'status': 'ready'}, 'c1', '+1555')
        post.assert_not_called()

    @override_settings(GHL_AUDIT_WEBHOOK_URL='https://hooks.test/audit')
    @patch('public.tasks.requests.post')
    def test_connection_errors_are_retried_once_then_given_up(self, post):
        import requests as rq
        post.side_effect = rq.ConnectionError('boom')
        audit_tasks._post_audit_callback({'status': 'ready'}, 'c1', '+1555')
        self.assertEqual(post.call_count, 2)
