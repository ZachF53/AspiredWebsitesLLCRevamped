"""Tests for the PIN-gated client portal credentials vault."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientProfile
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
