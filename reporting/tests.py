"""Tests for Phase 5a — uptime, GBP sync, keyword tracking, conversions."""

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientProfile, Project, UptimeAlert, UptimeRecord
from reporting.conversion_helpers import conversion_6month_chart, conversion_counts
from reporting.keyword_helpers import keyword_trend, position_class
from reporting.models import (
    ConversionEvent,
    GBPSyncCheck,
    KeywordRankRecord,
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
    """Create a ClientProfile with a unique placeholder user."""
    global _seq
    _seq += 1
    user = User.objects.create_user(username=f'u{_seq}', password='x')
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
        self.cp = _client('Up Co', status='active', do_droplet_ip='10.0.0.1')
        Project.objects.create(
            client=self.cp, stage='live', live_url='https://upco.com')

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
        cp = _client('GBP Co', status='active')
        Project.objects.create(client=cp, stage='live', live_url='https://g.com')
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
        for name in ['client_detail', 'client_uptime', 'client_keywords',
                     'client_conversions', 'client_tracker']:
            resp = self.client.get(
                reverse(f'admin_dashboard:{name}', args=[self.cp.id]))
            self.assertEqual(resp.status_code, 200, name)

    def test_keyword_add(self):
        resp = self.client.post(
            reverse('admin_dashboard:keyword_add', args=[self.cp.id]),
            {'keyword': 'family law attorney', 'target_url': '', 'notes': ''})
        self.assertRedirects(resp, reverse(
            'admin_dashboard:client_keywords', args=[self.cp.id]))
        self.assertTrue(TrackedKeyword.objects.filter(
            client=self.cp, keyword='family law attorney').exists())

    def test_keyword_add_rejects_duplicate(self):
        TrackedKeyword.objects.create(client=self.cp, keyword='dup kw')
        self.client.post(
            reverse('admin_dashboard:keyword_add', args=[self.cp.id]),
            {'keyword': 'dup kw', 'target_url': '', 'notes': ''})
        self.assertEqual(TrackedKeyword.objects.filter(
            client=self.cp, keyword='dup kw').count(), 1)

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
