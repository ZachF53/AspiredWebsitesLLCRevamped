"""Tests for the vault — credentials, signals, vault-level TOTP, terminal."""

import os

import pyotp
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientProfile
from vault.crypto import (
    derive_key,
    encrypt_value,
    generate_salt,
    hash_pin,
    wrap_key,
)
from vault.models import (
    ServerCommandLibrary,
    SSHSessionLog,
    VaultConfig,
    VaultCredential,
)
from vault.ssh_helpers import (
    is_vault_session_authenticated,
    vault_session_remaining_seconds,
)
from vault.totp_helpers import (
    ACCOUNT_NAME,
    ISSUER_NAME,
    generate_qr_code_base64,
    generate_totp_secret,
    get_totp_uri,
    verify_totp_code,
)

User = get_user_model()
_seq = 0


def _client(firm='SSH Co'):
    global _seq
    _seq += 1
    user = User.objects.create_user(username=f'cu{_seq}', password='x')
    return ClientProfile.objects.create(user=user, firm_name=firm)


# ── TOTP helpers ────────────────────────────────────────────────────────────

class TotpHelperTests(TestCase):

    def test_generate_and_verify(self):
        secret = generate_totp_secret()
        self.assertTrue(verify_totp_code(secret, pyotp.TOTP(secret).now()))
        self.assertFalse(verify_totp_code(secret, '000000'))
        self.assertFalse(verify_totp_code(secret, ''))

    def test_uri_carries_aspired_branding_and_image(self):
        uri = get_totp_uri(generate_totp_secret())
        self.assertTrue(uri.startswith('otpauth://totp/'))
        # Branded entry name (URL-encoded "Aspired Websites Servers").
        self.assertIn('Aspired%20Websites%20Servers', uri)
        self.assertIn('admin%40aspiredwebsites.com', uri)
        # Image parameter for apps that respect it (Aegis, 1Password, etc.).
        self.assertIn('image=', uri)
        self.assertIn('favicon-32x32.png', uri)

    def test_qr_code_is_base64_png(self):
        uri = get_totp_uri(generate_totp_secret())
        qr = generate_qr_code_base64(uri)
        import base64
        self.assertTrue(base64.b64decode(qr).startswith(b'\x89PNG'))


# ── Vault-session helpers ───────────────────────────────────────────────────

class VaultSessionHelperTests(TestCase):

    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, session_data):
        req = self.factory.get('/')
        req.session = session_data
        return req

    def test_unauth_without_unlock(self):
        self.assertFalse(is_vault_session_authenticated(self._request({})))
        self.assertEqual(vault_session_remaining_seconds(self._request({})), 0)

    def test_unauth_with_unlock_but_no_totp(self):
        req = self._request({
            'vault_unlocked_at': timezone.now().isoformat(),
        })
        self.assertFalse(is_vault_session_authenticated(req))
        self.assertGreater(vault_session_remaining_seconds(req), 3000)

    def test_authed_with_unlock_and_totp(self):
        req = self._request({
            'vault_unlocked_at': timezone.now().isoformat(),
            'vault_totp_verified': True,
        })
        self.assertTrue(is_vault_session_authenticated(req))


# ── Models + signal ─────────────────────────────────────────────────────────

class SSHCredentialModelTests(TestCase):

    def test_ssh_credential_seeds_default_commands(self):
        profile = _client()
        cred = VaultCredential.objects.create(
            vault=profile.vault, label='Prod Server', category='server',
            is_ssh_credential=True)
        self.assertEqual(cred.commands.count(), 10)
        self.assertTrue(cred.commands.filter(
            label='Check all services').exists())

    def test_non_ssh_credential_seeds_nothing(self):
        profile = _client()
        cred = VaultCredential.objects.create(
            vault=profile.vault, label='Gmail', category='google')
        self.assertEqual(cred.commands.count(), 0)

    def test_session_log_str(self):
        profile = _client()
        cred = VaultCredential.objects.create(
            vault=profile.vault, label='S', is_ssh_credential=True)
        log = SSHSessionLog.objects.create(credential=cred)
        self.assertIn('S', str(log))


