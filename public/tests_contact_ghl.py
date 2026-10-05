"""
Contact form <-> GoHighLevel integration — claude-code-website-spec.md
Job 1. outreach.ghl.sync_lead_to_ghl is always mocked; a GHL outage
must never cost a lead, and that guarantee has to hold whether or not
the real API is reachable from the test runner.
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
class ContactGhlIntegrationTests(TestCase):

    def setUp(self):
        cache.clear()

    def _payload(self, **overrides):
        from public.views import _signed_form_timestamp
        body = {
            'name': 'Jane Tester',
            'phone': '210-555-0100',
            'email': 'jane@tester.example',
            'message': 'We need a new website.',
            'form_timestamp': _signed_form_timestamp(),
        }
        body.update(overrides)
        return body

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_successful_push_stamps_lead(self, mock_sync, mock_age):
        mock_sync.return_value = ('ghl-contact-1', 'trace-1')
        r = self.client.post(reverse('public:contact'), data=self._payload())
        self.assertEqual(r.status_code, 302)
        lead = Lead.objects.get(email='jane@tester.example')
        self.assertEqual(lead.ghl_contact_id, 'ghl-contact-1')
        self.assertIsNotNone(lead.pushed_to_ghl_at)
        self.assertEqual(FailedLeadSubmission.objects.count(), 0)

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_ghl_failure_still_creates_lead_and_shows_success(
            self, mock_sync, mock_age):
        """The core guarantee: a GHL outage must never cost a lead, and
        the visitor must never see anything other than success."""
        exc = ghl.GHLError('GHL unreachable: timeout')
        exc.payload = {'firstName': 'Jane Tester'}
        mock_sync.side_effect = exc

        r = self.client.post(reverse('public:contact'), data=self._payload())

        self.assertEqual(r.status_code, 302)
        self.assertIn('thanks', r['Location'])
        lead = Lead.objects.get(email='jane@tester.example')
        self.assertEqual(lead.ghl_contact_id, '')
        self.assertIsNone(lead.pushed_to_ghl_at)

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_ghl_failure_writes_failed_submission_and_fallback_email(
            self, mock_sync, mock_age):
        exc = ghl.GHLError('GHL unreachable: timeout')
        exc.payload = {'firstName': 'Jane Tester'}
        mock_sync.side_effect = exc

        self.client.post(reverse('public:contact'), data=self._payload())

        failed = FailedLeadSubmission.objects.get()
        self.assertEqual(failed.lead.email, 'jane@tester.example')
        self.assertIn('timeout', failed.error)
        self.assertEqual(failed.payload, {'firstName': 'Jane Tester'})

        fallback_emails = [
            m for m in mail.outbox
            if 'fallback@aspiredwebsites.com' in m.to
        ]
        self.assertEqual(len(fallback_emails), 1)
        self.assertIn('GHL push failed', fallback_emails[0].subject)

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_ghl_not_configured_is_handled_the_same_way(
            self, mock_sync, mock_age):
        """No GHL_API_KEY set locally/on a fresh box must degrade the
        same way as a live outage — not a 500, not a lost lead."""
        mock_sync.side_effect = ghl.GHLNotConfigured('GHL_API_KEY is not set.')
        r = self.client.post(reverse('public:contact'), data=self._payload())
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Lead.objects.filter(
            email='jane@tester.example').count(), 1)
        self.assertEqual(FailedLeadSubmission.objects.count(), 1)

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_sms_consent_defaults_false_and_is_not_required(
            self, mock_sync, mock_age):
        mock_sync.return_value = ('ghl-contact-2', 'trace-2')
        # sms_consent intentionally omitted — the box must not be
        # required to submit (documented carrier rejection cause).
        r = self.client.post(reverse('public:contact'), data=self._payload())
        self.assertEqual(r.status_code, 302)
        custom_fields = mock_sync.call_args.kwargs['custom_fields']
        self.assertEqual(custom_fields['sms_consent'], 'false')

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_sms_consent_checked_is_forwarded_as_true(
            self, mock_sync, mock_age):
        mock_sync.return_value = ('ghl-contact-3', 'trace-3')
        r = self.client.post(
            reverse('public:contact'),
            data=self._payload(sms_consent='true'))
        self.assertEqual(r.status_code, 302)
        custom_fields = mock_sync.call_args.kwargs['custom_fields']
        self.assertEqual(custom_fields['sms_consent'], 'true')

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_business_name_maps_to_firm_name_and_ghl_company_name(
            self, mock_sync, mock_age):
        mock_sync.return_value = ('ghl-contact-4', 'trace-4')
        self.client.post(
            reverse('public:contact'),
            data=self._payload(business_name='Doe Heating & Air'))
        lead = Lead.objects.get(email='jane@tester.example')
        self.assertEqual(lead.firm_name, 'Doe Heating & Air')
        self.assertEqual(
            mock_sync.call_args.kwargs['company_name'], 'Doe Heating & Air')

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_utm_fields_forwarded_as_custom_fields(self, mock_sync, mock_age):
        mock_sync.return_value = ('ghl-contact-5', 'trace-5')
        self.client.post(reverse('public:contact'), data=self._payload(
            utm_source='google', utm_medium='cpc',
            landing_page='https://aspiredwebsites.com/',
        ))
        custom_fields = mock_sync.call_args.kwargs['custom_fields']
        self.assertEqual(custom_fields['utm_source'], 'google')
        self.assertEqual(custom_fields['utm_medium'], 'cpc')
        self.assertEqual(
            custom_fields['landing_page'], 'https://aspiredwebsites.com/')

    @patch('public.views._form_age_seconds', return_value=(10, True))
    @patch('outreach.ghl.sync_lead_to_ghl')
    def test_honeypot_submission_never_reaches_ghl(self, mock_sync, mock_age):
        """Spam layers run before the GHL push — a bot must never
        consume a GHL API call or create a FailedLeadSubmission."""
        r = self.client.post(reverse('public:contact'), data=self._payload(
            website_url='http://botland'))
        self.assertEqual(r.status_code, 302)
        mock_sync.assert_not_called()
        self.assertEqual(FailedLeadSubmission.objects.count(), 0)
