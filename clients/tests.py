"""Tests for the PIN-gated credentials vault and the site changelog."""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientProfile, SiteChangelogEntry
from vault.crypto import generate_salt, hash_client_pin, hash_pin, verify_client_pin
from vault.models import VaultCredential

User = get_user_model()


class ClientPinCryptoTests(TestCase):
    """The client-PIN hashing primitives."""

    def test_hash_verify_roundtrip(self):
        salt = generate_salt()
        stored = hash_client_pin('4821', salt)
        self.assertTrue(verify_client_pin('4821', stored, salt))
        self.assertFalse(verify_client_pin('0000', stored, salt))

    def test_independent_of_admin_pin(self):
        """Same PIN + salt must hash differently from the admin vault PIN."""
        salt = generate_salt()
        self.assertNotEqual(hash_pin('4821', salt), hash_client_pin('4821', salt))

    def test_verify_handles_empty_inputs(self):
        self.assertFalse(verify_client_pin('1234', '', generate_salt()))
        self.assertFalse(verify_client_pin('1234', 'abc', None))


class PortalCredentialsTests(TestCase):
    """The /portal/credentials/ PIN gate end to end."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='client1', password='portal-pass-123')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='Test Firm')
        self.url = reverse('clients:credentials')
        self.reauth_url = reverse('clients:credentials_reauth')
        self.client.login(username='client1', password='portal-pass-123')

    def _set_pin(self, pin='1234'):
        salt = generate_salt()
        self.profile.client_pin_salt = salt
        self.profile.client_pin_hash = hash_client_pin(pin, salt)
        self.profile.client_pin_set = True
        self.profile.save()

    def _unlock_session(self, when=None):
        session = self.client.session
        session['client_vault_unlocked_at'] = (
            when or timezone.now()).isoformat()
        session.save()

    # ── First-time setup ──
    def test_setup_page_shown_when_no_pin(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Set Your Credentials PIN')

    def test_setup_creates_pin_and_unlocks(self):
        resp = self.client.post(self.url, {'pin': '4821', 'pin_confirm': '4821'})
        self.assertRedirects(resp, self.url)
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.client_pin_set)
        self.assertTrue(verify_client_pin(
            '4821', self.profile.client_pin_hash,
            bytes(self.profile.client_pin_salt)))
        # The setup unlocked the session — credentials render straight away.
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Account logins Aspired Websites')

    def test_setup_rejects_mismatch(self):
        resp = self.client.post(self.url, {'pin': '4821', 'pin_confirm': '0000'})
        self.assertContains(resp, 'do not match')
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.client_pin_set)

    def test_setup_rejects_bad_length(self):
        resp = self.client.post(self.url, {'pin': '12', 'pin_confirm': '12'})
        self.assertContains(resp, 'exactly 4 digits')

    # ── PIN entry ──
    def test_enter_pin_page_shown_when_session_locked(self):
        self._set_pin()
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Enter Your PIN')

    def test_correct_pin_unlocks(self):
        self._set_pin('1234')
        resp = self.client.post(
            self.url, {'d1': '1', 'd2': '2', 'd3': '3', 'd4': '4'})
        self.assertRedirects(resp, self.url)
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Account logins Aspired Websites')

    def test_wrong_pin_shows_error_and_counts(self):
        self._set_pin('1234')
        resp = self.client.post(
            self.url, {'d1': '9', 'd2': '9', 'd3': '9', 'd4': '9'})
        self.assertContains(resp, 'Incorrect PIN')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.client_pin_failed_attempts, 1)

    def test_five_wrong_pins_trigger_lockout(self):
        self._set_pin('1234')
        for _ in range(5):
            resp = self.client.post(
                self.url, {'d1': '9', 'd2': '9', 'd3': '9', 'd4': '9'})
        self.assertContains(resp, 'Too Many Attempts')
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.client_pin_lockout_until)
        # A fresh GET stays locked.
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Too Many Attempts')

    # ── Credentials display ──
    def test_only_visible_credentials_listed(self):
        self._set_pin()
        self._unlock_session()
        vault = self.profile.vault
        VaultCredential.objects.create(
            vault=vault, label='DigitalOcean', category='server',
            visible_to_client=True,
            client_username_plain='admin@test.com',
            client_password_plain='s3cret-pw')
        VaultCredential.objects.create(
            vault=vault, label='Hidden Cred', category='custom',
            visible_to_client=False)
        resp = self.client.get(self.url)
        self.assertContains(resp, 'DigitalOcean')
        self.assertContains(resp, 'admin@test.com')
        self.assertContains(resp, 's3cret-pw')
        self.assertNotContains(resp, 'Hidden Cred')

    def test_expired_session_reprompts_for_pin(self):
        self._set_pin()
        self._unlock_session(when=timezone.now() - timedelta(minutes=20))
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Enter Your PIN')

    # ── HTMX re-auth ──
    def test_reauth_success_fires_trigger(self):
        self._set_pin('1234')
        resp = self.client.post(
            self.reauth_url, {'d1': '1', 'd2': '2', 'd3': '3', 'd4': '4'})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp['HX-Trigger'], 'vaultReauthed')

    def test_reauth_wrong_pin_returns_error_partial(self):
        self._set_pin('1234')
        resp = self.client.post(
            self.reauth_url, {'d1': '0', 'd2': '0', 'd3': '0', 'd4': '0'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Incorrect PIN')

    def test_reauth_lockout_redirects(self):
        self._set_pin('1234')
        for _ in range(4):
            self.client.post(
                self.reauth_url, {'d1': '0', 'd2': '0', 'd3': '0', 'd4': '0'})
        resp = self.client.post(
            self.reauth_url, {'d1': '0', 'd2': '0', 'd3': '0', 'd4': '0'})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp['HX-Redirect'], self.url)


# ════════════════════════════════════════════════════════════════════════════
# Site changelog
# ════════════════════════════════════════════════════════════════════════════

class SiteChangelogModelTests(TestCase):
    """The SiteChangelogEntry model."""

    def setUp(self):
        user = User.objects.create_user(username='cl-model', password='x')
        self.client_profile = ClientProfile.objects.create(
            user=user, firm_name='Model Firm')

    def test_defaults_to_today(self):
        entry = SiteChangelogEntry.objects.create(
            client=self.client_profile, title='A change')
        self.assertEqual(entry.date_of_change, timezone.localdate())
        self.assertEqual(entry.change_type, 'other')
        self.assertTrue(entry.is_client_visible)

    def test_str(self):
        entry = SiteChangelogEntry.objects.create(
            client=self.client_profile, change_type='bug_fix', title='Fixed')
        self.assertIn('Model Firm', str(entry))
        self.assertIn('Bug Fix', str(entry))

    def test_ordering_newest_first(self):
        old = SiteChangelogEntry.objects.create(
            client=self.client_profile, title='Old', date_of_change=date(2026, 1, 1))
        new = SiteChangelogEntry.objects.create(
            client=self.client_profile, title='New', date_of_change=date(2026, 5, 1))
        self.assertEqual(
            list(SiteChangelogEntry.objects.all()), [new, old])


class AdminChangelogTests(TestCase):
    """The admin-dashboard changelog views."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff', password='staff-pass-123', is_staff=True)
        cuser = User.objects.create_user(username='acme-user', password='x')
        self.client_profile = ClientProfile.objects.create(
            user=cuser, firm_name='Acme Law')
        self.client.login(username='staff', password='staff-pass-123')

    def test_list_shows_entries(self):
        SiteChangelogEntry.objects.create(
            client=self.client_profile, title='Patched Django')
        resp = self.client.get(reverse('admin_dashboard:changelog_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Patched Django')

    def test_add_get(self):
        resp = self.client.get(reverse('admin_dashboard:changelog_add'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Add Changelog Entry')

    def test_add_post_creates_entry(self):
        resp = self.client.post(reverse('admin_dashboard:changelog_add'), {
            'client': str(self.client_profile.id),
            'date_of_change': '2026-05-20',
            'change_type': 'security_patch',
            'title': 'Security patches applied',
            'description': 'Django updated to latest LTS.',
            'is_client_visible': 'on',
        })
        self.assertRedirects(resp, reverse('admin_dashboard:changelog_list'))
        entry = SiteChangelogEntry.objects.get(title='Security patches applied')
        self.assertEqual(entry.change_type, 'security_patch')
        self.assertTrue(entry.is_client_visible)

    def test_client_scoped_add_prefills_and_redirects(self):
        url = reverse('admin_dashboard:changelog_add_client',
                      args=[self.client_profile.id])
        resp = self.client.post(url, {
            'client': str(self.client_profile.id),
            'date_of_change': '2026-05-21',
            'change_type': 'page_added',
            'title': 'New estate planning page',
            'is_client_visible': 'on',
        })
        self.assertRedirects(resp, reverse(
            'admin_dashboard:client_changelog', args=[self.client_profile.id]))

    def test_edit_updates_entry(self):
        entry = SiteChangelogEntry.objects.create(
            client=self.client_profile, title='Before')
        resp = self.client.post(
            reverse('admin_dashboard:changelog_edit', args=[entry.id]), {
                'client': str(self.client_profile.id),
                'date_of_change': '2026-05-21',
                'change_type': 'other',
                'title': 'After',
                'is_client_visible': 'on',
            })
        self.assertRedirects(resp, reverse('admin_dashboard:changelog_list'))
        entry.refresh_from_db()
        self.assertEqual(entry.title, 'After')

    def test_delete_removes_entry(self):
        entry = SiteChangelogEntry.objects.create(
            client=self.client_profile, title='Doomed')
        resp = self.client.post(
            reverse('admin_dashboard:changelog_delete', args=[entry.id]))
        self.assertRedirects(resp, reverse('admin_dashboard:changelog_list'))
        self.assertFalse(SiteChangelogEntry.objects.filter(id=entry.id).exists())

    def test_delete_rejects_get(self):
        entry = SiteChangelogEntry.objects.create(
            client=self.client_profile, title='Safe')
        resp = self.client.get(
            reverse('admin_dashboard:changelog_delete', args=[entry.id]))
        self.assertEqual(resp.status_code, 405)
        self.assertTrue(SiteChangelogEntry.objects.filter(id=entry.id).exists())

    def test_client_changelog_filtered_to_client(self):
        other_user = User.objects.create_user(username='other', password='x')
        other = ClientProfile.objects.create(
            user=other_user, firm_name='Other Firm')
        SiteChangelogEntry.objects.create(
            client=self.client_profile, title='Acme entry')
        SiteChangelogEntry.objects.create(client=other, title='Other entry')
        resp = self.client.get(reverse(
            'admin_dashboard:client_changelog', args=[self.client_profile.id]))
        self.assertContains(resp, 'Acme entry')
        self.assertNotContains(resp, 'Other entry')

    def test_list_filter_by_change_type(self):
        SiteChangelogEntry.objects.create(
            client=self.client_profile, title='A patch', change_type='security_patch')
        SiteChangelogEntry.objects.create(
            client=self.client_profile, title='A blog', change_type='blog_published')
        resp = self.client.get(reverse('admin_dashboard:changelog_list'),
                               {'change_type': 'security_patch'})
        self.assertContains(resp, 'A patch')
        self.assertNotContains(resp, 'A blog')

    def test_import_preview_then_save(self):
        raw = ('[1/7] Pulling latest code from GitHub...\n'
               'noise line\n'
               '[2/7] Installing dependencies...\n'
               '[3/7] Running migrations...')
        url = reverse('admin_dashboard:changelog_import')
        # Preview — parses but saves nothing.
        resp = self.client.post(url, {
            'step': 'preview',
            'import_client': str(self.client_profile.id),
            'raw_log': raw,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Running migrations')
        self.assertContains(resp, 'Imported entries are internal by default')
        self.assertEqual(SiteChangelogEntry.objects.count(), 0)
        # Save — creates one entry per [n/n] step.
        resp = self.client.post(url, {
            'step': 'save',
            'import_client': str(self.client_profile.id),
            'raw_log': raw,
        })
        self.assertRedirects(resp, reverse(
            'admin_dashboard:client_changelog', args=[self.client_profile.id]))
        self.assertEqual(SiteChangelogEntry.objects.count(), 3)
        self.assertTrue(SiteChangelogEntry.objects.filter(
            change_type='deployment',
            title='Pulling latest code from GitHub...').exists())
        # Imported entries are internal by default — never auto-shown.
        self.assertEqual(
            SiteChangelogEntry.objects.filter(is_client_visible=True).count(), 0)

    def test_import_preview_keeps_add_form_action(self):
        """After a preview re-render the main entry form must still post to
        changelog_add — not to changelog_import (the current page URL)."""
        resp = self.client.post(reverse('admin_dashboard:changelog_import'), {
            'step': 'preview',
            'import_client': str(self.client_profile.id),
            'raw_log': '[1/7] Pulling latest code from GitHub...',
        })
        self.assertContains(
            resp, 'action="%s"' % reverse('admin_dashboard:changelog_add'))

    def test_requires_staff(self):
        self.client.logout()
        cuser = User.objects.create_user(username='nonstaff', password='np-123')
        self.client.login(username='nonstaff', password='np-123')
        resp = self.client.get(reverse('admin_dashboard:changelog_list'))
        self.assertNotEqual(resp.status_code, 200)


class PortalChangelogTests(TestCase):
    """The client-facing /portal/changelog/ Activity Log."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='portal-cl', password='portal-pass-123')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='Portal Firm')
        self.url = reverse('clients:portal_changelog')
        self.client.login(username='portal-cl', password='portal-pass-123')

    def test_visible_entry_shown(self):
        SiteChangelogEntry.objects.create(
            client=self.profile, title='Visible work', is_client_visible=True)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Visible work')

    def test_internal_entry_hidden(self):
        SiteChangelogEntry.objects.create(
            client=self.profile, title='Secret internal note',
            is_client_visible=False)
        resp = self.client.get(self.url)
        self.assertNotContains(resp, 'Secret internal note')

    def test_other_clients_entries_hidden(self):
        other_user = User.objects.create_user(username='other-cl', password='x')
        other = ClientProfile.objects.create(
            user=other_user, firm_name='Someone Else')
        SiteChangelogEntry.objects.create(
            client=other, title='Not your work')
        resp = self.client.get(self.url)
        self.assertNotContains(resp, 'Not your work')

    def test_empty_state(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, 'No activity logged yet')

    def test_month_filter(self):
        SiteChangelogEntry.objects.create(
            client=self.profile, title='April thing',
            date_of_change=date(2026, 4, 10))
        SiteChangelogEntry.objects.create(
            client=self.profile, title='May thing',
            date_of_change=date(2026, 5, 10))
        resp = self.client.get(self.url, {'month': '2026-05'})
        self.assertContains(resp, 'May thing')
        self.assertNotContains(resp, 'April thing')

    def test_new_entry_dot_in_context(self):
        SiteChangelogEntry.objects.create(
            client=self.profile, title='Fresh', is_client_visible=True)
        resp = self.client.get(self.url)
        self.assertTrue(resp.context['changelog_has_new'])

    def test_old_entry_no_dot(self):
        entry = SiteChangelogEntry.objects.create(
            client=self.profile, title='Stale', is_client_visible=True)
        SiteChangelogEntry.objects.filter(id=entry.id).update(
            created_at=timezone.now() - timedelta(days=30))
        resp = self.client.get(self.url)
        self.assertFalse(resp.context['changelog_has_new'])


class LogDeploymentCommandTests(TestCase):
    """The log_deployment management command."""

    def setUp(self):
        user = User.objects.create_user(username='cmd-user', password='x')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Command Firm')

    def test_creates_deployment_entry(self):
        call_command('log_deployment', str(self.profile.id),
                     '--title', 'Deployed updates',
                     '--description', 'Code + migrations.')
        entry = SiteChangelogEntry.objects.get(client=self.profile)
        self.assertEqual(entry.change_type, 'deployment')
        self.assertEqual(entry.title, 'Deployed updates')
        self.assertTrue(entry.is_client_visible)
        self.assertEqual(entry.date_of_change, timezone.localdate())

    def test_bad_client_id_raises(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('log_deployment', 'not-a-real-id')


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3.4 — Contract signing flow (Phase 2.3 audit-trail coverage)
# ─────────────────────────────────────────────────────────────────────────────

class ContractSignFlowTests(TestCase):
    """Token-gated signing + Phase 2.3 audit-trail hardening."""

    @classmethod
    def setUpTestData(cls):
        from clients.models import Contract
        from decimal import Decimal
        # Note: don't shadow `User` (loaded at top of file).
        u = User.objects.create_user(
            username='contractsigner1', password='x',
            email='cs1@example.com')
        cls.profile = ClientProfile.objects.create(
            user=u, firm_name='Contract LLC',
            contact_name='Carla Counsel')
        cls.contract = Contract.objects.create(
            client=cls.profile,
            package='website-essential',
            build_price=Decimal('2500'),
            deposit_amount=Decimal('1250'),
            timeline_weeks=4,
            contract_text='<h1>Test Contract</h1><p>Body text here.</p>',
        )
        cls.sign_url = reverse('clients:contract_sign',
                               args=[cls.contract.contract_token])

    def test_get_renders_unsigned_contract(self):
        r = self.client.get(self.sign_url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Test Contract')

    def test_post_without_name_shows_error(self):
        r = self.client.post(self.sign_url, data={
            'signed_name': '',
            'agree': 'on',
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Please type your full legal name')
        self.contract.refresh_from_db()
        self.assertFalse(self.contract.signed)

    def test_post_without_checkbox_shows_error(self):
        r = self.client.post(self.sign_url, data={
            'signed_name': 'Carla Counsel',
            # no `agree`
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'check the box')
        self.contract.refresh_from_db()
        self.assertFalse(self.contract.signed)

    def test_post_valid_captures_all_audit_fields(self):
        """Phase 2.3 — happy path captures IP, user-agent, name, content
        hash, and triggers issue_deposit_invoice."""
        from unittest.mock import patch
        from datetime import datetime
        UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 Chrome/149 Safari/537.36')
        with patch('billing.stripe_helpers.issue_deposit_invoice') as mock_dep, \
             patch('clients.views.render_contract_pdf') as mock_pdf, \
             patch('clients.views.send_contract_signed_email') as mock_email:
            mock_pdf.return_value = 'contracts/test.pdf'
            r = self.client.post(self.sign_url, data={
                'signed_name': 'Carla Counsel',
                'agree': 'on',
            }, HTTP_USER_AGENT=UA)
        self.assertEqual(r.status_code, 302)  # redirects to signed page

        self.contract.refresh_from_db()
        self.assertTrue(self.contract.signed)
        self.assertEqual(self.contract.signed_name, 'Carla Counsel')
        self.assertIsNotNone(self.contract.signed_at)
        # Audit fields populated:
        self.assertEqual(self.contract.signed_user_agent[:50], UA[:50])
        self.assertTrue(self.contract.signed_content_hash)
        self.assertEqual(len(self.contract.signed_content_hash), 64)
        # Side effects fired:
        mock_dep.assert_called_once()
        mock_email.assert_called_once()
        # Client state advanced:
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.stage, 'intake')
        self.assertEqual(self.profile.payment_status, 'awaiting_deposit')

    def test_signed_content_hash_reproduces_sha256(self):
        """Phase 2.3 — the stored hash must be sha256(contract_text)."""
        from unittest.mock import patch
        import hashlib
        with patch('billing.stripe_helpers.issue_deposit_invoice'), \
             patch('clients.views.render_contract_pdf', return_value=''), \
             patch('clients.views.send_contract_signed_email'):
            self.client.post(self.sign_url, data={
                'signed_name': 'Carla',
                'agree': 'on',
            })
        self.contract.refresh_from_db()
        expected = hashlib.sha256(
            self.contract.contract_text.encode('utf-8')).hexdigest()
        self.assertEqual(self.contract.signed_content_hash, expected)

    def test_already_signed_renders_locked_page(self):
        from unittest.mock import patch
        with patch('billing.stripe_helpers.issue_deposit_invoice'), \
             patch('clients.views.render_contract_pdf', return_value=''), \
             patch('clients.views.send_contract_signed_email'):
            self.client.post(self.sign_url, data={
                'signed_name': 'First Signer', 'agree': 'on',
            })
        # A second GET should render the "already signed" view, not the form.
        r = self.client.get(self.sign_url)
        self.assertEqual(r.status_code, 200)
        # Page should NOT show the sign form again — we look for the
        # absence of an input named 'signed_name' which only the form has.
        self.assertNotContains(r, 'name="signed_name"')


class RenderContractPdfFallbackTests(TestCase):
    """WeasyPrint failure (e.g. on Windows) falls back to .html so the
    signed record is still persisted on disk."""

    @classmethod
    def setUpTestData(cls):
        from clients.models import Contract
        from decimal import Decimal
        u = User.objects.create_user(
            username='pdfclient1', password='x',
            email='pdfclient1@example.com')
        cls.profile = ClientProfile.objects.create(
            user=u, firm_name='PDF LLC')
        cls.contract = Contract.objects.create(
            client=cls.profile,
            package='website-essential',
            build_price=Decimal('2500'),
            deposit_amount=Decimal('1250'),
            timeline_weeks=4,
            contract_text='<p>tiny</p>',
            signed=True, signed_name='Test',
            signed_ip='127.0.0.1',
            signed_at=timezone.now(),
        )

    def test_fallback_to_html_when_weasyprint_fails(self):
        """Phase 3.4 AC — WeasyPrint import failure (e.g. Windows dev
        without libpango/libcairo) must fall back to writing an .html
        file so the signed contract is still persisted."""
        import tempfile
        from clients.pdf_utils import render_contract_pdf
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with override_settings(MEDIA_ROOT=tmp):
                # Force weasyprint to be unimportable at the function-
                # local `from weasyprint import ...` line.
                import builtins
                real_import = builtins.__import__

                def fake_import(name, *args, **kwargs):
                    if name == 'weasyprint':
                        raise ImportError('libpango missing')
                    return real_import(name, *args, **kwargs)
                with patch('builtins.__import__', side_effect=fake_import):
                    path = render_contract_pdf(self.contract)
        # Fallback returns a path string ending in .html (relative).
        self.assertIsInstance(path, str)
        self.assertTrue(path.endswith('.html'),
                        f'expected .html fallback, got {path!r}')


def _seed_contract_tiers():
    """Create the three ServiceTiers the combined-contract picker needs."""
    from decimal import Decimal

    from billing.pricing_models import ServiceTier
    ServiceTier.objects.get_or_create(
        slug='website-essential', defaults=dict(
            category='website_build', name='Essential Build',
            price=Decimal('2500'), is_recurring=False, billing_interval='',
            timeline_weeks=3, pages_included=8, practice_areas_included=5))
    ServiceTier.objects.get_or_create(
        slug='maintenance-growth', defaults=dict(
            category='maintenance', name='Growth', price=Decimal('599'),
            is_recurring=True, billing_interval='month'))
    ServiceTier.objects.get_or_create(
        slug='social-standard', defaults=dict(
            category='social_media', name='Standard', price=Decimal('699'),
            is_recurring=True, billing_interval='month'))


class CombinedContractGeneratorTests(TestCase):
    """clients.contract_template.generate_combined_contract_text."""

    @classmethod
    def setUpTestData(cls):
        _seed_contract_tiers()
        u = User.objects.create_user(
            username='cg1', password='x', email='cg1@example.com')
        cls.profile = ClientProfile.objects.create(
            user=u, firm_name='Combo LLC', contact_name='Cody Combo')

    def _tier(self, slug):
        from billing.pricing_models import ServiceTier
        return ServiceTier.objects.get(slug=slug)

    def test_single_build_text(self):
        from clients.contract_template import generate_combined_contract_text
        text = generate_combined_contract_text(
            self.profile,
            [{'service_type': 'build', 'tier': self._tier('website-essential')}])
        self.assertIn('Website Development', text)
        self.assertIn('Combo LLC', text)
        self.assertIn('$2,500', text)
        self.assertIn('$1,250', text)  # 50% deposit
        # No recurring section for a build-only contract.
        self.assertNotIn('Recurring Services', text)

    def test_all_three_services_text(self):
        from clients.contract_template import generate_combined_contract_text
        text = generate_combined_contract_text(self.profile, [
            {'service_type': 'build', 'tier': self._tier('website-essential')},
            {'service_type': 'maintenance', 'tier': self._tier('maintenance-growth')},
            {'service_type': 'social', 'tier': self._tier('social-standard')},
        ])
        self.assertIn('Website Development', text)
        self.assertIn('Website Maintenance', text)
        self.assertIn('Social Media Marketing', text)
        self.assertIn('$599 per month', text)
        self.assertIn('$699 per month', text)
        self.assertIn('Recurring Services', text)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class AccountSendContractTests(TestCase):
    """The Account-dashboard 'Generate & send contract' endpoint."""

    @classmethod
    def setUpTestData(cls):
        _seed_contract_tiers()
        from clients.account_models import Account
        # Staff operator who drives the dashboard.
        cls.staff = User.objects.create_user(
            username='op1', password='x', email='op1@example.com',
            is_staff=True)
        # The client account — created the production way: a ClientProfile is
        # made, and clients.signals auto-creates the linked Account.
        cls.client_user = User.objects.create_user(
            username='acct1', password='x', email='acct1@example.com')
        cls.profile = ClientProfile.objects.create(
            user=cls.client_user, firm_name='Sendme LLC')
        cls.account = Account.objects.get(legacy_client_profile=cls.profile)
        cls.url = reverse('admin_dashboard:account_send_contract',
                          args=[cls.account.id])

    def setUp(self):
        self.client.force_login(self.staff)

    def test_detail_page_renders_contract_card(self):
        # GET the account page so a template error in the new Contracts
        # card surfaces (the other tests only POST to the endpoint).
        detail_url = reverse('admin_dashboard:account_detail',
                             args=[self.account.id])
        r = self.client.get(detail_url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Contracts')
        self.assertContains(r, 'Generate &amp; send contract')

    def test_requires_staff(self):
        self.client.logout()
        r = self.client.post(self.url, data={'svc_build': 'on',
                                             'tier_build': 'website-essential'})
        self.assertIn(r.status_code, (302, 403))  # redirected to login

    def test_no_services_selected_errors(self):
        from clients.models import Contract
        r = self.client.post(self.url, data={}, follow=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Contract.objects.filter(account=self.account).count(), 0)

    def test_service_without_tier_errors(self):
        from clients.models import Contract
        # Checked build but left tier blank.
        self.client.post(self.url, data={'svc_build': 'on', 'tier_build': ''})
        self.assertEqual(Contract.objects.filter(account=self.account).count(), 0)

    def test_single_build_creates_contract(self):
        from clients.models import Contract
        from decimal import Decimal
        self.client.post(self.url, data={
            'svc_build': 'on', 'tier_build': 'website-essential'})
        c = Contract.objects.get(account=self.account)
        self.assertEqual(c.package, 'essential_build')
        self.assertEqual(c.build_price, Decimal('2500'))
        self.assertEqual(c.deposit_amount, Decimal('1250.00'))
        self.assertEqual(c.services.count(), 1)
        self.assertTrue(c.includes_build)
        # Auto-linked a ClientProfile to the account.
        self.account.refresh_from_db()
        self.assertIsNotNone(self.account.legacy_client_profile_id)

    def test_all_three_creates_three_service_rows(self):
        from clients.models import Contract
        self.client.post(self.url, data={
            'svc_build': 'on', 'tier_build': 'website-essential',
            'svc_maintenance': 'on', 'tier_maintenance': 'maintenance-growth',
            'svc_social': 'on', 'tier_social': 'social-standard',
        })
        c = Contract.objects.get(account=self.account)
        self.assertEqual(c.services.count(), 3)
        types = set(c.services.values_list('service_type', flat=True))
        self.assertEqual(types, {'build', 'maintenance', 'social'})
        self.assertTrue(c.includes_build)

    def test_maintenance_only_contract(self):
        from clients.models import Contract
        self.client.post(self.url, data={
            'svc_maintenance': 'on', 'tier_maintenance': 'maintenance-growth'})
        c = Contract.objects.get(account=self.account)
        self.assertEqual(c.package, '')
        self.assertIsNone(c.build_price)
        self.assertFalse(c.includes_build)
        self.assertEqual(c.services.count(), 1)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class MaintenanceOnlySigningTests(TestCase):
    """Signing a non-build contract records the agreement but does NOT
    fire the build-only deposit-invoice / awaiting-deposit side effects."""

    @classmethod
    def setUpTestData(cls):
        from clients.models import Contract, ContractService
        from decimal import Decimal
        u = User.objects.create_user(
            username='mo1', password='x', email='mo1@example.com')
        cls.profile = ClientProfile.objects.create(
            user=u, firm_name='Maint Only LLC')
        cls.contract = Contract.objects.create(
            client=cls.profile, package='', build_price=None,
            deposit_amount=None, timeline_weeks=0,
            contract_text='<h1>Maintenance Agreement</h1>')
        ContractService.objects.create(
            contract=cls.contract, service_type='maintenance',
            tier_slug='maintenance-growth', tier_name='Growth',
            price=Decimal('599'), is_recurring=True, billing_interval='month')
        cls.sign_url = reverse('clients:contract_sign',
                               args=[cls.contract.contract_token])

    def test_sign_does_not_issue_deposit_invoice(self):
        from unittest.mock import patch
        # Set distinctive state first; the build block (if it wrongly ran)
        # would overwrite these to stage='intake'/payment='awaiting_deposit'.
        self.profile.stage = 'live'
        self.profile.payment_status = 'fully_paid'
        self.profile.save(update_fields=['stage', 'payment_status'])
        with patch('billing.stripe_helpers.issue_deposit_invoice') as mock_dep, \
             patch('clients.views.render_contract_pdf', return_value=''), \
             patch('clients.views.send_contract_signed_email'):
            r = self.client.post(self.sign_url, data={
                'signed_name': 'Mo Owner', 'agree': 'on'})
        self.assertEqual(r.status_code, 302)
        self.contract.refresh_from_db()
        self.assertTrue(self.contract.signed)
        mock_dep.assert_not_called()
        # Client was NOT pushed into the build onboarding pipeline.
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.stage, 'live')
        self.assertEqual(self.profile.payment_status, 'fully_paid')
