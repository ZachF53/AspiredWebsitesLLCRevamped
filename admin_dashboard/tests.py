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
        c = _new_client('Apex Holdings')
        from admin_dashboard.ai_assistant import resolve_client
        self.assertEqual(resolve_client('Apex Holdings').id, c.id)

    def test_case_insensitive_match(self):
        c = _new_client('Apex Holdings')
        from admin_dashboard.ai_assistant import resolve_client
        self.assertEqual(resolve_client('apex holdings').id, c.id)

    def test_partial_substring_resolves(self):
        c = _new_client('Smith & Associates')
        from admin_dashboard.ai_assistant import resolve_client
        self.assertEqual(resolve_client('Smith').id, c.id)

    def test_no_match_raises(self):
        from admin_dashboard.ai_assistant import (
            ClientNotFound, resolve_client,
        )
        with self.assertRaises(ClientNotFound):
            resolve_client('Nonexistent Firm XYZ')

    def test_ambiguous_substring_raises(self):
        _new_client('Smith Holdings')
        _new_client('Smith Capital')
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
        c = _new_client('Stage Test LLC')
        from admin_dashboard.ai_assistant import execute
        with patch('admin_dashboard.ai_assistant.change_client_stage') as m:
            m.return_value = (MagicMock(id='log-1'), True)
            result = execute('move_stage',
                             {'client': c.firm_name, 'stage': 'design'}, c,
                             set_by='Tester')
        self.assertTrue(result['ok'])
        m.assert_called_once()

    def test_mark_live_blocked_when_not_fully_paid(self):
        """Phase 4.0 guard: mark_live refuses unless payment_status='fully_paid'.
        Verified via the integration path — no mock — because the guard
        lives inside the service."""
        c = _new_client('Notpaid LLC', payment_status='awaiting_deposit',
                        stage='pre_launch')
        from admin_dashboard.ai_assistant import execute
        result = execute('mark_live', {'client': c.firm_name}, c)
        self.assertFalse(result['ok'])
        self.assertIn('final payment', result['message'].lower())

    def test_create_invoice_refuses_zero_amount(self):
        c = _new_client('ZeroBill LLC')
        from admin_dashboard.ai_assistant import execute
        result = execute(
            'create_out_of_scope_invoice',
            {'client': c.firm_name, 'description': 'extra work',
             'amount': 0}, c)
        self.assertFalse(result['ok'])

    def test_get_status_read_only(self):
        c = _new_client('Status LLC')
        from admin_dashboard.ai_assistant import execute
        result = execute('get_status', {'client': c.firm_name}, c)
        self.assertTrue(result['ok'])
        self.assertEqual(result['extra']['firm_name'], 'Status LLC')


class AssistantViewTests(TestCase):
    """End-to-end of the HTMX endpoints — auth + flow."""

    def setUp(self):
        self.staff = _staff()
        self.client.force_login(self.staff)
        self.profile = _new_client('Viewtest LLC')

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
            'input': {'client': self.profile.firm_name},
        }
        r = self.client.post(
            reverse('admin_dashboard:ai_assistant_parse'),
            data={'command': f'show me {self.profile.firm_name}'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Proposed action')
        self.assertContains(r, self.profile.firm_name)

    def test_execute_writes_log_row(self):
        """Even a guard-refused execution should land in AIAssistantLog."""
        from admin_dashboard.models import AIAssistantLog
        import json as _json
        r = self.client.post(
            reverse('admin_dashboard:ai_assistant_execute'),
            data={
                'intent': 'mark_live',
                'args': _json.dumps({'client': self.profile.firm_name}),
                'client_id': str(self.profile.id),
                'raw_command': 'launch',
            })
        self.assertEqual(r.status_code, 200)
        log = AIAssistantLog.objects.filter(
            client=self.profile, intent='mark_live').first()
        self.assertIsNotNone(log)
        # Should be False since the test client isn't 'fully_paid'.
        self.assertFalse(log.success)
