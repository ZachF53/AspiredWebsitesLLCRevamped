"""Tests for the PIN-gated credentials vault and the site changelog."""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
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
