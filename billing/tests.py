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


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3.1 — Stripe webhook + dunning chain coverage
# ─────────────────────────────────────────────────────────────────────────────

import json
from decimal import Decimal
from unittest.mock import ANY


def _new_client(firm='Test Co', **kw):
    """Make a fresh ClientProfile with a unique user."""
    import time
    suffix = str(int(time.monotonic() * 1_000_000))[-9:]
    u = User.objects.create_user(
        username=f'wh{suffix}', password='x',
        email=f'wh{suffix}@example.com',
    )
    return ClientProfile.objects.create(user=u, firm_name=firm, **kw)


def _make_onboarding_invoice(client, total_amount=Decimal('2500')):
    from clients.models import OnboardingInvoice
    return OnboardingInvoice.objects.create(
        client=client,
        line_items=[{'description': 'Build', 'amount': str(total_amount)}],
        total_amount=total_amount,
        status='sent',
    )


def _make_mini_invoice(client, amount=Decimal('250'), status='sent',
                      stripe_invoice_id='in_test_mini_1'):
    from billing.models import MiniInvoice
    return MiniInvoice.objects.create(
        client=client,
        description='Out-of-scope work',
        amount=amount,
        hours=2,
        status=status,
        stripe_invoice_id=stripe_invoice_id,
    )


@override_settings(STRIPE_WEBHOOK_SECRET='', DEBUG=True)
class WebhookVerifyTests(TestCase):
    """_verify_event — three branches the signature path takes."""

    @patch('billing.webhooks.stripe.Webhook.construct_event')
    def test_verified_when_secret_set_and_construct_succeeds(self, m):
        """When the secret IS set, _verify_event hands the call off to
        Stripe's construct_event and returns whatever it produces."""
        m.return_value = {'type': 'ping', 'data': {'object': {}}}
        from billing import webhooks
        with override_settings(STRIPE_WEBHOOK_SECRET='whsec_test'):
            event = webhooks._verify_event(b'{}', 'sig123')
        self.assertEqual(event['type'], 'ping')

    @patch('billing.webhooks.stripe.Webhook.construct_event')
    def test_rejects_when_construct_raises(self, m):
        """Bad signature → construct_event raises SignatureVerificationError
        → _verify_event returns None."""
        import stripe
        m.side_effect = stripe.error.SignatureVerificationError(
            'bad', sig_header='nope')
        from billing import webhooks
        with override_settings(STRIPE_WEBHOOK_SECRET='whsec_test'):
            self.assertIsNone(webhooks._verify_event(b'{}', 'sig123'))

    def test_dev_bypass_when_secret_unset_and_debug(self):
        """No secret + DEBUG=True is the dev-bypass path: parse the raw
        body as JSON and trust it. Lets local Stripe-CLI forwards work."""
        from billing import webhooks
        event = webhooks._verify_event(
            b'{"type":"ping","data":{"object":{}}}', 'irrelevant')
        self.assertEqual(event['type'], 'ping')


