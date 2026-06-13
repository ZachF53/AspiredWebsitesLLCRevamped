"""Tests for Phase 5a + 5b reporting features."""

import json
import tempfile
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientProfile, Project, UptimeAlert, UptimeRecord
from reporting.conversion_helpers import conversion_6month_chart, conversion_counts
from reporting.freshness import calculate_freshness_score
from reporting.keyword_helpers import keyword_trend, position_class
from reporting.models import (
    BlogPost,
    ChatbotConversation,
    ClientChatbot,
    ContentFreshnessReport,
    ConversionEvent,
    GBPSyncCheck,
    KeywordRankRecord,
    MonthlyReport,
    NPSSurvey,
    TrackedKeyword,
)
from reporting.uptime_helpers import (
    get_avg_response_time,
    get_current_status,
    get_uptime_percentage,
)

User = get_user_model()

_seq = 0


def _client(firm='Test Co', **kw):
    """Create a ClientProfile with a unique placeholder user (with email)."""
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'u{_seq}', password='x', email=f'u{_seq}@example.com')
    return ClientProfile.objects.create(user=user, firm_name=firm, **kw)


# ── Part 4: tracking endpoint ───────────────────────────────────────────────

class TrackEndpointTests(TestCase):

    def setUp(self):
        self.cp = _client('Track Co')
        self.url = reverse('reporting:track')

    def _post(self, payload):
        return self.client.post(
            self.url, data=json.dumps(payload),
            content_type='application/json')

    def test_valid_event_recorded(self):
        resp = self._post({
            'client_id': str(self.cp.id), 'event_type': 'form_submit',
            'page_url': 'https://x.com', 'timestamp': '2026-05-22T10:00:00Z',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ConversionEvent.objects.count(), 1)

    def test_unknown_client_returns_200_no_record(self):
        resp = self._post({
            'client_id': '00000000-0000-0000-0000-000000000000',
            'event_type': 'form_submit',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ConversionEvent.objects.count(), 0)

    def test_non_uuid_client_returns_200(self):
        resp = self._post({'client_id': 'test-uuid', 'event_type': 'form_submit'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ConversionEvent.objects.count(), 0)

    def test_bad_event_type_ignored(self):
        resp = self._post({
            'client_id': str(self.cp.id), 'event_type': 'malicious'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ConversionEvent.objects.count(), 0)

    def test_malformed_json_returns_200(self):
        resp = self.client.post(
            self.url, data='not json', content_type='application/json')
        self.assertEqual(resp.status_code, 200)

    def test_get_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_ip_never_stored_raw(self):
        self._post({'client_id': str(self.cp.id), 'event_type': 'phone_click'})
        event = ConversionEvent.objects.get()
        self.assertNotIn('127.0.0.1', event.ip_hash)
        self.assertEqual(len(event.ip_hash), 64)  # sha-256 hex digest


# ── Part 1: uptime monitoring ───────────────────────────────────────────────

class UptimeTaskTests(TestCase):

    def setUp(self):
        # Post-2026-05-25: live URL lives on client.website.
        self.cp = _client(
            'Up Co', status='active', do_droplet_ip='10.0.0.1',
            stage='live', website='https://upco.com')

    @patch('requests.get')
    def test_check_records_an_up_result(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        from reporting.tasks import check_client_uptime
        check_client_uptime()
        record = UptimeRecord.objects.get(client=self.cp)
        self.assertTrue(record.is_up)
        self.assertEqual(record.status_code, 200)

    @patch('requests.get')
    def test_three_failures_fire_one_alert(self, mock_get):
        mock_get.return_value = MagicMock(status_code=503)
        from reporting.tasks import check_client_uptime
        for _ in range(3):
            check_client_uptime()
        self.assertEqual(
            UptimeRecord.objects.filter(client=self.cp, is_up=False).count(), 3)
        self.assertEqual(
            UptimeAlert.objects.filter(client=self.cp, is_resolved=False).count(),
            1)
        check_client_uptime()  # 4th failure must not open a second alert
        self.assertEqual(
            UptimeAlert.objects.filter(client=self.cp, is_resolved=False).count(),
            1)

    @patch('requests.get')
    def test_recovery_resolves_alert(self, mock_get):
        from reporting.tasks import check_client_uptime
        mock_get.return_value = MagicMock(status_code=503)
        for _ in range(3):
            check_client_uptime()
        mock_get.return_value = MagicMock(status_code=200)
        check_client_uptime()
        self.assertFalse(
            UptimeAlert.objects.filter(client=self.cp, is_resolved=False).exists())
        self.assertTrue(
            UptimeAlert.objects.filter(client=self.cp, is_resolved=True).exists())

    @patch('requests.get')
    def test_request_exception_records_down(self, mock_get):
        import requests
        mock_get.side_effect = requests.RequestException('connection refused')
        from reporting.tasks import check_client_uptime
        check_client_uptime()
        record = UptimeRecord.objects.get(client=self.cp)
        self.assertFalse(record.is_up)
        self.assertIn('connection refused', record.error_message)


class UptimeHelperTests(TestCase):

    def setUp(self):
        self.cp = _client('Helper Co')

    def test_percentage_and_avg(self):
        for code, up, ms in [(200, True, 100), (200, True, 300),
                             (500, False, None)]:
            UptimeRecord.objects.create(
                client=self.cp, status_code=code, is_up=up, response_time_ms=ms)
        self.assertEqual(get_uptime_percentage(self.cp, 30), 66.67)
        self.assertEqual(get_avg_response_time(self.cp, 30), 200)

    def test_no_data_returns_none(self):
        self.assertIsNone(get_uptime_percentage(self.cp, 30))
        self.assertIsNone(get_current_status(self.cp))


# ── Part 2: GBP sync ────────────────────────────────────────────────────────

class GBPSyncTests(TestCase):

    def test_records_not_connected_status(self):
        # Post-2026-05-25: stage + website on client directly.
        cp = _client('GBP Co', status='active',
                     stage='live', website='https://g.com')
        from reporting.tasks import check_gbp_sync
        check_gbp_sync()
        check = GBPSyncCheck.objects.get(client=cp)
        self.assertEqual(check.website_value, 'GBP not connected')
        self.assertFalse(check.is_mismatch)


# ── Part 3: keyword helpers ─────────────────────────────────────────────────

class KeywordHelperTests(TestCase):

    def test_position_bands(self):
        self.assertEqual(position_class(2), 'top')
        self.assertEqual(position_class(8), 'page1')
        self.assertEqual(position_class(15), 'page2')
        self.assertEqual(position_class(40), 'low')
        self.assertEqual(position_class(None), 'muted')

    def test_trend_directions(self):
        self.assertEqual(
            keyword_trend(MagicMock(position=5), MagicMock(position=9))['css'],
            'up')
        self.assertEqual(
            keyword_trend(MagicMock(position=12), MagicMock(position=4))['css'],
            'down')
        self.assertEqual(
            keyword_trend(MagicMock(position=5), MagicMock(position=5))['css'],
            'same')
        self.assertEqual(keyword_trend(MagicMock(position=5), None)['css'], 'new')
        self.assertEqual(
            keyword_trend(MagicMock(position=None), None)['css'], 'muted')


# ── Part 4: conversion helpers + drop alert ─────────────────────────────────

class ConversionHelperTests(TestCase):

    def setUp(self):
        self.cp = _client('Conv Co')

    def test_counts_and_chart(self):
        ConversionEvent.objects.create(
            client=self.cp, event_type='form_submit',
            event_timestamp=timezone.now())
        rows = conversion_counts(self.cp)
        form_row = next(r for r in rows if r['type'] == 'form_submit')
        self.assertEqual(form_row['this_month'], 1)
        chart = conversion_6month_chart(self.cp)
        self.assertEqual(len(chart), 6)
        self.assertEqual(chart[-1]['count'], 1)  # current month is newest

    def test_conversion_drop_alert(self):
        now = timezone.now()
        last_month = now.replace(day=1) - timedelta(days=2)
        for _ in range(10):
            ConversionEvent.objects.create(
                client=self.cp, event_type='form_submit',
                event_timestamp=last_month)
        ConversionEvent.objects.create(
            client=self.cp, event_type='form_submit', event_timestamp=now)
        from reporting.tasks import check_conversion_drops
        self.assertIn('1 alert', check_conversion_drops())


# ── Part 6: admin pages ─────────────────────────────────────────────────────

class AdminMonitoringPageTests(TestCase):

    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff5a', password='staff-pass', is_staff=True)
        self.cp = _client('Admin Co')
        self.client.login(username='staff5a', password='staff-pass')

    def test_pages_render(self):
        self.assertEqual(
            self.client.get(reverse('admin_dashboard:client_list')).status_code,
            200)
        for name in ['client_conversions', 'client_tracker']:
            resp = self.client.get(
                reverse(f'admin_dashboard:{name}', args=[self.cp.id]))
            self.assertEqual(resp.status_code, 200, name)
        # Per-website monitoring (Phase D): uptime + keywords are
        # website-scoped now.
        site = self.cp.migrated_account.websites.first()
        for name in ['website_uptime', 'website_keywords']:
            resp = self.client.get(
                reverse(f'admin_dashboard:{name}', args=[site.id]))
            self.assertEqual(resp.status_code, 200, name)
        # client_detail is retired — now redirects to the account page.
        resp = self.client.get(
            reverse('admin_dashboard:client_detail', args=[self.cp.id]))
        self.assertEqual(resp.status_code, 302)

    def test_keyword_add(self):
        site = self.cp.migrated_account.websites.first()
        resp = self.client.post(
            reverse('admin_dashboard:keyword_add', args=[site.id]),
            {'keyword': 'family law attorney', 'target_url': '', 'notes': ''})
        self.assertRedirects(resp, reverse(
            'admin_dashboard:website_keywords', args=[site.id]))
        self.assertTrue(TrackedKeyword.objects.filter(
            website_new=site, keyword='family law attorney').exists())

    def test_keyword_add_rejects_duplicate(self):
        site = self.cp.migrated_account.websites.first()
        TrackedKeyword.objects.create(
            client=self.cp, website_new=site, keyword='dup kw')
        self.client.post(
            reverse('admin_dashboard:keyword_add', args=[site.id]),
            {'keyword': 'dup kw', 'target_url': '', 'notes': ''})
        self.assertEqual(TrackedKeyword.objects.filter(
            website_new=site, keyword='dup kw').count(), 1)

    def test_tracker_snippet_contains_client_id(self):
        resp = self.client.get(
            reverse('admin_dashboard:client_tracker', args=[self.cp.id]))
        self.assertContains(resp, str(self.cp.id))
        self.assertContains(resp, 'aspired-tracker.js')

    def test_gbp_flag_and_resolve(self):
        check = GBPSyncCheck.objects.create(
            client=self.cp, field_name='phone', is_mismatch=True,
            website_value='210-555-1000', gbp_value='210-555-9999')
        self.client.post(reverse(
            'admin_dashboard:gbp_flag', args=[self.cp.id, check.id]))
        check.refresh_from_db()
        self.assertTrue(check.flagged_for_fix)
        self.client.post(reverse(
            'admin_dashboard:gbp_resolve', args=[self.cp.id, check.id]))
        check.refresh_from_db()
        self.assertTrue(check.resolved)


# ── Part 3/4: client portal ─────────────────────────────────────────────────

class PortalSeoTests(TestCase):

    def setUp(self):
        user = User.objects.create_user(username='portal5a', password='pp')
        self.cp = ClientProfile.objects.create(user=user, firm_name='Portal Co')
        self.client.login(username='portal5a', password='pp')

    def test_seo_page_empty_state(self):
        resp = self.client.get(reverse('clients:portal_seo'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Your keyword rankings will appear here')

    def test_seo_page_shows_keyword(self):
        kw = TrackedKeyword.objects.create(
            client=self.cp, keyword='probate lawyer', is_active=True)
        KeywordRankRecord.objects.create(keyword=kw, position=4, impressions=120)
        resp = self.client.get(reverse('clients:portal_seo'))
        self.assertContains(resp, 'probate lawyer')
        self.assertContains(resp, '#4')

    def test_dashboard_and_project_render(self):
        self.assertEqual(
            self.client.get(reverse('clients:dashboard')).status_code, 200)
        self.assertEqual(
            self.client.get(reverse('clients:project')).status_code, 200)


# ════════════════════════════════════════════════════════════════════════════
# Phase 5b
# ════════════════════════════════════════════════════════════════════════════

@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class MonthlyReportTests(TestCase):

    def test_generate_creates_and_sends(self):
        from reporting.tasks import generate_monthly_report
        cp = _client('Report Co', status='active', maintenance_active=True)
        ConversionEvent.objects.create(
            client=cp, event_type='form_submit',
            event_timestamp=timezone.now())
        generate_monthly_report(str(cp.id), '2026-04-01')
        report = MonthlyReport.objects.get(client=cp)
        self.assertEqual(report.status, 'sent')
        self.assertTrue(report.pdf_path)

    def test_already_sent_not_regenerated(self):
        from reporting.tasks import generate_monthly_report
        cp = _client('Once Co', status='active', maintenance_active=True)
        MonthlyReport.objects.create(
            client=cp, report_month=date(2026, 4, 1), status='sent')
        result = generate_monthly_report(str(cp.id), '2026-04-01')
        self.assertIn('Already sent', result)

    def test_management_command(self):
        from django.core.management import call_command
        _client('Cmd Co', status='active', maintenance_active=True)
        call_command('send_monthly_reports')
        self.assertTrue(MonthlyReport.objects.exists())


class FreshnessTests(TestCase):

    def test_score_algorithm(self):
        fresh = calculate_freshness_score({
            'last_modified': timezone.now() - timedelta(days=5),
            'word_count': 800, 'is_blog': True, 'has_structured_data': True})
        self.assertEqual(fresh, 100)  # 40 + 30 + 15 + 15
        stale = calculate_freshness_score({
            'last_modified': timezone.now() - timedelta(days=400),
            'word_count': 50, 'is_blog': False, 'has_structured_data': False})
        self.assertEqual(stale, 0)

    @patch('reporting.freshness.crawl_site')
    def test_generate_report(self, mock_crawl):
        mock_crawl.return_value = [
            {'url': 'https://x.com/', 'title': 'Home',
             'last_modified': None, 'word_count': 120,
             'is_blog': False, 'has_structured_data': False},
            {'url': 'https://x.com/blog/post/', 'title': 'Post',
             'last_modified': timezone.now(), 'word_count': 900,
             'is_blog': True, 'has_structured_data': True},
        ]
        # Post-2026-05-25: stage + website on client directly.
        cp = _client('Crawl Co', status='active',
                     stage='live', website='https://x.com')
        from reporting.tasks import generate_freshness_report
        generate_freshness_report(str(cp.id))
        report = ContentFreshnessReport.objects.get(client=cp)
        self.assertEqual(report.pages_analyzed, 2)
        self.assertEqual(report.pages_needing_update, 1)  # the thin home page


class NPSTests(TestCase):

    def test_eligible_clients_surveyed(self):
        from reporting.tasks import send_nps_surveys
        cp = _client('NPS Co', maintenance_active=True)
        ClientProfile.objects.filter(pk=cp.pk).update(
            created_at=timezone.now() - timedelta(days=60))
        send_nps_surveys()
        self.assertEqual(NPSSurvey.objects.filter(client=cp).count(), 1)

    def test_new_client_not_surveyed(self):
        from reporting.tasks import send_nps_surveys
        _client('Fresh Co', maintenance_active=True)  # created just now
        send_nps_surveys()
        self.assertEqual(NPSSurvey.objects.count(), 0)

    def test_response_records_score_and_branches(self):
        cp = _client('Resp Co')
        survey = NPSSurvey.objects.create(client=cp)
        url = reverse('nps_response', args=[survey.survey_token, 9])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        survey.refresh_from_db()
        self.assertEqual(survey.score, 9)
        # Promoter POST → review prompt.
        resp = self.client.post(url, {'feedback': 'Great work'})
        self.assertContains(resp, 'Google review')
        survey.refresh_from_db()
        self.assertEqual(survey.response_action_taken, 'review_requested')

    def test_detractor_creates_needs_you(self):
        cp = _client('Sad Co')
        survey = NPSSurvey.objects.create(client=cp)
        url = reverse('nps_response', args=[survey.survey_token, 3])
        self.client.get(url)
        self.client.post(url, {'feedback': 'Not happy'})
        survey.refresh_from_db()
        self.assertEqual(survey.response_action_taken, 'needs_you_created')

    def test_bad_token_404(self):
        import uuid as _uuid
        resp = self.client.get(
            reverse('nps_response', args=[_uuid.uuid4(), 8]))
        self.assertEqual(resp.status_code, 404)


class TestimonialTests(TestCase):

    def test_request_sent_30_days_after_launch(self):
        from reporting.tasks import send_testimonial_requests
        # Post-2026-05-25: launch_date + stage on client directly.
        cp = _client('Launch Co', stage='live',
                     launch_date=timezone.localdate() - timedelta(days=35))
        send_testimonial_requests()
        cp.refresh_from_db()
        self.assertIsNotNone(cp.testimonial_requested_at)

    def test_not_resent(self):
        from reporting.tasks import send_testimonial_requests
        cp = _client('Done Co')
        Project.objects.create(
            client=cp, stage='live',
            launch_date=timezone.localdate() - timedelta(days=35))
        cp.testimonial_requested_at = timezone.now()
        cp.save()
        result = send_testimonial_requests()
        self.assertIn('0 testimonial', result)


class BlogTests(TestCase):

    def setUp(self):
        self.staff = User.objects.create_user(
            username='blogstaff', password='bp', is_staff=True)
        self.cp = _client('Blog Co')
        self.client.login(username='blogstaff', password='bp')

    @patch('reporting.ai.claude_complete')
    def test_generate_creates_review_post(self, mock_ai):
        mock_ai.side_effect = [
            '<h2>Top Tips</h2><p>Helpful content here for readers.</p>',
            'A concise meta description for the post.',
        ]
        resp = self.client.post(reverse('admin_dashboard:blog_generate'), {
            'client': str(self.cp.id),
            'topic': 'What to do after a car accident',
            'target_keyword': 'car accident lawyer',
            'length': 'medium', 'tone': 'professional',
        })
        post = BlogPost.objects.get(client=self.cp)
        self.assertEqual(post.status, 'review')
        self.assertIn('Top Tips', post.content)
        self.assertRedirects(resp, reverse(
            'admin_dashboard:blog_detail', args=[post.id]))

    def test_approve_action(self):
        post = BlogPost.objects.create(
            client=self.cp, topic='X', status='review',
            content='<p>Body</p>', title='X')
        self.client.post(reverse('admin_dashboard:blog_detail', args=[post.id]), {
            'action': 'approve', 'title': 'X', 'meta_description': 'm',
            'content': '<p>Body</p>',
        })
        post.refresh_from_db()
        self.assertEqual(post.status, 'approved')
        self.assertEqual(post.reviewed_by, 'blogstaff')

    def test_blog_list_renders(self):
        self.assertEqual(
            self.client.get(reverse('admin_dashboard:blog_list')).status_code,
            200)


class ChatbotTests(TestCase):

    def setUp(self):
        self.cp = _client('Chat Co', phone='210-555-0100')
        self.chatbot = ClientChatbot.objects.create(
            client=self.cp, is_active=True)
        self.url = reverse('reporting:chat')

    def _post(self, payload):
        return self.client.post(
            self.url, data=json.dumps(payload), content_type='text/plain')

    @patch('reporting.ai.claude_complete')
    def test_chat_replies_and_logs(self, mock_ai):
        mock_ai.return_value = 'Happy to help with that!'
        resp = self._post({
            'client_id': str(self.cp.id), 'session_id': 'sess-1',
            'message': 'Do you handle wills?', 'conversation_history': [],
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['response'], 'Happy to help with that!')
        conv = ChatbotConversation.objects.get(session_id='sess-1')
        self.assertEqual(len(conv.messages), 2)

    @patch('reporting.ai.claude_complete')
    def test_lead_detection(self, mock_ai):
        mock_ai.return_value = 'Thanks!'
        self._post({
            'client_id': str(self.cp.id), 'session_id': 'sess-2',
            'message': 'Call me at jane@example.com', 'conversation_history': [],
        })
        conv = ChatbotConversation.objects.get(session_id='sess-2')
        self.assertTrue(conv.lead_captured)
        self.assertEqual(conv.visitor_email, 'jane@example.com')

    def test_inactive_chatbot_403(self):
        self.chatbot.is_active = False
        self.chatbot.save()
        resp = self._post({
            'client_id': str(self.cp.id), 'session_id': 's', 'message': 'hi'})
        self.assertEqual(resp.status_code, 403)

    def test_config_endpoint(self):
        resp = self.client.get(reverse(
            'reporting:chat_config', args=[self.cp.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['active'])


class AdminReportingPageTests(TestCase):

    def setUp(self):
        User.objects.create_user(
            username='rstaff', password='rp', is_staff=True)
        self.cp = _client('Page Co')
        self.client.login(username='rstaff', password='rp')

    def test_pages_render(self):
        for name in ['reports_list', 'blog_list', 'blog_generate', 'nps_list']:
            self.assertEqual(
                self.client.get(reverse(f'admin_dashboard:{name}')).status_code,
                200, name)
        for name in ['client_freshness', 'client_chatbot']:
            self.assertEqual(
                self.client.get(reverse(
                    f'admin_dashboard:{name}', args=[self.cp.id])).status_code,
                200, name)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6 — GBP NAP sync + GSC keyword ranks
# ─────────────────────────────────────────────────────────────────────────────

from unittest.mock import patch as _patch6

from django.core import mail as _mail
from django.test import override_settings as _override6


@_override6(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class GbpIsConnectedTests(TestCase):
    @_patch6('social.services.google_access_token')
    def test_no_token_means_disconnected(self, mock_tok):
        from reporting.tasks import _gbp_is_connected
        mock_tok.return_value = None
        c = _client('NoConn LLC', status='active', stage='live',
                    gbp_location_name='accounts/1/locations/2')
        self.assertFalse(_gbp_is_connected(c))

    @_patch6('social.services.google_access_token')
    def test_token_no_location_disconnected(self, mock_tok):
        from reporting.tasks import _gbp_is_connected
        mock_tok.return_value = 'ya29.test'
        c = _client('NoLoc LLC', status='active', stage='live')
        self.assertFalse(_gbp_is_connected(c))

    @_patch6('social.services.google_access_token')
    def test_token_and_location_connected(self, mock_tok):
        from reporting.tasks import _gbp_is_connected
        mock_tok.return_value = 'ya29.test'
        c = _client('OK LLC', status='active', stage='live',
                    gbp_location_name='accounts/1/locations/2')
        self.assertTrue(_gbp_is_connected(c))


@_override6(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class CheckGbpSyncTests(TestCase):
    @_patch6('social.services.google_access_token')
    def test_disconnected_records_informational_row(self, mock_tok):
        from reporting.models import GBPSyncCheck
        from reporting.tasks import check_gbp_sync
        mock_tok.return_value = None
        _client('A LLC', status='active', stage='live')
        _mail.outbox = []
        check_gbp_sync()
        rows = GBPSyncCheck.objects.all()
        self.assertEqual(rows.count(), 1)
        self.assertFalse(rows.first().is_mismatch)
        self.assertEqual(len(_mail.outbox), 0)

    @_patch6('social.services.google_access_token')
    @_patch6('reporting.google_gbp.fetch_location')
    def test_mismatch_records_rows_and_emails(self, mock_fetch, mock_tok):
        from reporting.google_gbp import encrypt_token
        from reporting.models import GBPSyncCheck, GbpOperatorToken
        from reporting.tasks import check_gbp_sync

        mock_tok.return_value = 'ya29.test'
        mock_fetch.return_value = {
            'title':            'Wrong Name LLC',
            'phoneNumbers':     {'primaryPhone': '210-555-9999'},
            'storefrontAddress': {'addressLines': ['123 Main St']},
            'websiteUri':       'https://example.com',
        }
        # Need a real GbpOperatorToken so the inner fetch lookup works.
        c = _client('Drift LLC', status='active', stage='live',
                    gbp_location_name='accounts/1/locations/2',
                    phone='210-555-0000', address='123 Main St',
                    website='https://example.com')
        u = c.user
        with _override6(VAULT_SERVER_SECRET='test-vault-secret'):
            GbpOperatorToken.objects.create(
                user=u,
                access_token_encrypted=encrypt_token('ya29.test'),
                refresh_token_encrypted=encrypt_token('refresh.test'),
                expires_at=timezone.now() + timedelta(hours=1),
            )
            _mail.outbox = []
            check_gbp_sync()

        # 4 fields recorded
        self.assertEqual(GBPSyncCheck.objects.filter(client=c).count(), 4)
        name_row = GBPSyncCheck.objects.get(
            client=c, field_name='business_name')
        self.assertTrue(name_row.is_mismatch)
        phone_row = GBPSyncCheck.objects.get(client=c, field_name='phone')
        self.assertTrue(phone_row.is_mismatch)
        # At least one alert email
        self.assertGreaterEqual(len(_mail.outbox), 1)


class CheckKeywordRanksTests(TestCase):
    @_patch6('social.services.google_access_token')
    def test_skips_when_no_token(self, mock_tok):
        from reporting.models import KeywordRankRecord, TrackedKeyword
        from reporting.tasks import check_keyword_ranks
        mock_tok.return_value = None
        c = _client('NoToken LLC', status='active', stage='live',
                    website='https://example.com')
        TrackedKeyword.objects.create(
            client=c, keyword='estate planning', is_active=True)
        check_keyword_ranks()
        self.assertEqual(KeywordRankRecord.objects.count(), 0)

    @_patch6('social.services.google_access_token')
    @_patch6('reporting.tasks._gsc_query_position')
    def test_creates_record_when_gsc_returns_data(self, mock_gsc, mock_tok):
        from reporting.models import KeywordRankRecord, TrackedKeyword
        from reporting.tasks import check_keyword_ranks
        mock_tok.return_value = 'ya29.test'
        mock_gsc.return_value = {
            'position': 7, 'impressions': 250, 'clicks': 15,
        }
        c = _client('HasToken LLC', status='active', stage='live',
                    website='https://example.com')
        TrackedKeyword.objects.create(
            client=c, keyword='estate planning', is_active=True)
        check_keyword_ranks()
        rec = KeywordRankRecord.objects.get()
        self.assertEqual(rec.position, 7)
        self.assertEqual(rec.impressions, 250)
        self.assertEqual(rec.clicks, 15)

    @_patch6('social.services.google_access_token')
    @_patch6('reporting.tasks._gsc_query_position')
    def test_gsc_error_does_not_abort_loop(self, mock_gsc, mock_tok):
        from reporting.models import KeywordRankRecord, TrackedKeyword
        from reporting.tasks import check_keyword_ranks
        mock_tok.return_value = 'ya29.test'
        # First call raises, second returns data
        mock_gsc.side_effect = [RuntimeError('boom'), {
            'position': 5, 'impressions': 100, 'clicks': 8,
        }]
        c1 = _client('Err LLC', status='active', stage='live',
                     website='https://example.com')
        c2 = _client('OK LLC', status='active', stage='live',
                     website='https://example.com')
        TrackedKeyword.objects.create(
            client=c1, keyword='one', is_active=True)
        TrackedKeyword.objects.create(
            client=c2, keyword='two', is_active=True)
        check_keyword_ranks()
        # Second keyword still got a record despite first one raising
        self.assertEqual(KeywordRankRecord.objects.count(), 1)


@override_settings(GA4_ACCOUNT_ID='12345')
class GA4ProvisionTests(TestCase):
    """reporting.ga4.provision_ga4_for_website — create property + stream."""

    @classmethod
    def setUpTestData(cls):
        from clients.account_models import Account
        u = User.objects.create_user(
            username='ga4u', password='x', email='ga4u@example.com')
        cls.profile = ClientProfile.objects.create(
            user=u, firm_name='GA4 LLC', website='https://ga4llc.com')
        cls.account = Account.objects.get(legacy_client_profile=cls.profile)
        cls.website = cls.account.websites.first()

    def test_provisions_property_stream_and_stores_id(self):
        prop = MagicMock(); prop.raise_for_status.return_value = None
        prop.json.return_value = {'name': 'properties/999'}
        stream = MagicMock(); stream.raise_for_status.return_value = None
        stream.json.return_value = {
            'name': 'properties/999/dataStreams/1',
            'webStreamData': {'measurementId': 'G-TEST123'}}
        access = MagicMock(); access.raise_for_status.return_value = None
        with patch('reporting.ga4._operator_token', return_value=MagicMock()), \
             patch('reporting.ga4.refresh_if_needed'), \
             patch('reporting.ga4._auth_header',
                   return_value={'Authorization': 'Bearer x'}), \
             patch('reporting.ga4.requests.post',
                   side_effect=[prop, stream, access]) as mpost:
            from reporting.ga4 import provision_ga4_for_website
            mid = provision_ga4_for_website(self.website)
        self.assertEqual(mid, 'G-TEST123')
        self.website.refresh_from_db()
        self.assertEqual(self.website.ga4_measurement_id, 'G-TEST123')
        self.assertEqual(self.website.ga4_property_id, 'properties/999')
        # property + stream + access binding = 3 POSTs
        self.assertEqual(mpost.call_count, 3)

    def test_idempotent_when_already_provisioned(self):
        self.website.ga4_measurement_id = 'G-EXISTS'
        self.website.save(update_fields=['ga4_measurement_id'])
        with patch('reporting.ga4.requests.post') as mpost:
            from reporting.ga4 import provision_ga4_for_website
            r = provision_ga4_for_website(self.website)
        self.assertEqual(r, 'G-EXISTS')
        mpost.assert_not_called()

    def test_skips_without_account_id(self):
        with override_settings(GA4_ACCOUNT_ID=''):
            with patch('reporting.ga4.requests.post') as mpost:
                from reporting.ga4 import provision_ga4_for_website
                r = provision_ga4_for_website(self.website)
        self.assertIsNone(r)
        mpost.assert_not_called()

    def test_skips_without_operator_token(self):
        with patch('reporting.ga4._operator_token', return_value=None), \
             patch('reporting.ga4.requests.post') as mpost:
            from reporting.ga4 import provision_ga4_for_website
            r = provision_ga4_for_website(self.website)
        self.assertIsNone(r)
        mpost.assert_not_called()
