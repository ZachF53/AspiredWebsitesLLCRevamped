"""Tests for Phase 6a — SSH credential vault, TOTP, command library."""

import os

import pyotp
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientProfile
from vault.crypto import encrypt_value, wrap_key
from vault.models import ServerCommandLibrary, SSHSessionLog, VaultCredential
from vault.ssh_helpers import (
    is_ssh_session_valid,
    mark_ssh_session_verified,
    ssh_session_remaining_seconds,
)
from vault.totp_helpers import (
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

    def test_qr_code_is_base64_png(self):
        uri = get_totp_uri(generate_totp_secret(), 'Test Server')
        self.assertTrue(uri.startswith('otpauth://totp/'))
        qr = generate_qr_code_base64(uri)
        import base64
        self.assertTrue(base64.b64decode(qr).startswith(b'\x89PNG'))


# ── SSH session helpers ─────────────────────────────────────────────────────

class SSHSessionHelperTests(TestCase):

    def setUp(self):
        self.factory_session = self.client.session

    def test_mark_and_validate(self):
        request = type('R', (), {'session': {}})()
        mark_ssh_session_verified(request, 'abc')
        self.assertTrue(is_ssh_session_valid(request, 'abc'))
        self.assertGreater(ssh_session_remaining_seconds(request, 'abc'), 800)

    def test_unverified_is_invalid(self):
        request = type('R', (), {'session': {}})()
        self.assertFalse(is_ssh_session_valid(request, 'abc'))
        self.assertEqual(ssh_session_remaining_seconds(request, 'abc'), 0)


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


# ── TOTP setup / connect / terminal views ───────────────────────────────────

class SSHTerminalViewTests(TestCase):

    def setUp(self):
        self.staff = User.objects.create_user(
            username='vstaff', password='vp', is_staff=True)
        self.client.login(username='vstaff', password='vp')
        self.profile = _client('Terminal Co')
        self.vault_key = os.urandom(32)
        self.cred = VaultCredential.objects.create(
            vault=self.profile.vault, label='Prod', category='server',
            is_ssh_credential=True,
            ssh_host_encrypted=encrypt_value('161.35.108.209', self.vault_key),
            ssh_username_encrypted=encrypt_value('root', self.vault_key),
            ssh_auth_type='password',
            ssh_password_encrypted=encrypt_value('secret', self.vault_key),
        )

    def _unlock(self):
        session = self.client.session
        session['vault_unlocked_at'] = timezone.now().isoformat()
        session['vault_key_wrapped'] = wrap_key(self.vault_key)
        session.save()

    def test_totp_setup_requires_unlocked_vault(self):
        resp = self.client.get(
            reverse('vault:totp_setup', args=[self.cred.id]))
        self.assertEqual(resp.status_code, 302)  # → vault PIN gate

    def test_totp_setup_shows_qr_and_verifies(self):
        self._unlock()
        resp = self.client.get(
            reverse('vault:totp_setup', args=[self.cred.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'data:image/png;base64,')
        secret = self.client.session[f'totp_setup_secret_{self.cred.id}']
        resp = self.client.post(
            reverse('vault:totp_setup', args=[self.cred.id]),
            {'code': pyotp.TOTP(secret).now()})
        self.cred.refresh_from_db()
        self.assertTrue(self.cred.totp_configured)

    def _configure_totp(self):
        secret = generate_totp_secret()
        self.cred.totp_secret_encrypted = encrypt_value(secret, self.vault_key)
        self.cred.totp_configured = True
        self.cred.save()
        return secret

    def test_connect_redirects_to_setup_when_unconfigured(self):
        self._unlock()
        resp = self.client.get(
            reverse('vault:totp_connect', args=[self.cred.id]))
        self.assertRedirects(resp, reverse(
            'vault:totp_setup', args=[self.cred.id]))

    def test_connect_verifies_and_opens_terminal(self):
        self._unlock()
        secret = self._configure_totp()
        code = pyotp.TOTP(secret).now()
        resp = self.client.post(
            reverse('vault:totp_connect', args=[self.cred.id]),
            {f'd{i + 1}': code[i] for i in range(6)})
        self.assertRedirects(resp, reverse(
            'vault:terminal', args=[self.cred.id]))

    def test_terminal_requires_totp_session(self):
        self._unlock()
        self._configure_totp()
        resp = self.client.get(reverse('vault:terminal', args=[self.cred.id]))
        self.assertRedirects(resp, reverse(
            'vault:totp_connect', args=[self.cred.id]))

    def test_terminal_renders_after_totp(self):
        self._unlock()
        secret = self._configure_totp()
        self.client.post(
            reverse('vault:totp_connect', args=[self.cred.id]),
            {f'd{i + 1}': pyotp.TOTP(secret).now()[i] for i in range(6)})
        resp = self.client.get(reverse('vault:terminal', args=[self.cred.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'vault-terminal.js')
        self.assertContains(resp, 'xterm/xterm.js')
        self.assertContains(resp, 'data-cred-id')

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
        self.assertEqual(cmd.command, 'pwd')

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