@override_settings(
    STRIPE_WEBHOOK_SECRET='',
    DEBUG=True,  # exercises the dev-bypass parse path
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class PaymentIntentSucceededTests(TestCase):
    """payment_intent.succeeded — happy path, idempotency, and the
    Phase 0.4b amount-mismatch guard."""

    def _post(self, body):
        return self.client.post(
            reverse('billing:stripe_webhook'),
            data=json.dumps(body), content_type='application/json')

    def test_happy_path_marks_paid(self):
        c = _new_client(firm='HappyPath LLC')
        inv = _make_onboarding_invoice(c, total_amount=Decimal('2500'))
        body = {
            'type': 'payment_intent.succeeded',
            'data': {'object': {
                'id': 'pi_happy',
                'amount': 250000,
                'amount_received': 250000,
                'customer': '',
                'payment_method': '',
                'metadata': {
                    'kind': 'onboarding',
                    'invoice_id': str(inv.id),
                },
            }},
        }
        r = self._post(body)
        self.assertEqual(r.status_code, 200)
        inv.refresh_from_db()
        self.assertEqual(inv.status, 'paid')
        self.assertEqual(inv.stripe_payment_intent_id, 'pi_happy')

    def test_idempotent_on_redelivery(self):
        """A second webhook for the same paid invoice is a no-op."""
        c = _new_client(firm='Idem LLC')
        inv = _make_onboarding_invoice(c, total_amount=Decimal('2500'))
        inv.status = 'paid'
        inv.paid_at = timezone.now()
        inv.save()
        first_paid_at = inv.paid_at

        body = {
            'type': 'payment_intent.succeeded',
            'data': {'object': {
                'id': 'pi_dup',
                'amount': 250000,
                'amount_received': 250000,
                'metadata': {
                    'kind': 'onboarding',
                    'invoice_id': str(inv.id),
                },
            }},
        }
        r = self._post(body)
        self.assertEqual(r.status_code, 200)
        inv.refresh_from_db()
        # paid_at was not bumped — confirms the early-return guard.
        self.assertEqual(inv.paid_at, first_paid_at)

    def test_amount_mismatch_refuses_to_mark_paid(self):
        """Phase 0.4b — tampered amount must NOT result in paid status."""
        c = _new_client(firm='Tamper LLC')
        inv = _make_onboarding_invoice(c, total_amount=Decimal('2500'))
        body = {
            'type': 'payment_intent.succeeded',
            'data': {'object': {
                'id': 'pi_bad',
                'amount': 100,           # wrong (charged $1 not $2500)
                'amount_received': 100,
                'metadata': {
                    'kind': 'onboarding',
                    'invoice_id': str(inv.id),
                },
            }},
        }
        r = self._post(body)
        self.assertEqual(r.status_code, 200)  # webhook itself OK
        inv.refresh_from_db()
        self.assertNotEqual(inv.status, 'paid')


@override_settings(
    STRIPE_WEBHOOK_SECRET='',
    DEBUG=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class InvoicePaidTests(TestCase):
    """invoice.paid — MiniInvoice branch (1.3) + reinstatement (1.6)."""

    def _post(self, body):
        return self.client.post(
            reverse('billing:stripe_webhook'),
            data=json.dumps(body), content_type='application/json')

    def test_mini_invoice_branch_flips_to_paid(self):
        c = _new_client(firm='MiniPay LLC')
        c.stripe_customer_id = 'cus_mini'
        c.save()
        mini = _make_mini_invoice(c, amount=Decimal('250'),
                                  stripe_invoice_id='in_mini_1')
        body = {
            'type': 'invoice.paid',
            'data': {'object': {
                'id': 'in_mini_1',
                'customer': 'cus_mini',
                'subscription': None,
                'metadata': {
                    'kind': 'mini_invoice',
                    'mini_invoice_id': str(mini.id),
                },
            }},
        }
        r = self._post(body)
        self.assertEqual(r.status_code, 200)
        mini.refresh_from_db()
        self.assertEqual(mini.status, 'paid')

    def test_mini_invoice_idempotent_redelivery(self):
        c = _new_client(firm='MiniDup LLC')
        c.stripe_customer_id = 'cus_minidup'
        c.save()
        mini = _make_mini_invoice(c, status='paid',
                                  stripe_invoice_id='in_mini_dup')
        body = {
            'type': 'invoice.paid',
            'data': {'object': {
                'id': 'in_mini_dup',
                'customer': 'cus_minidup',
                'subscription': None,
                'metadata': {'kind': 'mini_invoice'},
            }},
        }
        r = self._post(body)
        self.assertEqual(r.status_code, 200)
        mini.refresh_from_db()
        self.assertEqual(mini.status, 'paid')  # unchanged

    def test_reinstatement_first_offense_no_fee(self):
        """First offense: payment_failure_started_at clears, NO fee charged."""
        c = _new_client(firm='First LLC')
        c.stripe_customer_id = 'cus_first'
        c.payment_failure_started_at = timezone.now()
        c.payment_failure_offenses = 0
        c.save()

        body = {
            'type': 'invoice.paid',
            'data': {'object': {
                'id': 'in_rein_1',
                'customer': 'cus_first',
                'subscription': None,
                'metadata': {},
            }},
        }
        # Patch the imports inside _handle_reinstatement (they're imported
        # locally inside the function so module-level patches don't apply).
        with patch('billing.do_helpers.restore_client_site',
                   return_value=True), \
             patch('billing.stripe_helpers.charge_reinstatement_fee',
                   return_value=MagicMock(id='pi_fee')) as mc:
            r = self._post(body)
        self.assertEqual(r.status_code, 200)
        c.refresh_from_db()
        self.assertIsNone(c.payment_failure_started_at)
        self.assertEqual(c.payment_failure_offenses, 1)
        # Fee only charged on offense >= 2
        mc.assert_not_called()

    def test_reinstatement_second_offense_charges_fee(self):
        c = _new_client(firm='Second LLC')
        c.stripe_customer_id = 'cus_second'
        c.payment_failure_started_at = timezone.now()
        c.payment_failure_offenses = 1  # this one becomes the 2nd
        c.save()

        body = {
            'type': 'invoice.paid',
            'data': {'object': {
                'id': 'in_rein_2',
                'customer': 'cus_second',
                'subscription': None,
                'metadata': {},
            }},
        }
        with patch('billing.do_helpers.restore_client_site',
                   return_value=True), \
             patch('billing.stripe_helpers.charge_reinstatement_fee',
                   return_value=MagicMock(id='pi_fee_2')) as mock_charge:
            r = self._post(body)
        self.assertEqual(r.status_code, 200)
        mock_charge.assert_called_once()
        c.refresh_from_db()
        self.assertIsNone(c.payment_failure_started_at)
        self.assertEqual(c.payment_failure_offenses, 2)

    def test_reinstatement_fee_failure_blocks_restore(self):
        """Fee charge returns None → DO NOT restore + DO NOT clear guard."""
        c = _new_client(firm='Fail LLC')
        c.stripe_customer_id = 'cus_fail'
        c.payment_failure_started_at = timezone.now()
        c.payment_failure_offenses = 1
        c.save()

        body = {
            'type': 'invoice.paid',
            'data': {'object': {
                'id': 'in_rein_fail',
                'customer': 'cus_fail',
                'subscription': None,
                'metadata': {},
            }},
        }
        with patch('billing.do_helpers.restore_client_site',
                   return_value=True) as mock_restore, \
             patch('billing.stripe_helpers.charge_reinstatement_fee',
                   return_value=None):
            r = self._post(body)
        self.assertEqual(r.status_code, 200)
        c.refresh_from_db()
        # Guard NOT cleared — chain keeps escalating
        self.assertIsNotNone(c.payment_failure_started_at)
        # Offense increment IS saved (no free 2nd offense later)
        self.assertEqual(c.payment_failure_offenses, 2)
        mock_restore.assert_not_called()


@override_settings(
    STRIPE_WEBHOOK_SECRET='',
    DEBUG=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class InvoicePaymentFailedTests(TestCase):
    """invoice.payment_failed — Day-3 email + 4-task escalation chain."""

    def _post(self, body):
        return self.client.post(
            reverse('billing:stripe_webhook'),
            data=json.dumps(body), content_type='application/json')

    @patch('billing.tasks.set_site_maintenance_mode_task.apply_async')
    @patch('billing.tasks.set_site_offline_task.apply_async')
    @patch('billing.tasks.destroy_client_droplet_task.apply_async')
    @patch('billing.tasks.delete_client_snapshot_task.apply_async')
    @patch('billing.tasks.send_payment_failed_email_task.apply_async')
    def test_schedules_full_chain_and_stamps_window(
            self, mock_email_task, mock_delete, mock_destroy, mock_offline,
            mock_maint):
        from django.core import mail
        c = _new_client(firm='Failed LLC')
        c.stripe_customer_id = 'cus_failed'
        c.save()

        body = {
            'type': 'invoice.payment_failed',
            'data': {'object': {
                'id': 'in_failed_1',
                'customer': 'cus_failed',
                'subscription': '',
            }},
        }
        r = self._post(body)
        self.assertEqual(r.status_code, 200)

        c.refresh_from_db()
        self.assertIsNotNone(c.payment_failure_started_at)

        # All 4 escalation tasks scheduled with correct countdowns
        mock_maint.assert_called_once()
        self.assertEqual(mock_maint.call_args.kwargs.get('countdown'),
                         14 * 24 * 60 * 60)
        mock_offline.assert_called_once()
        self.assertEqual(mock_offline.call_args.kwargs.get('countdown'),
                         21 * 24 * 60 * 60)
        mock_destroy.assert_called_once()
        self.assertEqual(mock_destroy.call_args.kwargs.get('countdown'),
                         30 * 24 * 60 * 60)
        mock_delete.assert_called_once()
        self.assertEqual(mock_delete.call_args.kwargs.get('countdown'),
                         60 * 24 * 60 * 60)

    @patch('billing.tasks.set_site_maintenance_mode_task.apply_async')
    @patch('billing.tasks.set_site_offline_task.apply_async')
    @patch('billing.tasks.destroy_client_droplet_task.apply_async')
    @patch('billing.tasks.delete_client_snapshot_task.apply_async')
    @patch('billing.tasks.send_payment_failed_email_task.apply_async')
    def test_window_stamped_only_on_first_failure(
            self, *mocks):
        """Repeat failures within the same window leave started_at alone."""
        c = _new_client(firm='Repeat LLC')
        c.stripe_customer_id = 'cus_repeat'
        first_failure = timezone.now() - timezone.timedelta(days=3)
        c.payment_failure_started_at = first_failure
        c.save()

        body = {
            'type': 'invoice.payment_failed',
            'data': {'object': {
                'id': 'in_repeat',
                'customer': 'cus_repeat',
                'subscription': '',
            }},
        }
        self._post(body)
        c.refresh_from_db()
        # Same timestamp (allow microsecond-level equality)
        self.assertEqual(c.payment_failure_started_at, first_failure)


class EscalationTaskGuardTests(TestCase):
    """Phase 1.2 — each escalation task must no-op when the guard is None."""

    def test_maintenance_task_noop_when_guard_none(self):
        from billing.tasks import set_site_maintenance_mode_task
        c = _new_client(firm='Guard1')
        c.payment_failure_started_at = None
        c.save()
        with patch('billing.do_helpers.set_site_maintenance_mode') as mock_h:
            set_site_maintenance_mode_task(str(c.id))
        mock_h.assert_not_called()

    def test_offline_task_noop_when_guard_none(self):
        from billing.tasks import set_site_offline_task
        c = _new_client(firm='Guard2')
        c.save()
        with patch('billing.do_helpers.set_site_offline') as mock_h:
            set_site_offline_task(str(c.id))
        mock_h.assert_not_called()

    def test_destroy_task_noop_when_guard_none(self):
        from billing.tasks import destroy_client_droplet_task
        c = _new_client(firm='Guard3')
        c.save()
        with patch('billing.do_helpers.destroy_client_droplet') as mock_h:
            destroy_client_droplet_task(str(c.id))
        mock_h.assert_not_called()

    def test_delete_snapshot_task_noop_when_guard_none(self):
        from billing.tasks import delete_client_snapshot_task
        c = _new_client(firm='Guard4')
        c.save()
        with patch('billing.do_helpers.delete_client_snapshot') as mock_h:
            delete_client_snapshot_task(str(c.id))
        mock_h.assert_not_called()

    def test_destroy_task_fires_when_guard_set(self):
        from billing.tasks import destroy_client_droplet_task
        c = _new_client(firm='GuardActive')
        c.payment_failure_started_at = timezone.now()
        c.do_droplet_id = '12345'
        c.save()
        with patch('billing.do_helpers.destroy_client_droplet') as mock_h:
            destroy_client_droplet_task(str(c.id))
        mock_h.assert_called_once_with(c)


class DestroyDropletSnapshotsFirstTests(TestCase):
    """Phase 1.2a — destroy_client_droplet MUST snapshot before DELETE."""

    @patch('billing.do_helpers.requests.delete')
    @patch('billing.do_helpers.take_retention_snapshot')
    def test_snapshot_taken_before_destroy(
            self, mock_snap, mock_delete):
        """Happy path: snapshot returns an id, DELETE fires."""
        from billing.do_helpers import destroy_client_droplet
        c = _new_client(firm='SnapFirst')
        c.do_droplet_id = '99'
        c.save()
        mock_snap.return_value = 'snap_abc'

        def _set_snap(*args, **kwargs):
            c.do_snapshot_id = 'snap_abc'
            c.save()
            return 'snap_abc'

        mock_snap.side_effect = _set_snap
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        mock_delete.return_value = resp

        destroy_client_droplet(c)

        mock_snap.assert_called_once_with(c)
        mock_delete.assert_called_once()
        c.refresh_from_db()
        self.assertEqual(c.site_status, 'destroyed')
        self.assertEqual(c.do_droplet_id, '')

    @patch('billing.do_helpers.requests.delete')
    @patch('billing.do_helpers.take_retention_snapshot')
    def test_destroy_refused_if_snapshot_fails(
            self, mock_snap, mock_delete):
        """If snapshot returns '', the destroy MUST NOT fire — data loss
        on billing failure is the worst possible outcome."""
        from billing.do_helpers import destroy_client_droplet
        c = _new_client(firm='SnapFail')
        c.do_droplet_id = '99'
        c.save()
        mock_snap.return_value = ''  # snapshot failed

        destroy_client_droplet(c)

        mock_delete.assert_not_called()
        c.refresh_from_db()
        self.assertEqual(c.do_droplet_id, '99')  # still set
        self.assertNotEqual(c.site_status, 'destroyed')


class SendMiniInvoiceTests(TestCase):
    """Phase 1.3 — send_mini_invoice refuses zero, otherwise creates Stripe
    invoice + flips status to 'sent'."""

    def test_refuses_zero_amount(self):
        from billing.stripe_helpers import send_mini_invoice
        c = _new_client(firm='ZeroMini')
        mini = _make_mini_invoice(c, amount=Decimal('0'), status='pending',
                                  stripe_invoice_id='')
        with self.assertRaises(ValueError):
            send_mini_invoice(mini)
        mini.refresh_from_db()
        self.assertEqual(mini.status, 'pending')

    @patch('billing.stripe_helpers.stripe')
    @patch('billing.stripe_helpers._init')
    @patch('billing.stripe_helpers.create_or_get_customer')
    def test_happy_path_creates_invoice_and_flips_sent(
            self, mock_customer, mock_init, mock_stripe):
        from billing.stripe_helpers import send_mini_invoice
        c = _new_client(firm='HappyMini')
        c.stripe_customer_id = 'cus_mini_happy'
        c.save()
        mini = _make_mini_invoice(c, amount=Decimal('200'),
                                  status='pending', stripe_invoice_id='')
        mock_customer.return_value = MagicMock(id='cus_mini_happy')
        mock_stripe.InvoiceItem.create.return_value = MagicMock()
        invoice_obj = MagicMock(id='in_sent_mini')
        mock_stripe.Invoice.create.return_value = invoice_obj
        mock_stripe.Invoice.finalize_invoice.return_value = invoice_obj
        mock_stripe.Invoice.send_invoice.return_value = invoice_obj

        send_mini_invoice(mini)

        mini.refresh_from_db()
        self.assertEqual(mini.status, 'sent')
        self.assertEqual(mini.stripe_invoice_id, 'in_sent_mini')