# ── Vault-level TOTP setup + combined PIN/TOTP unlock + terminal access ─────

class VaultTotpAndTerminalViewTests(TestCase):

    def setUp(self):
        self.staff = User.objects.create_user(
            username='vstaff', password='vp', is_staff=True)
        self.client.login(username='vstaff', password='vp')

        # Real vault PIN setup so derive_key()/verify_pin() round-trip.
        self.pin = '4321'
        self.salt = generate_salt()
        self.vault_key = derive_key(self.pin, self.salt)
        cfg = VaultConfig.get()
        cfg.encryption_salt = self.salt
        cfg.pin_hash = hash_pin(self.pin, self.salt)
        cfg.pin_set = True
        cfg.save()
        self.cfg = cfg

        self.profile = _client('Terminal Co')
        self.cred = VaultCredential.objects.create(
            vault=self.profile.vault, label='Prod', category='server',
            is_ssh_credential=True,
            ssh_host_encrypted=encrypt_value('161.35.108.209', self.vault_key),
            ssh_username_encrypted=encrypt_value('root', self.vault_key),
            ssh_auth_type='password',
            ssh_password_encrypted=encrypt_value('secret', self.vault_key),
        )

    def _unlock(self, totp_verified=False):
        session = self.client.session
        session['vault_unlocked_at'] = timezone.now().isoformat()
        session['vault_key_wrapped'] = wrap_key(self.vault_key)
        if totp_verified:
            session['vault_totp_verified'] = True
        session.save()

    def _enroll_totp(self):
        """Mark VaultConfig totp_configured + return the secret in use."""
        secret = generate_totp_secret()
        self.cfg.totp_secret_encrypted = encrypt_value(secret, self.vault_key)
        self.cfg.totp_configured = True
        self.cfg.save()
        return secret

    # ── TOTP setup view ────────────────────────────────────────────────────

    def test_totp_setup_requires_unlocked_vault(self):
        resp = self.client.get(reverse('vault:totp_setup'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('vault:home'), resp.url)

    def test_totp_setup_shows_qr_and_branding(self):
        self._unlock()
        resp = self.client.get(reverse('vault:totp_setup'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'data:image/png;base64,')
        self.assertContains(resp, ISSUER_NAME)
        self.assertContains(resp, ACCOUNT_NAME)

    def test_totp_setup_enrolls_on_vault_config(self):
        self._unlock()
        # GET first so the per-session secret is stashed.
        self.client.get(reverse('vault:totp_setup'))
        secret = self.client.session['vault_totp_setup_secret']
        resp = self.client.post(
            reverse('vault:totp_setup'),
            {'code': pyotp.TOTP(secret).now()})
        # Successful enrolment → redirect back to vault home.
        self.assertEqual(resp.status_code, 302)
        self.cfg.refresh_from_db()
        self.assertTrue(self.cfg.totp_configured)
        self.assertTrue(self.cfg.totp_secret_encrypted)
        # Session is now marked TOTP-verified, no re-prompt.
        self.assertTrue(self.client.session.get('vault_totp_verified'))

    def test_totp_setup_redirects_when_already_configured(self):
        self._unlock()
        self._enroll_totp()
        resp = self.client.get(reverse('vault:totp_setup'))
        self.assertRedirects(resp, reverse('vault:home'))

    # ── Combined PIN + TOTP unlock ─────────────────────────────────────────

    def test_unlock_requires_both_pin_and_totp(self):
        self._enroll_totp()
        # PIN right, TOTP wrong.
        resp = self.client.post(reverse('vault:home'), {
            'd1': self.pin[0], 'd2': self.pin[1],
            'd3': self.pin[2], 'd4': self.pin[3],
            't1': '0', 't2': '0', 't3': '0',
            't4': '0', 't5': '0', 't6': '0',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Incorrect authenticator code')
        # Session NOT unlocked.
        self.assertNotIn('vault_key_wrapped', self.client.session)

    def test_unlock_with_pin_and_totp_succeeds(self):
        secret = self._enroll_totp()
        code = pyotp.TOTP(secret).now()
        resp = self.client.post(reverse('vault:home'), {
            'd1': self.pin[0], 'd2': self.pin[1],
            'd3': self.pin[2], 'd4': self.pin[3],
            **{f't{i + 1}': code[i] for i in range(6)},
        })
        # Successful unlock → redirect to vault home.
        self.assertEqual(resp.status_code, 302)
        self.assertIn('vault_key_wrapped', self.client.session)
        self.assertTrue(self.client.session.get('vault_totp_verified'))

    def test_pin_setup_redirects_to_totp_setup(self):
        # Wipe the pre-set PIN so this is genuinely first-time setup.
        cfg = VaultConfig.get()
        cfg.pin_set = False
        cfg.pin_hash = ''
        cfg.encryption_salt = b''
        cfg.save()
        resp = self.client.post(reverse('vault:home'), {
            'pin': '9876', 'pin_confirm': '9876',
        })
        self.assertRedirects(resp, reverse('vault:totp_setup'))

    # ── Terminal access ────────────────────────────────────────────────────

    def test_terminal_redirects_when_locked(self):
        # No unlock at all — straight to PIN gate.
        resp = self.client.get(reverse('vault:terminal', args=[self.cred.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('vault:home'), resp.url)

    def test_terminal_redirects_when_totp_not_verified(self):
        self._enroll_totp()
        self._unlock(totp_verified=False)
        resp = self.client.get(reverse('vault:terminal', args=[self.cred.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('vault:home'), resp.url)

    def test_terminal_opens_when_totp_verified(self):
        self._enroll_totp()
        self._unlock(totp_verified=True)
        resp = self.client.get(reverse('vault:terminal', args=[self.cred.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'vault-terminal.js')
        self.assertContains(resp, 'xterm/xterm.js')
        self.assertContains(resp, 'data-cred-id')

    def test_second_terminal_opens_without_re_totp(self):
        """Once vault is unlocked + TOTP verified, any SSH cred opens directly."""
        self._enroll_totp()
        self._unlock(totp_verified=True)
        other = VaultCredential.objects.create(
            vault=self.profile.vault, label='Staging', category='server',
            is_ssh_credential=True,
            ssh_host_encrypted=encrypt_value('10.0.0.5', self.vault_key),
            ssh_username_encrypted=encrypt_value('root', self.vault_key),
            ssh_auth_type='password',
            ssh_password_encrypted=encrypt_value('s', self.vault_key),
        )
        self.assertEqual(
            self.client.get(reverse('vault:terminal', args=[self.cred.id]))
            .status_code, 200)
        self.assertEqual(
            self.client.get(reverse('vault:terminal', args=[other.id]))
            .status_code, 200)

    # ── Command library still works under the new gate ─────────────────────

    def test_command_library_add(self):
        self._unlock()
        resp = self.client.post(
            reverse('vault:command_library', args=[self.cred.id]),
            {'label': 'List home', 'command': 'ls -la', 'category': 'custom',
             'sort_order': 1})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(ServerCommandLibrary.objects.filter(
            credential=self.cred, label='List home').exists())

    def test_command_inline_edit(self):
        cmd = ServerCommandLibrary.objects.create(
            credential=self.cred, label='Old label', command='ls',
            category='custom')
        resp = self.client.get(
            reverse('vault:command_edit', args=[self.cred.id, cmd.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Old label')
        resp = self.client.post(
            reverse('vault:command_edit', args=[self.cred.id, cmd.id]),
            {'label': 'New label', 'command': 'pwd', 'category': 'custom',
             'sort_order': 3})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'New label')
        cmd.refresh_from_db()
        self.assertEqual(cmd.label, 'New label')

    def test_command_row_partial(self):
        cmd = ServerCommandLibrary.objects.create(
            credential=self.cred, label='Row test', command='id',
            category='custom')
        resp = self.client.get(
            reverse('vault:command_row', args=[self.cred.id, cmd.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Row test')


# ── Consumer module ─────────────────────────────────────────────────────────

class ConsumerImportTests(TestCase):

    def test_consumer_and_routing_import(self):
        from vault import consumers, routing
        self.assertTrue(hasattr(consumers, 'SSHTerminalConsumer'))
        self.assertEqual(len(routing.websocket_urlpatterns), 1)
