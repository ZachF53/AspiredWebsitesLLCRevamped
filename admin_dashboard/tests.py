"""Tests for the admin-dashboard AI assistant and related views.

(The legacy ClientProfileEditForm + quick-edit endpoint tests were
removed when the client_detail/client_edit pages they covered were
retired in favour of the Account/Website editors.)
"""

from unittest.mock import MagicMock, patch
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from clients.models import ClientProfile

User = get_user_model()
_ai_seq = 0


def _new_client(firm, **kw):
    global _ai_seq
    _ai_seq += 1
    u = User.objects.create_user(
        username=f'ai{_ai_seq}', password='x',
        email=f'ai{_ai_seq}@example.com')
    return ClientProfile.objects.create(user=u, firm_name=firm, **kw)


# Site-level values, mirrored onto the Website the autocreate signal
# makes. Same mapping the backfill applies, so a fixture and a migrated
# production row agree.
_SITE_FIELDS = {
    'stage': 'stage',
    'package': 'package',
    'payment_status': 'payment_status',
    'website': 'url',
    'maintenance_active': 'maintenance_active',
    'launch_date': 'launch_date',
    'revision_count': 'revision_count',
    'revision_limit': 'revision_limit',
}


def _new_site(firm, **kw):
    """A Website named `firm`, with the site-level kwargs applied.

    The AI assistant resolves and acts on SITES: every command it runs
    ("move X to design", "X is live", "invoice X") changes a build's
    state, and a build is a site.
    """
    profile = _new_client(firm, **kw)
    site = profile.migrated_account.websites.first()
    changed = []
    for legacy, canonical in _SITE_FIELDS.items():
        if legacy in kw:
            setattr(site, canonical, kw[legacy])
            changed.append(canonical)
    if site.name != firm:
        site.name = firm
        changed.append('name')
    if changed:
        site.save(update_fields=changed + ['updated_at'])
    return site


def _staff(password='admin-pass-123'):
    global _ai_seq
    _ai_seq += 1
    return User.objects.create_user(
        username=f'staff{_ai_seq}', password=password,
        email=f'staff{_ai_seq}@example.com',
        is_staff=True, is_active=True,
    )


class ParseCommandTests(TestCase):
    """Phase 4.1 — parse_command maps natural text → {intent, args}."""

    @patch('admin_dashboard.ai_assistant.claude_tools')
    def test_move_stage_maps_to_tool_use(self, mock_tools):
        from admin_dashboard.ai_assistant import parse_command
        mock_tools.return_value = {
            'kind': 'tool_use',
            'name': 'move_stage',
            'input': {'client': 'Johnson Law', 'stage': 'design'},
        }
        result = parse_command('Move Johnson Law to design')
        self.assertEqual(result['intent'], 'move_stage')
        self.assertEqual(result['args']['client'], 'Johnson Law')
        self.assertEqual(result['args']['stage'], 'design')

    @patch('admin_dashboard.ai_assistant.claude_tools')
    def test_text_response_becomes_clarify(self, mock_tools):
        from admin_dashboard.ai_assistant import parse_command
        mock_tools.return_value = {
            'kind': 'text',
            'text': 'Which client did you mean?',
        }
        result = parse_command('do the thing')
        self.assertIn('clarify', result)
        self.assertIn('Which client', result['clarify'])

    def test_empty_text_returns_clarify_immediately(self):
        from admin_dashboard.ai_assistant import parse_command
        result = parse_command('')
        self.assertIn('clarify', result)


