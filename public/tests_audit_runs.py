"""AuditRun: every audit run is logged, linked to an email request when
there is one, and backfillable from session recordings."""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from public.models import AuditLead, AuditRun
from public.tests import _fake_pagespeed_result


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    GOOGLE_PAGESPEED_API_KEY='',
)
class AuditRunLoggingTests(TestCase):
    def setUp(self):
        cache.clear()

    @patch('public.views._run_pagespeed_audit')
    def test_anonymous_run_is_logged_without_identifier(self, mock_run):
        mock_run.return_value = _fake_pagespeed_result()
        self.client.post(reverse('public:audit'),
                         data={'url': 'https://www.example.com/'})
        run = AuditRun.objects.get()
        self.assertEqual(run.domain, 'example.com')
        self.assertIsNone(run.audit_lead)
        self.assertFalse(run.by_staff)
        self.assertEqual(run.source, 'live')

    @patch('public.views._run_pagespeed_audit')
    def test_email_request_links_to_run(self, mock_run):
        mock_run.return_value = _fake_pagespeed_result()
        self.client.post(reverse('public:audit'),
                         data={'url': 'https://example.com'})
        self.client.post(reverse('public:audit_results'),
                         data={'email': 'visitor@example.com'})
        run = AuditRun.objects.get()
        self.assertEqual(run.audit_lead.email, 'visitor@example.com')

    @patch('public.views._run_pagespeed_audit')
    def test_staff_run_is_flagged(self, mock_run):
        mock_run.return_value = _fake_pagespeed_result()
        staff = get_user_model().objects.create_user(
            'staffer', 's@example.com', 'pw', is_staff=True)
        self.client.force_login(staff)
        self.client.post(reverse('public:audit'),
                         data={'url': 'https://example.com'})
        self.assertTrue(AuditRun.objects.get().by_staff)


class BackfillAuditRunsTests(TestCase):
    def _recording(self, title, when):
        from reporting.models import SessionRecording
        r = SessionRecording.objects.create(
            session_id='s', page_title=title,
            page_url='https://aspiredwebsites.com/audit/results/',
            expires_at=when + timedelta(days=30))
        SessionRecording.objects.filter(pk=r.pk).update(created_at=when)

    def _lead(self, url, when, email='a@example.com'):
        lead = AuditLead.objects.create(
            url=url, performance_score=50, seo_score=60,
            best_practices_score=70, accessibility_score=80, email=email)
        AuditLead.objects.filter(pk=lead.pk).update(created_at=when)
        return lead

    def test_recordings_collapse_reloads_and_link_leads(self):
        t = timezone.now() - timedelta(days=5)
        self._recording('Audit Results for https://foo.com/ | Aspired Websites', t)
        self._recording('Audit Results for https://foo.com/ | Aspired Websites',
                        t + timedelta(minutes=2))
        self._recording('Audit Results for https://bar.com/ — Aspired Websites',
                        t + timedelta(hours=3))
        self._lead('http://foo.com', t + timedelta(minutes=1))

        call_command('backfill_audit_runs', stdout=StringIO())
        self.assertEqual(AuditRun.objects.count(), 2)
        foo = AuditRun.objects.get(domain='foo.com')
        self.assertIsNotNone(foo.audit_lead)
        self.assertEqual(foo.performance_score, 50)
        self.assertIsNone(AuditRun.objects.get(domain='bar.com').audit_lead)

        # Idempotent.
        call_command('backfill_audit_runs', stdout=StringIO())
        self.assertEqual(AuditRun.objects.count(), 2)

    def test_unmatched_lead_becomes_its_own_run(self):
        self._lead('https://baz.com', timezone.now() - timedelta(days=3))
        call_command('backfill_audit_runs', stdout=StringIO())
        run = AuditRun.objects.get()
        self.assertEqual(run.source, 'backfill_lead')

    def test_dry_run_writes_nothing(self):
        self._lead('https://baz.com', timezone.now() - timedelta(days=3))
        call_command('backfill_audit_runs', '--dry-run', stdout=StringIO())
        self.assertFalse(AuditRun.objects.exists())


class AuditRunsPageTests(TestCase):
    def test_page_shows_all_runs_and_can_hide_own(self):
        admin = get_user_model().objects.create_user(
            'boss', 'b@example.com', 'pw', is_staff=True, is_superuser=True)
        self.client.force_login(admin)
        AuditRun.objects.create(url='https://outside.com/',
                                domain='outside.com')
        AuditRun.objects.create(url='https://aspiredwebsites.com/',
                                domain='aspiredwebsites.com')
        r = self.client.get(reverse('admin_dashboard:v2_audit_runs_list'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'outside.com')
        self.assertContains(r, 'https://aspiredwebsites.com/')
        r = self.client.get(
            reverse('admin_dashboard:v2_audit_runs_list') + '?internal=0')
        self.assertNotContains(r, 'https://aspiredwebsites.com/')


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    COMPANY_POSTAL_ADDRESS='1 Test St',
)
class DetailedReportEmailTests(TestCase):
    def _lead(self, **kw):
        defaults = dict(
            url='https://example.com/', performance_score=62, seo_score=91,
            best_practices_score=100, accessibility_score=45,
            email='v@example.com',
            details={
                'performance': {
                    'failed': [{'id': 'unused-javascript',
                                'title': 'Reduce unused JavaScript',
                                'display_value': 'Est savings of 200 KiB',
                                'learn_more': 'https://developer.chrome.com/x',
                                'description': 'x', 'score': 0.3}],
                    'passed': [{'id': 'font-display', 'title': 'Font display'}],
                },
                'seo': {'failed': [], 'passed': [{'id': 'x', 'title': 'Has a `<title>`'}]},
                'accessibility': {
                    'failed': [{'id': 'some-new-check', 'title': 'New check',
                                'description': 'Google says why.',
                                'learn_more': '', 'score': 0}],
                    'passed': [],
                },
                'best_practices': {'failed': [], 'passed': []},
                'metrics': [{'id': 'largest-contentful-paint',
                             'title': 'Largest Contentful Paint',
                             'display_value': '4.1 s', 'score': 0.2}],
            },
        )
        defaults.update(kw)
        return AuditLead.objects.create(**defaults)

    def test_report_is_html_with_full_detail(self):
        from django.core import mail
        from public.audit_sequence import send_report
        self.assertTrue(send_report(self._lead()))
        msg = mail.outbox[0]
        html = msg.alternatives[0][0]
        for text in ('Reduce unused JavaScript', 'Why it matters',
                     'How to fix it', 'What passed', 'Font display',
                     'Largest Contentful Paint', 'Google says why.',
                     'Unsubscribe', 'Has a &lt;title&gt;'):
            self.assertIn(text, html)
        self.assertIn('How to fix it:', msg.body)
        self.assertIn('+ Font display', msg.body)

    def test_legacy_lead_without_details_still_sends(self):
        from django.core import mail
        from public.audit_sequence import send_report
        lead = self._lead(details={}, issues={
            'performance': [{'title': 'Old issue', 'description': 'd'}]})
        self.assertTrue(send_report(lead))
        self.assertIn('Old issue', mail.outbox[0].alternatives[0][0])
