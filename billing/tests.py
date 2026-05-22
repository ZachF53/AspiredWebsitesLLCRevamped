"""
Tests for billing.do_helpers — the SSH-vault-key bootstrap path of
provision_client_droplet, plus the re-encryption hand-off into the vault
view. Paramiko + the DigitalOcean API are mocked everywhere; nothing here
actually talks to the network.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing import do_helpers
from clients.models import ClientProfile
from vault.crypto import (
    decrypt_value,
    derive_key,
    derive_server_key,
    encrypt_value,
    generate_salt,
    hash_pin,
    wrap_key,
)
from vault.models import (
    ClientVault,
    ServerCommandLibrary,
    VaultConfig,
    VaultCredential,
)

User = get_user_model()

# Use a stable, non-empty VAULT_SERVER_SECRET for every test so
# derive_server_key() is deterministic and never raises ValueError.
TEST_SETTINGS = {
    'VAULT_SERVER_SECRET': 'test-vault-server-secret-for-bootstrap',
}


# Minimal but valid PEM so the "looks like a real key" guard passes.
FAKE_PRIVATE_KEY = (
    '-----BEGIN OPENSSH PRIVATE KEY-----\n'
    'b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n'
    'AAAAGAAAABFAAAAAQAAAAEAAAAGFmYWtl\n'
    '-----END OPENSSH PRIVATE KEY-----\n'
)


def _exec_command_mock(captured_commands):
    """
    Build a fake paramiko exec_command() that records every command and
    returns canned stdout. `cat /root/.ssh/aspired_vault_key` returns the
    fake private key; every other command returns empty stdout + exit 0.
    """

    def fake_exec(command, timeout=None):
        captured_commands.append(command)
        stdout = MagicMock()
        if command.startswith('cat ') and 'aspired_vault_key' in command:
            stdout.read.return_value = FAKE_PRIVATE_KEY.encode('utf-8')
        else:
            stdout.read.return_value = b''
        stdout.channel.recv_exit_status.return_value = 0
        stderr = MagicMock()
        stderr.read.return_value = b''
        return (MagicMock(), stdout, stderr)

    return fake_exec


@override_settings(**TEST_SETTINGS)
class VaultKeyBootstrapTests(TestCase):

    def setUp(self):
        user = User.objects.create_user(username='c1', password='x')
        self.client_profile = ClientProfile.objects.create(
            user=user, firm_name='Bootstrap Co')

    @patch('billing.do_helpers.paramiko.SSHClient')
    def test_setup_vault_key_creates_credential_encrypted_with_server_key(
            self, ssh_client_cls):
        """
        Happy path — paramiko connects, runs every bootstrap command, captures
        the private key, and a server-key-encrypted VaultCredential lands in
        the client's vault.
        """
        captured = []
        instance = MagicMock()
        instance.exec_command.side_effect = _exec_command_mock(captured)
        ssh_client_cls.return_value = instance

        do_helpers.setup_vault_key_for_droplet(
            self.client_profile, '10.0.0.5', 'temp-pass', retry_delay=0)

        instance.connect.assert_called_once()
        connect_kwargs = instance.connect.call_args.kwargs
        self.assertEqual(connect_kwargs['hostname'], '10.0.0.5')
        self.assertEqual(connect_kwargs['username'], 'root')
        self.assertEqual(connect_kwargs['password'], 'temp-pass')

        # Sanity: the bootstrap script ran the keygen, the loopback test, and
        # the lockdown step.
        joined = '\n'.join(captured)
        self.assertIn('ssh-keygen -t ed25519', joined)
        self.assertIn('root@127.0.0.1 true', joined)
        self.assertIn('99-vault-lockdown.conf', joined)
        self.assertIn('passwd -l root', joined)

        cred = VaultCredential.objects.get(
            vault__client=self.client_profile, is_ssh_credential=True)
        self.assertTrue(cred.encrypted_with_server_key)
        self.assertEqual(cred.ssh_auth_type, 'private_key')
        # Host + private key decrypt under the server key — the PIN key isn't
        # available yet at provisioning time.
        server_key = derive_server_key()
        self.assertEqual(
            decrypt_value(cred.ssh_host_encrypted, server_key), '10.0.0.5')
        self.assertEqual(
            decrypt_value(cred.ssh_username_encrypted, server_key), 'root')
        self.assertIn(
            'BEGIN OPENSSH PRIVATE KEY',
            decrypt_value(cred.ssh_private_key_encrypted, server_key))

        # Default ServerCommandLibrary entries were seeded.
        self.assertEqual(
            ServerCommandLibrary.objects.filter(credential=cred).count(), 10)

    @patch('billing.do_helpers.time.sleep', return_value=None)
    @patch('billing.do_helpers.paramiko.SSHClient')
    def test_setup_retries_on_transient_ssh_error(
            self, ssh_client_cls, _sleep):
        """First two attempts fail to connect; the third succeeds."""
        import paramiko

        captured = []
        success_instance = MagicMock()
        success_instance.exec_command.side_effect = _exec_command_mock(captured)

        fail_instance = MagicMock()
        fail_instance.connect.side_effect = paramiko.SSHException('boot')

        fail_instance_2 = MagicMock()
        fail_instance_2.connect.side_effect = paramiko.SSHException('boot')

        ssh_client_cls.side_effect = [
            fail_instance, fail_instance_2, success_instance]

        do_helpers.setup_vault_key_for_droplet(
            self.client_profile, '10.0.0.5', 'temp-pass',
            max_retries=3, retry_delay=0)

        self.assertEqual(ssh_client_cls.call_count, 3)
        self.assertEqual(
            VaultCredential.objects.filter(
                vault__client=self.client_profile).count(), 1)

    @patch('billing.do_helpers.time.sleep', return_value=None)
    @patch('billing.do_helpers.paramiko.SSHClient')
    def test_setup_raises_after_exhausting_retries(
            self, ssh_client_cls, _sleep):
        import paramiko

        fail = MagicMock()
        fail.connect.side_effect = paramiko.SSHException('nope')
        ssh_client_cls.return_value = fail

        with self.assertRaises(RuntimeError):
            do_helpers.setup_vault_key_for_droplet(
                self.client_profile, '10.0.0.5', 'temp-pass',
                max_retries=2, retry_delay=0)

        self.assertFalse(VaultCredential.objects.filter(
            vault__client=self.client_profile).exists())


@override_settings(**TEST_SETTINGS)
class ProvisionDropletIntegrationTests(TestCase):
    """provision_client_droplet — DO API + paramiko both mocked."""

    def setUp(self):
        user = User.objects.create_user(username='p1', password='x')
        self.client_profile = ClientProfile.objects.create(
            user=user, firm_name='Provision Co')

    def _droplet_payload(self, ip='10.0.0.99'):
        return {
            'droplet': {
                'id': 42,
                'status': 'active',
                'networks': {'v4': [{'type': 'public', 'ip_address': ip}]},
            }
        }

    @patch('billing.do_helpers.time.sleep', return_value=None)
    @patch('billing.do_helpers.paramiko.SSHClient')
    @patch('billing.do_helpers.requests.post')
    def test_provision_passes_temp_password_via_cloud_init(
            self, post, ssh_client_cls, _sleep):
        post.return_value = MagicMock(
            status_code=200,
            json=lambda: self._droplet_payload(),
            raise_for_status=lambda: None,
        )
        captured = []
        instance = MagicMock()
        instance.exec_command.side_effect = _exec_command_mock(captured)
        ssh_client_cls.return_value = instance

        with self.settings(DO_API_TOKEN='fake-token',
                           DO_BASE_SNAPSHOT_ID='snap-1'):
            do_helpers.provision_client_droplet(self.client_profile)

        # The temp password Provision generated must appear in the cloud-init
        # user_data that was POSTed to DO — and it must be the SAME password
        # paramiko was told to use.
        sent_payload = post.call_args.kwargs['json']
        self.assertIn('user_data', sent_payload)
        ssh_password = ssh_client_cls.return_value.connect.call_args.kwargs[
            'password']
        self.assertIn(f'root:{ssh_password}', sent_payload['user_data'])
        self.assertIn('ssh_pwauth: true', sent_payload['user_data'])

        # And the vault credential was created.
        self.assertTrue(VaultCredential.objects.filter(
            vault__client=self.client_profile,
            encrypted_with_server_key=True).exists())

    @patch('billing.do_helpers.time.sleep', return_value=None)
    @patch('billing.do_helpers.paramiko.SSHClient')
    @patch('billing.do_helpers.requests.post')
    def test_vault_setup_failure_stashes_password_does_not_block(
            self, post, ssh_client_cls, _sleep):
        import paramiko

        post.return_value = MagicMock(
            status_code=200,
            json=lambda: self._droplet_payload(),
            raise_for_status=lambda: None,
        )
        fail = MagicMock()
        fail.connect.side_effect = paramiko.SSHException('still booting')
        ssh_client_cls.return_value = fail

        with self.settings(DO_API_TOKEN='fake-token',
                           DO_BASE_SNAPSHOT_ID='snap-1'):
            # MUST NOT raise — provisioning succeeds even if vault setup fails.
            do_helpers.provision_client_droplet(self.client_profile)

        self.client_profile.refresh_from_db()
        self.assertEqual(self.client_profile.do_droplet_id, '42')
        self.assertIn(do_helpers.TEMP_PASSWORD_PREFIX,
                      self.client_profile.internal_notes)
        # No credential created — the SSH bootstrap never completed.
        self.assertFalse(VaultCredential.objects.filter(
            vault__client=self.client_profile).exists())


# ── Re-encryption hand-off in the vault view ────────────────────────────────

@override_settings(**TEST_SETTINGS)
class ReEncryptionOnClientVaultViewTests(TestCase):
    """
    When an admin opens a client's vault, any credential still flagged
    encrypted_with_server_key gets re-encrypted under the PIN key.
    """

    def setUp(self):
        # Staff user that the admin_required decorator will accept.
        self.staff = User.objects.create_user(
            username='admin1', password='p', is_staff=True)
        self.client.login(username='admin1', password='p')

        # Set up a real vault PIN so derive_key() gives a stable PIN key.
        self.pin = '1234'
        self.salt = generate_salt()
        config = VaultConfig.get()
        config.encryption_salt = self.salt
        config.pin_hash = hash_pin(self.pin, self.salt)
        config.pin_set = True
        config.save()
        self.pin_key = derive_key(self.pin, self.salt)

        # Pre-populate the session as if the admin had just unlocked.
        session = self.client.session
        session['vault_unlocked_at'] = timezone.now().isoformat()
        session['vault_key_wrapped'] = wrap_key(self.pin_key)
        session.save()

        user = User.objects.create_user(username='cliento', password='x')
        self.client_profile = ClientProfile.objects.create(
            user=user, firm_name='Reencrypt Co')
        vault, _ = ClientVault.objects.get_or_create(
            client=self.client_profile)

        # A credential encrypted with the server provisioning key — as if
        # provision_client_droplet had just created it.
        server_key = derive_server_key()
        self.cred = VaultCredential.objects.create(
            vault=vault, label='DigitalOcean — reencrypt-co-prod',
            category='server', is_ssh_credential=True,
            ssh_auth_type='private_key', ssh_port=22,
            ssh_host_encrypted=encrypt_value('10.0.0.7', server_key),
            ssh_username_encrypted=encrypt_value('root', server_key),
            ssh_private_key_encrypted=encrypt_value(
                FAKE_PRIVATE_KEY, server_key),
            encrypted_with_server_key=True,
        )

    def test_first_open_reencrypts_and_clears_flag(self):
        url = reverse('vault:client_vault', args=[self.client_profile.id])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        # The info banner is shown on the first open.
        self.assertContains(resp, 'auto-provisioned credential')

        self.cred.refresh_from_db()
        self.assertFalse(self.cred.encrypted_with_server_key)
        # Each field now decrypts under the PIN key (not the server key).
        self.assertEqual(
            decrypt_value(self.cred.ssh_host_encrypted, self.pin_key),
            '10.0.0.7')
        self.assertIn(
            'BEGIN OPENSSH PRIVATE KEY',
            decrypt_value(
                self.cred.ssh_private_key_encrypted, self.pin_key))

    def test_second_open_is_idempotent_no_banner(self):
        url = reverse('vault:client_vault', args=[self.client_profile.id])
        self.client.get(url)  # first open — re-encrypts.
        resp = self.client.get(url)  # second open — already PIN-encrypted.
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'auto-provisioned credential')