class ResolveClientTests(TestCase):
    """Phase 4.2 — fuzzy resolution of name fragments."""

    def test_exact_match_wins(self):
        c = _new_site('Apex Holdings')
        from admin_dashboard.ai_assistant import resolve_client
        self.assertEqual(resolve_client('Apex Holdings').id, c.id)

    def test_case_insensitive_match(self):
        c = _new_site('Apex Holdings')
        from admin_dashboard.ai_assistant import resolve_client
        self.assertEqual(resolve_client('apex holdings').id, c.id)

    def test_partial_substring_resolves(self):
        c = _new_site('Smith & Associates')
        from admin_dashboard.ai_assistant import resolve_client
        self.assertEqual(resolve_client('Smith').id, c.id)

    def test_no_match_raises(self):
        from admin_dashboard.ai_assistant import (
            ClientNotFound, resolve_client,
        )
        with self.assertRaises(ClientNotFound):
            resolve_client('Nonexistent Firm XYZ')

    def test_ambiguous_substring_raises(self):
        _new_site('Smith Holdings')
        _new_site('Smith Capital')
        from admin_dashboard.ai_assistant import (
            ClientAmbiguous, resolve_client,
        )
        with self.assertRaises(ClientAmbiguous) as cm:
            resolve_client('Smith')
        self.assertGreaterEqual(len(cm.exception.matches), 2)


class ExecuteTests(TestCase):
    """Phase 4.3 — execute dispatches to clients.services correctly +
    guards convert exceptions to {'ok': False, 'message': ...}."""

    def test_move_stage_calls_service(self):
        c = _new_site('Stage Test LLC')
        from admin_dashboard.ai_assistant import execute
        with patch('admin_dashboard.ai_assistant.change_client_stage') as m:
            m.return_value = (MagicMock(id='log-1'), True)
            result = execute('move_stage',
                             {'client': c.name, 'stage': 'design'}, c,
                             set_by='Tester')
        self.assertTrue(result['ok'])
        m.assert_called_once()

    def test_mark_live_blocked_when_not_fully_paid(self):
        """Phase 4.0 guard: mark_live refuses unless payment_status='fully_paid'.
        Verified via the integration path — no mock — because the guard
        lives inside the service."""
        c = _new_site('Notpaid LLC', payment_status='awaiting_deposit',
                      stage='pre_launch')
        from admin_dashboard.ai_assistant import execute
        result = execute('mark_live', {'client': c.name}, c)
        self.assertFalse(result['ok'])
        self.assertIn('final payment', result['message'].lower())

    def test_create_invoice_refuses_zero_amount(self):
        c = _new_site('ZeroBill LLC')
        from admin_dashboard.ai_assistant import execute
        result = execute(
            'create_out_of_scope_invoice',
            {'client': c.name, 'description': 'extra work',
             'amount': 0}, c)
        self.assertFalse(result['ok'])

    def test_get_status_read_only(self):
        c = _new_site('Status LLC')
        from admin_dashboard.ai_assistant import execute
        result = execute('get_status', {'client': c.name}, c)
        self.assertTrue(result['ok'])
        self.assertEqual(result['extra']['firm_name'], 'Status LLC')


