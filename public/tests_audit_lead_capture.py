"""
The free audit is a lead capture (Oct 2026).

What this pins:

  1. The four scores stay FREE and ungated. That is the thing people
     link to and the only reason the page converts, so a change that
     hides them behind the form is a regression, not a tightening.

  2. The plain-English Aspired AI summary is gated, and gated on the
     SERVER. /audit/ai-review/ is a real URL; hiding the HTMX trigger in
     the template while leaving the endpoint open would let anyone read
     the summary and burn an Anthropic call doing it.

  3. A capture produces a workable CRM Lead, not just an AuditLead.
     CLAUDE.md has specified source='audit_tool' with the scores copied
     across since the beginning; until this work the audit funnel only
     ever wrote AuditLead rows, so the leads were invisible to the CRM.

  4. An anonymous audit creates NO Lead. No contact details means
     nothing actionable -- same rule, stated in CLAUDE.md.

GHL is stubbed throughout. settings_test blanks the credentials, but a
test that reached the network would be both slow and a live write.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from outreach.models import Lead
from public.models import AuditLead


SCORES = {
    'performance': 41,
    'seo': 78,
    'best_practices': 83,
    'accessibility': 64,
}
ISSUES = {'performance': ['Serve images in a modern format'], 'seo': []}

GOOD_POST = {
    'name': 'Dale Gribble',
    'email': 'dale@dalesdeadbug.com',
    'phone': '(210) 555-0123',
    'company': "Dale's Dead Bug HVAC",
}


@override_settings(PRODUCTION_HOST='aspiredwebsites.com',
                   ALLOWED_HOSTS=['aspiredwebsites.com', 'testserver'],
                   SECURE_SSL_REDIRECT=False)
class AuditLeadCaptureTests(TestCase):

    def _seed_session(self, email_submitted=False):
        """Put a finished audit in the session, as _run_pagespeed_audit
        would, so the results page renders without calling PageSpeed."""
        session = self.client.session
        session['audit_url'] = 'https://dalesdeadbug.example'
        session['audit_scores'] = SCORES
        session['audit_issues'] = ISSUES
        session['audit_details'] = {}
        if email_submitted:
            session['audit_email_submitted'] = True
        session.save()

    # ── 1. scores stay free ──────────────────────────────────────────
    def test_scores_are_visible_without_submitting_anything(self):
        self._seed_session()
        html = self.client.get('/audit/results/').content.decode()
        for score in ('41', '78', '83', '64'):
            self.assertIn(score, html)
        for label in ('Performance', 'SEO', 'Best Practices',
                      'Accessibility'):
            self.assertIn(label, html)

    # ── 2. the summary is gated, server-side ─────────────────────────
    def test_ai_review_endpoint_is_gated_before_capture(self):
        self._seed_session()
        html = self.client.get('/audit/ai-review/').content.decode()
        self.assertIn('Email me the full report', html)
        self.assertNotIn('ai-review-body', html)

    @patch('public.views._generate_ai_audit_review',
           return_value='Your site is slow.\n\nHere is why.')
    def test_ai_review_endpoint_opens_after_capture(self, _gen):
        self._seed_session(email_submitted=True)
        with override_settings(ANTHROPIC_API_KEY='sk-test'):
            html = self.client.get('/audit/ai-review/').content.decode()
        self.assertIn('ai-review-body', html)
        self.assertIn('Your site is slow.', html)

    @patch('public.views._generate_ai_audit_review')
    def test_gated_request_never_calls_anthropic(self, gen):
        """The gate must come before generation, not after. Checking it
        only in the template would still bill a call per request."""
        self._seed_session()
        with override_settings(ANTHROPIC_API_KEY='sk-test'):
            self.client.get('/audit/ai-review/')
        gen.assert_not_called()

    # ── 3. a capture creates a CRM lead ──────────────────────────────
    @patch('public.views.ghl_client.sync_lead_to_ghl',
           return_value=('ghl-contact-1', 'trace-1'))
    @patch('public.audit_sequence.send_report')
    def test_capture_creates_audit_lead_and_crm_lead(self, _send, _ghl):
        self._seed_session()
        resp = self.client.post('/audit/results/', GOOD_POST)
        self.assertRedirects(resp, '/audit/results/')

        al = AuditLead.objects.get()
        self.assertEqual(al.email, 'dale@dalesdeadbug.com')
        self.assertEqual(al.name, 'Dale Gribble')
        self.assertEqual(al.company, "Dale's Dead Bug HVAC")
        self.assertTrue(al.phone)

        lead = Lead.objects.get()
        self.assertEqual(lead.source, 'audit_tool')
        self.assertEqual(lead.business_type, 'HVAC')
        self.assertEqual(lead.email, 'dale@dalesdeadbug.com')
        self.assertEqual(lead.attorney_name, 'Dale Gribble')
        self.assertEqual(lead.firm_name, "Dale's Dead Bug HVAC")
        # Scores copied across, per CLAUDE.md.
        self.assertEqual(lead.website_performance_score, 41)
        self.assertEqual(lead.website_seo_score, 78)
        self.assertEqual(lead.website_best_practices_score, 83)
        self.assertEqual(lead.ghl_contact_id, 'ghl-contact-1')
        self.assertIsNotNone(lead.pushed_to_ghl_at)

    @patch('public.views.ghl_client.sync_lead_to_ghl',
           return_value=('c', 't'))
    @patch('public.audit_sequence.send_report')
    def test_phone_is_optional(self, _send, _ghl):
        self._seed_session()
        post = dict(GOOD_POST, phone='')
        resp = self.client.post('/audit/results/', post)
        self.assertRedirects(resp, '/audit/results/')
        self.assertEqual(AuditLead.objects.get().phone, '')
        self.assertEqual(Lead.objects.count(), 1)

    @patch('public.audit_sequence.send_report')
    def test_name_and_company_are_required(self, _send):
        self._seed_session()
        resp = self.client.post(
            '/audit/results/', {'email': 'a@b.com', 'phone': '', 'company': '',
                                'name': ''})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AuditLead.objects.exists())
        self.assertFalse(Lead.objects.exists())
        # The visitor has to be told why nothing happened -- the old
        # markup only rendered email errors, so a missing company
        # re-rendered a silent form.
        html = resp.content.decode()
        self.assertIn('Enter your company name.', html)
        self.assertIn('Enter your name', html)

    @patch('public.views.ghl_client.sync_lead_to_ghl',
           side_effect=Exception('boom'))
    @patch('public.audit_sequence.send_report')
    def test_a_ghl_outage_still_keeps_the_lead(self, _send, _ghl):
        """The visitor already saw their report; a CRM outage must not
        turn the capture into an error page or lose the row."""
        self._seed_session()
        resp = self.client.post('/audit/results/', GOOD_POST)
        self.assertRedirects(resp, '/audit/results/')
        self.assertEqual(AuditLead.objects.count(), 1)
        self.assertEqual(Lead.objects.count(), 1)

    # ── 4. anonymous audits stay anonymous ───────────────────────────
    def test_running_an_audit_alone_creates_no_lead(self):
        self._seed_session()
        self.client.get('/audit/results/')
        self.assertFalse(Lead.objects.exists())
        self.assertFalse(AuditLead.objects.exists())
