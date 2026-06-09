"""Tests for the admin-dashboard client edit form + quick-edit endpoint."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.models import ClientProfile

User = get_user_model()


class ClientProfileEditFormTests(TestCase):
    """Regression coverage for the package-dropdown + live-url
    auto-prepend fixes."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='admin-form', email='af@example.com',
            password='x', is_staff=True, is_superuser=True)
        self.client_profile = ClientProfile.objects.create(
            user=self.user, firm_name='Edit Co')

    def _base_post(self, **overrides):
        """Minimum-valid POST data for ClientProfileEditForm."""
        data = {
            'firm_name': 'Edit Co',
            'contact_name': '',
            'business_type': '',
            'status': 'active',
            'package': '',
            'city': '', 'state': '', 'phone': '',
            'do_droplet_ip': '',
            'do_droplet_created_at': '',
            'live_url': '',
            'maintenance_active': '',
            'auto_send_scan_reports': '',
            'onboarding_complete': '',
            'is_tester': '',
            'internal_notes': '',
        }
        data.update(overrides)
        return data

    # ── package = dropdown ──

    def test_package_is_a_choice_field_with_canonical_choices(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(instance=self.client_profile)
        choices = dict(form.fields['package'].choices)
        # Blank option always present
        self.assertIn('', choices)
        # All canonical package codes are options
        for code, _label in ClientProfile.PACKAGE_CHOICES:
            self.assertIn(code, choices)

    def test_package_dropdown_renders_select_html(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(instance=self.client_profile)
        rendered = str(form['package'])
        self.assertIn('<select', rendered)
        self.assertNotIn('<input', rendered)

    def test_package_invalid_value_rejected(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(package='garbage-not-a-real-code'),
            instance=self.client_profile)
        self.assertFalse(form.is_valid())
        self.assertIn('package', form.errors)

    def test_package_valid_choice_saves(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(package='essential_build'),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.package, 'essential_build')

    def test_package_blank_is_allowed(self):
        from admin_dashboard.forms import ClientProfileEditForm
        self.client_profile.package = 'essential_build'
        self.client_profile.save()
        form = ClientProfileEditForm(
            self._base_post(package=''),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.package, '')

    # ── live_url = tolerant CharField + auto-https ──

    def test_live_url_empty_stays_empty(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(live_url=''),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['live_url'], '')

    def test_live_url_naked_domain_gets_https_prepended(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(live_url='clientdomain.com'),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data['live_url'], 'https://clientdomain.com')

    def test_live_url_existing_https_unchanged(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(live_url='https://already.com'),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data['live_url'], 'https://already.com')

    def test_live_url_http_kept_as_is(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(live_url='http://legacy.com'),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data['live_url'], 'http://legacy.com')

    def test_live_url_whitespace_stripped(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(live_url='   spaced.com   '),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data['live_url'], 'https://spaced.com')

    def test_live_url_garbage_rejected(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(live_url='not a domain at all'),
            instance=self.client_profile)
        self.assertFalse(form.is_valid())
        self.assertIn('live_url', form.errors)


class ClientQuickEditLiveUrlTests(TestCase):
    """The inline HTMX quick-edit for live_url should accept naked
    domains and auto-prepend https://."""

    def setUp(self):
        from django.test import Client as DjangoTestClient
        self.user = User.objects.create_user(
            username='qe-admin', email='qe@example.com',
            password='x', is_staff=True, is_superuser=True)
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='QE Co', stage='live')
        # 2026-05-25 refactor: quick-edit live_url writes to
        # client.website (canonical). No project row required.
        self.tc = DjangoTestClient()
        self.tc.force_login(self.user)

    def _post_url(self, value):
        return self.tc.post(
            reverse('admin_dashboard:client_quick_edit_field',
                    args=[self.profile.id]),
            {'field': 'live_url', 'value': value})

    def test_naked_domain_gets_https_prepended(self):
        resp = self._post_url('clientdomain.com')
        self.assertEqual(resp.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(
            self.profile.website, 'https://clientdomain.com')

    def test_https_value_kept_as_is(self):
        self._post_url('https://kept.com')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.website, 'https://kept.com')

    def test_empty_value_clears_url(self):
        self.profile.website = 'https://old.com'
        self.profile.save()
        self._post_url('')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.website, '')

    def test_quick_edit_field_meta_uses_text_input(self):
        """Browser-side blocker: type=url rejects 'clientdomain.com'.
        Our quick-edit must use type=text so submission isn't blocked
        before the server can normalise."""
        from admin_dashboard.forms import CLIENT_QUICK_EDIT_FIELDS
        self.assertEqual(
            CLIENT_QUICK_EDIT_FIELDS['live_url']['type'], 'text')


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — AI Assistant tests
# ─────────────────────────────────────────────────────────────────────────────

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