class AssistantViewTests(TestCase):
    """End-to-end of the HTMX endpoints — auth + flow."""

    def setUp(self):
        self.staff = _staff()
        self.client.force_login(self.staff)
        self.profile = _new_client('Viewtest LLC')
        # The assistant resolves and acts on SITES — the commands it runs
        # ("move X to design", "X is live") all change a build's state.
        self.site = self.profile.migrated_account.websites.first()

    def test_page_requires_staff(self):
        self.client.logout()
        r = self.client.get(reverse('admin_dashboard:ai_assistant'))
        self.assertIn(r.status_code, (302, 403))

    def test_page_renders_for_staff(self):
        r = self.client.get(reverse('admin_dashboard:ai_assistant'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'AI Assistant')

    @patch('admin_dashboard.ai_assistant.claude_tools')
    def test_parse_returns_preview_partial(self, mock_tools):
        mock_tools.return_value = {
            'kind': 'tool_use',
            'name': 'get_status',
            'input': {'client': self.site.name},
        }
        r = self.client.post(
            reverse('admin_dashboard:ai_assistant_parse'),
            data={'command': f'show me {self.site.name}'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Proposed action')
        self.assertContains(r, self.site.name)

    def test_execute_writes_log_row(self):
        """Even a guard-refused execution should land in AIAssistantLog."""
        from admin_dashboard.models import AIAssistantLog
        import json as _json
        r = self.client.post(
            reverse('admin_dashboard:ai_assistant_execute'),
            data={
                'intent': 'mark_live',
                'args': _json.dumps({'client': self.site.name}),
                'client_id': str(self.site.id),
                'raw_command': 'launch',
            })
        self.assertEqual(r.status_code, 200)
        log = AIAssistantLog.objects.filter(
            website_new=self.site, intent='mark_live').first()
        self.assertIsNotNone(log)
        # Should be False since the test client isn't 'fully_paid'.
        self.assertFalse(log.success)


# ── DMARC trend chart ───────────────────────────────────────────────────────

class DmarcTrendChartTests(TestCase):
    """
    Two bugs, both visible as "the graph shows nothing" or "the 1-year
    view overflows the page":

      1. Bars were sized with `height: N%` inline, which CSP dropped —
         and even once allowed, the percentage had nothing definite to
         resolve against, so every bar collapsed to its 1px minimum.
         Height is now driven off a definite-height track, and the
         pass/fail split uses flex-grow instead of percentages.
      2. The window was capped at `min(days, 90)` DAILY columns. 90
         columns at an 18px minimum is ~2000px — wider than the card.
         Long windows are now grouped into weeks or months.
    """

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from reporting.models import DmarcReport
        self.staff = User.objects.create_user(
            username='dstaff', password='dp', is_staff=True, is_superuser=True)
        self.client.login(username='dstaff', password='dp')
        now = timezone.now()
        for i in range(0, 300, 3):
            day = now - timedelta(days=i)
            DmarcReport.objects.create(
                org_name='google.com', report_id=f'test-{i}',
                period_start=day - timedelta(days=1), period_end=day,
                policy_domain='aspiredwebsites.com',
                total_messages=10 + i, dmarc_pass=9 + i, dmarc_fail=1)

    def _trend(self, days):
        resp = self.client.get(f'/admin-dashboard/dmarc/?days={days}')
        self.assertEqual(resp.status_code, 200)
        return resp.context['trend'], resp.context['trend_group']

    def test_short_window_stays_daily(self):
        trend, group = self._trend(30)
        self.assertEqual(group, 'day')
        self.assertEqual(len(trend), 30)

    def test_long_windows_are_grouped_to_stay_narrow(self):
        """The actual regression: 365 days must not be 90 columns."""
        for days, expected_group in ((90, 'week'), (365, 'month')):
            with self.subTest(days=days):
                trend, group = self._trend(days)
                self.assertEqual(group, expected_group)
                self.assertLessEqual(
                    len(trend), 52,
                    f'{len(trend)} columns at {days}d will overflow the card')

    def test_bar_height_tracks_volume_not_pass_rate(self):
        """
        The chart always claimed height was proportional to messages
        and never was — it was a 100% stacked split, so a 4-message day
        looked exactly like a 400-message day.
        """
        trend, _ = self._trend(90)
        populated = [t for t in trend if t['total']]
        self.assertTrue(populated)
        busiest = max(populated, key=lambda t: t['total'])
        self.assertEqual(busiest['height_pct'], 100)
        # More than one distinct height, i.e. the shape carries meaning.
        self.assertGreater(len({t['height_pct'] for t in populated}), 1)

    def test_empty_buckets_have_zero_height(self):
        trend, _ = self._trend(30)
        for t in trend:
            if not t['total']:
                self.assertEqual(t['height_pct'], 0)

    def test_quiet_buckets_stay_visible(self):
        """A real but small bucket must not be indistinguishable from a gap."""
        trend, _ = self._trend(365)
        for t in trend:
            if t['total']:
                self.assertGreaterEqual(t['height_pct'], 4)

    def test_no_percentage_height_attributes_on_segments(self):
        """
        The pass/fail split must use flex-grow. A percentage height
        here is what collapsed the chart in the first place.
        """
        html = self.client.get(
            '/admin-dashboard/dmarc/?days=30').content.decode()
        self.assertIn('dmarc-trend__pass', html)
        self.assertIn('flex-grow:', html)
        segment = html.split('dmarc-trend__pass')[1][:60]
        self.assertNotIn('height:', segment)
