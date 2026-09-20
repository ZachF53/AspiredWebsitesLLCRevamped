"""
Automation-safe SSH credential copy — VaultCredential.
automation_ssh_private_key_encrypted, populated at creation and
refreshed on manual edit, never touched by
reencrypt_credential_with_pin_key. See vault/ssh_ops.py.

Also covers the vault__account_new_id fix in vault.ssh_ops
(open_automation_ssh) — the previous bare account_new_id lookup in
billing.do_helpers._open_ssh_to_site raised FieldError on every call,
uncaught, so set_site_maintenance_mode/restore_client_site's SSH path
never actually ran.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account, Website
from vault.crypto import (
    decrypt_value, derive_key, derive_server_key, encrypt_value,
    generate_salt, hash_pin,
)
from vault.models import ClientVault, VaultConfig, VaultCredential
from vault.ssh_ops import _automation_credential, open_automation_ssh

User = get_user_model()
_seq = 0

TEST_KEY = (
    '-----BEGIN OPENSSH PRIVATE KEY-----\n'
    'not-a-real-key-just-fixture-text\n'
    '-----END OPENSSH PRIVATE KEY-----\n'
)


def _account_website(droplet_ip='192.0.2.10'):
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'ssh{_seq}', email=f'ssh{_seq}@example.com', password='x')
    account = Account.objects.create(user=u, name=f'SSH Co {_seq}')
    website = Website.objects.create(
        account=account, name=f'SSH Site {_seq}', build_platform='custom',
        do_droplet_ip=droplet_ip)
    return account, website


class CreateSSHVaultCredentialTests(TestCase):

    def test_populates_both_key_fields_identically(self):
        from billing.do_helpers import _create_ssh_vault_credential

        account, website = _account_website()
        cred = _create_ssh_vault_credential(
            website, website.do_droplet_ip, TEST_KEY)

        self.assertTrue(cred.encrypted_with_server_key)
        self.assertTrue(cred.automation_access_enabled)
        self.assertNotEqual(cred.automation_ssh_private_key_encrypted, '')

        server_key = derive_server_key()
        self.assertEqual(
            decrypt_value(cred.ssh_private_key_encrypted, server_key),
            TEST_KEY)
        self.assertEqual(
            decrypt_value(
                cred.automation_ssh_private_key_encrypted, server_key),
            TEST_KEY)


class ReencryptLeavesAutomationCopyAloneTests(TestCase):

    def test_pin_reencrypt_does_not_touch_automation_field(self):
        from vault.crypto import (
            derive_key, encrypt_value, generate_salt,
            reencrypt_credential_with_pin_key,
        )

        account, website = _account_website()
        vault, _ = ClientVault.objects.get_or_create(account_new=account)
        server_key = derive_server_key()
        cred = VaultCredential.objects.create(
            vault=vault, website_new=website, label='Prod',
            category='server', is_ssh_credential=True,
            ssh_auth_type='private_key',
            ssh_private_key_encrypted=encrypt_value(TEST_KEY, server_key),
            automation_ssh_private_key_encrypted=encrypt_value(
                TEST_KEY, server_key),
            encrypted_with_server_key=True,
        )
        before = cred.automation_ssh_private_key_encrypted

        pin_key = derive_key('123456', generate_salt())
        changed = reencrypt_credential_with_pin_key(cred, pin_key)

        self.assertTrue(changed)
        cred.refresh_from_db()
        self.assertFalse(cred.encrypted_with_server_key)
        # Main field is now PIN-encrypted (different ciphertext)...
        self.assertNotEqual(
            decrypt_value(cred.ssh_private_key_encrypted, server_key),
            TEST_KEY)
        self.assertEqual(
            decrypt_value(cred.ssh_private_key_encrypted, pin_key),
            TEST_KEY)
        # ...but the automation copy is untouched and still
        # server-key-decryptable.
        self.assertEqual(cred.automation_ssh_private_key_encrypted, before)
        self.assertEqual(
            decrypt_value(
                cred.automation_ssh_private_key_encrypted, server_key),
            TEST_KEY)


class OpenAutomationSSHTests(TestCase):

    def _cred(self, account, website, **overrides):
        vault, _ = ClientVault.objects.get_or_create(account_new=account)
        server_key = derive_server_key()
        defaults = dict(
            vault=vault, website_new=website, label='Prod',
            category='server', is_ssh_credential=True,
            ssh_auth_type='private_key',
            automation_ssh_private_key_encrypted=encrypt_value(
                TEST_KEY, server_key),
            automation_access_enabled=True,
        )
        defaults.update(overrides)
        return VaultCredential.objects.create(**defaults)

    def test_no_droplet_ip_returns_none(self):
        account, website = _account_website(droplet_ip=None)
        website.do_droplet_ip = None
        website.save(update_fields=['do_droplet_ip'])
        self.assertIsNone(open_automation_ssh(website))

    def test_no_credential_returns_none(self):
        account, website = _account_website()
        self.assertIsNone(open_automation_ssh(website))

    def test_automation_disabled_is_excluded(self):
        account, website = _account_website()
        self._cred(account, website, automation_access_enabled=False)
        self.assertIsNone(_automation_credential(website))

    def test_automation_enabled_credential_is_found(self):
        account, website = _account_website()
        cred = self._cred(account, website)
        found = _automation_credential(website)
        self.assertEqual(found.pk, cred.pk)

    @patch('vault.ssh_ops._load_private_key')
    @patch('paramiko.SSHClient')
    def test_successful_connect_stamps_last_used(
            self, mock_ssh_cls, mock_load_key):
        account, website = _account_website()
        cred = self._cred(account, website)
        mock_ssh_cls.return_value = MagicMock()
        mock_load_key.return_value = MagicMock()

        ssh = open_automation_ssh(website)
        self.assertIsNotNone(ssh)

        cred.refresh_from_db()
        self.assertIsNotNone(cred.automation_key_last_used_at)

    def test_connect_failure_returns_none(self):
        account, website = _account_website()
        self._cred(account, website)
        with patch('paramiko.SSHClient') as mock_ssh_cls:
            instance = MagicMock()
            instance.connect.side_effect = OSError('unreachable')
            mock_ssh_cls.return_value = instance
            self.assertIsNone(open_automation_ssh(website))


class DoHelpersOpenSSHDelegatesTests(TestCase):
    """billing.do_helpers._open_ssh_to_site used to filter VaultCredential
    by a bare account_new_id kwarg — not a real field, FieldError every
    call. Now it delegates to vault.ssh_ops, which uses the correct
    vault__account_new_id traversal."""

    def test_open_ssh_to_site_does_not_raise_and_finds_credential(self):
        from billing.do_helpers import _open_ssh_to_site

        account, website = _account_website()
        vault, _ = ClientVault.objects.get_or_create(account_new=account)
        server_key = derive_server_key()
        VaultCredential.objects.create(
            vault=vault, website_new=website, label='Prod',
            category='server', is_ssh_credential=True,
            ssh_auth_type='private_key',
            automation_ssh_private_key_encrypted=encrypt_value(
                TEST_KEY, server_key),
            automation_access_enabled=True,
        )

        with patch('paramiko.SSHClient') as mock_ssh_cls, \
                patch('vault.ssh_ops._load_private_key') as mock_load_key:
            mock_ssh_cls.return_value = MagicMock()
            mock_load_key.return_value = MagicMock()
            ssh = _open_ssh_to_site(website)
        self.assertIsNotNone(ssh)


class EditCredentialRefreshesAutomationCopyTests(TestCase):
    """Editing and saving an SSH credential's form always round-trips the
    decrypted private key through the textarea — this is the "one-click
    recovery" path for a credential whose automation copy is missing or
    stale (pre-existing rows, or a manually rotated key), with no need
    for a separate dedicated endpoint."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='vaultauto', password='vp', is_staff=True)
        self.client.login(username='vaultauto', password='vp')

        self.pin = '9999'
        self.salt = generate_salt()
        self.vault_key = derive_key(self.pin, self.salt)
        cfg = VaultConfig.get()
        cfg.encryption_salt = self.salt
        cfg.pin_hash = hash_pin(self.pin, self.salt)
        cfg.pin_set = True
        cfg.save()

        self.account, self.website = _account_website()
        self.vault, _ = ClientVault.objects.get_or_create(
            account_new=self.account)
        self.cred = VaultCredential.objects.create(
            vault=self.vault, website_new=self.website, label='Prod',
            category='server', is_ssh_credential=True,
            ssh_auth_type='private_key',
            ssh_host_encrypted=encrypt_value('192.0.2.10', self.vault_key),
            ssh_username_encrypted=encrypt_value('root', self.vault_key),
            ssh_private_key_encrypted=encrypt_value(
                TEST_KEY, self.vault_key),
            # Simulates a pre-existing credential from before this field
            # existed — empty automation copy, already PIN-encrypted.
            automation_ssh_private_key_encrypted='',
            encrypted_with_server_key=False,
        )

    def _unlock(self):
        from vault.crypto import wrap_key
        session = self.client.session
        session['vault_unlocked_at'] = timezone.now().isoformat()
        session['vault_key_wrapped'] = wrap_key(self.vault_key)
        session.save()

    def test_edit_form_shows_recovery_hint_when_not_configured(self):
        self._unlock()
        resp = self.client.get(reverse(
            'vault:edit_credential',
            args=[self.account.id, self.cred.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Automation access isn't set up")

    def test_saving_edit_form_populates_automation_copy(self):
        self._unlock()
        resp = self.client.post(
            reverse('vault:edit_credential',
                    args=[self.account.id, self.cred.id]),
            {
                'label': 'Prod', 'category': 'server',
                'credential_type': 'other', 'custom_label': 'Prod server', 'sort_order': 0,
                'is_ssh_credential': 'on',
                'ssh_host': '192.0.2.10', 'ssh_port': 22,
                'ssh_username': 'root', 'ssh_auth_type': 'private_key',
                'ssh_private_key': TEST_KEY,
                'automation_access_enabled': 'on',
            })
        self.assertEqual(resp.status_code, 302)

        self.cred.refresh_from_db()
        self.assertNotEqual(
            self.cred.automation_ssh_private_key_encrypted, '')
        # CharField.strip defaults True — the textarea value round-trips
        # through form cleaning with surrounding whitespace stripped.
        self.assertEqual(
            decrypt_value(
                self.cred.automation_ssh_private_key_encrypted,
                derive_server_key()),
            TEST_KEY.strip())

    def test_unchecking_automation_access_disables_it(self):
        self._unlock()
        resp = self.client.post(
            reverse('vault:edit_credential',
                    args=[self.account.id, self.cred.id]),
            {
                'label': 'Prod', 'category': 'server',
                'credential_type': 'other', 'custom_label': 'Prod server', 'sort_order': 0,
                'is_ssh_credential': 'on',
                'ssh_host': '192.0.2.10', 'ssh_port': 22,
                'ssh_username': 'root', 'ssh_auth_type': 'private_key',
                'ssh_private_key': TEST_KEY,
                # automation_access_enabled omitted == unchecked
            })
        self.assertEqual(resp.status_code, 302)

        self.cred.refresh_from_db()
        self.assertFalse(self.cred.automation_access_enabled)


class BackfillAutomationSSHKeysCommandTests(TestCase):

    def test_backfills_server_key_encrypted_credential(self):
        from io import StringIO
        from django.core.management import call_command

        account, website = _account_website()
        vault, _ = ClientVault.objects.get_or_create(account_new=account)
        server_key = derive_server_key()
        cred = VaultCredential.objects.create(
            vault=vault, website_new=website, label='Prod',
            category='server', is_ssh_credential=True,
            ssh_auth_type='private_key',
            ssh_private_key_encrypted=encrypt_value(TEST_KEY, server_key),
            automation_ssh_private_key_encrypted='',
            encrypted_with_server_key=True,
        )

        out = StringIO()
        call_command('backfill_automation_ssh_keys', stdout=out)

        cred.refresh_from_db()
        self.assertEqual(
            decrypt_value(
                cred.automation_ssh_private_key_encrypted, server_key),
            TEST_KEY)
        self.assertIn('backfilled: 1', out.getvalue())

    def test_reports_pin_encrypted_credential_as_needing_manual_save(self):
        from io import StringIO
        from django.core.management import call_command

        account, website = _account_website()
        vault, _ = ClientVault.objects.get_or_create(account_new=account)
        pin_key = derive_key('1234', generate_salt())
        cred = VaultCredential.objects.create(
            vault=vault, website_new=website, label='Prod',
            category='server', is_ssh_credential=True,
            ssh_auth_type='private_key',
            ssh_private_key_encrypted=encrypt_value(TEST_KEY, pin_key),
            automation_ssh_private_key_encrypted='',
            encrypted_with_server_key=False,
        )

        out = StringIO()
        call_command('backfill_automation_ssh_keys', stdout=out)

        cred.refresh_from_db()
        self.assertEqual(cred.automation_ssh_private_key_encrypted, '')
        self.assertIn('needs-manual-save: 1', out.getvalue())

    def test_dry_run_writes_nothing(self):
        from io import StringIO
        from django.core.management import call_command

        account, website = _account_website()
        vault, _ = ClientVault.objects.get_or_create(account_new=account)
        server_key = derive_server_key()
        cred = VaultCredential.objects.create(
            vault=vault, website_new=website, label='Prod',
            category='server', is_ssh_credential=True,
            ssh_auth_type='private_key',
            ssh_private_key_encrypted=encrypt_value(TEST_KEY, server_key),
            automation_ssh_private_key_encrypted='',
            encrypted_with_server_key=True,
        )

        out = StringIO()
        call_command('backfill_automation_ssh_keys', '--dry-run', stdout=out)

        cred.refresh_from_db()
        self.assertEqual(cred.automation_ssh_private_key_encrypted, '')
