"""
"Call me back" form <-> GoHighLevel integration (2026-10-06).

Same guarantees as public/tests_contact_ghl.py: outreach.ghl.sync_lead_to_ghl
is always mocked (no real network call), and a GHL outage must never
cost a lead or surface anything but success to the visitor.
"""

from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from outreach import ghl
from outreach.models import FailedLeadSubmission, Lead


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    LEAD_FALLBACK_EMAIL='fallback@aspiredwebsites.com',
)
class CallbackGhlIntegrationTests(TestCase):

    def setUp(self):
        cache.clear()

    def _payload(self, **overrides):
        from public.views import _signed_form_timestamp
        body = {
            'name': 'Pat Jones',
            'phone': '(478) 555-0142',
            'best_time': 'Afternoon (12pm-5pm)',
            'website_url': '',
            'form_timestamp': _signed_form_timestamp(),
        }
        body.update(overrides)
        return body

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_successful_push_stamps_lead(self, mock_sync, mock_age):
        mock_sync.return_value = ('ghl-contact-cb1', 'trace-cb1')
        r = self.client.post('/callback/', data=self._payload())
        self.assertEqual(r.status_code, 302)
        lead = Lead.objects.get()
        self.assertEqual(lead.ghl_contact_id, 'ghl-contact-cb1')
        self.assertIsNotNone(lead.pushed_to_ghl_at)
        self.assertEqual(FailedLeadSubmission.objects.count(), 0)

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_tag_and_source_are_callback_specific(self, mock_sync, mock_age):
        """The tag is the trigger — 'callback-request', not
        'website-lead'. Source distinguishes it from the contact form
        in GHL's own reporting."""
        mock_sync.return_value = ('ghl-contact-cb2', 'trace-cb2')
        self.client.post('/callback/', data=self._payload())
        self.assertEqual(
            mock_sync.call_args.kwargs['tag'], 'callback-request')
        self.assertEqual(
            mock_sync.call_args.kwargs['source'], 'website-callback-form')

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_name_is_split_for_ghl_first_last_name(self, mock_sync, mock_age):
        mock_sync.return_value = ('ghl-contact-cb3', 'trace-cb3')
        self.client.post('/callback/', data=self._payload(name='Dave Moreno'))
        self.assertEqual(mock_sync.call_args.kwargs['first_name'], 'Dave')
        self.assertEqual(mock_sync.call_args.kwargs['last_name'], 'Moreno')

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_best_time_value_forwarded_exactly(self, mock_sync, mock_age):
        """Byte-exact match matters here — GHL silently drops any
        value that doesn't match an existing SINGLE_OPTIONS choice."""
        mock_sync.return_value = ('ghl-contact-cb4', 'trace-cb4')
        self.client.post('/callback/', data=self._payload(
            best_time='Morning (8am-12pm)'))
        custom_fields = mock_sync.call_args.kwargs['custom_fields']
        self.assertEqual(custom_fields['best_time'], 'Morning (8am-12pm)')

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_blank_best_time_forwarded_as_blank_not_relabeled(
            self, mock_sync, mock_age):
        """Spec: blank is allowed and the GHL workflow treats it as
        "call now" — Django must send the literal blank, not the
        'As soon as possible' label used for internal display only."""
        mock_sync.return_value = ('ghl-contact-cb5', 'trace-cb5')
        self.client.post('/callback/', data=self._payload(best_time=''))
        custom_fields = mock_sync.call_args.kwargs['custom_fields']
        self.assertEqual(custom_fields['best_time'], '')

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_source_page_and_attribution_forwarded(self, mock_sync, mock_age):
        mock_sync.return_value = ('ghl-contact-cb6', 'trace-cb6')
        self.client.post('/callback/', data=self._payload(
            source_page='/pricing/',
            landing_page='https://aspiredwebsites.com/pricing/',
            referrer='https://www.google.com/',
        ))
        custom_fields = mock_sync.call_args.kwargs['custom_fields']
        self.assertEqual(custom_fields['source_page'], '/pricing/')
        self.assertEqual(
            custom_fields['landing_page'],
            'https://aspiredwebsites.com/pricing/')
        self.assertEqual(
            custom_fields['referrer'], 'https://www.google.com/')

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_no_utm_or_project_fields_sent(self, mock_sync, mock_age):
        """The callback form has no ad-campaign or project-qualifier
        fields — only best_time/source_page/landing_page/referrer
        belong in its customFields, per the GHL spec."""
        mock_sync.return_value = ('ghl-contact-cb7', 'trace-cb7')
        self.client.post('/callback/', data=self._payload())
        custom_fields = mock_sync.call_args.kwargs['custom_fields']
        self.assertEqual(
            set(custom_fields),
            {'best_time', 'source_page', 'landing_page', 'referrer'})

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_ghl_failure_still_creates_lead_and_shows_success(
            self, mock_sync, mock_age):
        exc = ghl.GHLError('GHL unreachable: timeout')
        exc.payload = {'firstName': 'Pat'}
        mock_sync.side_effect = exc

        r = self.client.post('/callback/', data=self._payload())

        self.assertEqual(r.status_code, 302)
        self.assertIn('thanks', r['Location'])
        lead = Lead.objects.get()
        self.assertEqual(lead.ghl_contact_id, '')
        self.assertIsNone(lead.pushed_to_ghl_at)

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_ghl_failure_writes_failed_submission_and_fallback_email(
            self, mock_sync, mock_age):
        exc = ghl.GHLError('tag endpoint down')
        exc.payload = {'contact_id': 'abc123', 'tags': ['callback-request']}
        mock_sync.side_effect = exc

        self.client.post('/callback/', data=self._payload())

        failed = FailedLeadSubmission.objects.get()
        self.assertIn('tag endpoint down', failed.error)
        self.assertEqual(failed.payload['contact_id'], 'abc123')

        fallback_emails = [
            m for m in mail.outbox if 'fallback@aspiredwebsites.com' in m.to
        ]
        self.assertEqual(len(fallback_emails), 1)

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_honeypot_submission_never_reaches_ghl(self, mock_sync, mock_age):
        r = self.client.post('/callback/', data=self._payload(
            website_url='http://botland'))
        self.assertEqual(r.status_code, 302)
        mock_sync.assert_not_called()
        self.assertEqual(FailedLeadSubmission.objects.count(), 0)

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_invalid_best_time_rejected_before_reaching_ghl(
            self, mock_sync, mock_age):
        """Free text (the pre-dropdown behaviour) must fail Django
        validation, not reach GHL as a value that will be silently
        dropped."""
        r = self.client.post('/callback/', data=self._payload(
            best_time='whenever works I guess'))
        self.assertEqual(r.status_code, 400)
        mock_sync.assert_not_called()
