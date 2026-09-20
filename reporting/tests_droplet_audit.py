"""
SSH-based droplet health audit — reporting.droplet_audit,
reporting.tasks.check_droplet_health_schedule, and the v2 admin views.

Custom-build sites only. WordPress sites (build_platform='wordpress')
never get a check — no Aspired-managed Droplet to SSH into.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account, Website
from reporting.droplet_audit import (
    _check_disk, _check_os_updates, _check_pip_audit, _check_services,
    run_droplet_health_check,
)
from reporting.models import DropletHealthCheck
from vault.crypto import derive_server_key, encrypt_value
from vault.models import ClientVault, VaultCredential

User = get_user_model()
_seq = 0


def _account_website(build_platform='custom', droplet_ip='192.0.2.20',
                      droplet_created_at=None):
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'dha{_seq}', email=f'dha{_seq}@example.com', password='x')
    account = Account.objects.create(user=u, name=f'DHA Co {_seq}')
    website = Website.objects.create(
        account=account, name=f'DHA Site {_seq}', build_platform=build_platform,
        status='active', do_droplet_ip=droplet_ip,
        do_droplet_id=f'droplet-{_seq}',
        do_droplet_created_at=droplet_created_at)
    return account, website


def _give_automation_credential(account, website):
    vault, _ = ClientVault.objects.get_or_create(account_new=account)
    server_key = derive_server_key()
    return VaultCredential.objects.create(
        vault=vault, website_new=website, label='Prod',
        category='server', is_ssh_credential=True,
        ssh_auth_type='private_key',
        automation_ssh_private_key_encrypted=encrypt_value(
            'fake-key', server_key),
        automation_access_enabled=True,
    )


class CheckParsingTests(TestCase):
    """Unit tests for the individual _check_* parsers against realistic
    command output — including the exact `df -h /` / `apt list
    --upgradable` output captured from the staging server."""

    def test_check_disk_parses_percent(self):
        ssh = MagicMock()
        with patch('vault.ssh_ops.run_remote') as mock_run:
            mock_run.return_value = (
                0,
                'Filesystem      Size  Used Avail Use% Mounted on\n'
                '/dev/vda1        48G  6.4G   42G  14% /\n',
                '')
            raw, percent = _check_disk(ssh)
        self.assertEqual(percent, 14)
        self.assertEqual(raw['exit_code'], 0)

    def test_check_os_updates_subtracts_header_line(self):
        ssh = MagicMock()
        listing = 'Listing...\n' + '\n'.join(
            f'pkg{i}/now 1.0 amd64 [upgradable from: 0.9]'
            for i in range(39))
        with patch('vault.ssh_ops.run_remote') as mock_run:
            mock_run.return_value = (0, listing, '')
            raw, count = _check_os_updates(ssh)
        self.assertEqual(count, 39)

    def test_check_os_updates_empty_output_is_zero(self):
        ssh = MagicMock()
        with patch('vault.ssh_ops.run_remote') as mock_run:
            mock_run.return_value = (0, '', '')
            raw, count = _check_os_updates(ssh)
        self.assertEqual(count, 0)

    def test_check_services_flags_down_systemd_and_supervisor(self):
        ssh = MagicMock()

        def fake_run(ssh_arg, cmd, check=False, **kw):
            if 'is-active nginx' in cmd:
                return 0, 'active\n', ''
            if 'is-active postgresql' in cmd:
                return 3, 'inactive\n', ''
            if 'is-active redis-server' in cmd:
                return 0, 'active\n', ''
            if 'supervisorctl status' in cmd:
                return 0, (
                    'aspiredwebsites   RUNNING   pid 1, uptime 0:01:00\n'
                    'aspiredwebsites-celery   STOPPED   Sep 19 07:18 PM\n'
                ), ''
            return -1, '', ''

        with patch('vault.ssh_ops.run_remote', side_effect=fake_run):
            raw, down = _check_services(ssh)
        self.assertIn('postgresql', down)
        self.assertIn('aspiredwebsites-celery', down)
        self.assertNotIn('nginx', down)
        self.assertNotIn('aspiredwebsites', down)

    def test_check_pip_audit_skips_when_no_venv_found(self):
        ssh = MagicMock()
        with patch('vault.ssh_ops.run_remote') as mock_run:
            mock_run.return_value = (0, '', '')  # find returns nothing
            raw, count = _check_pip_audit(ssh)
        self.assertTrue(raw['skipped'])
        self.assertIsNone(count)


class RunDropletHealthCheckTests(TestCase):

    def test_wordpress_site_fails_with_clear_message(self):
        account, website = _account_website(build_platform='wordpress')
        check = DropletHealthCheck.objects.create(website_new=website)
        result = run_droplet_health_check(str(check.id))
        self.assertEqual(result.status, 'failed')
        self.assertIn('wordpress', result.error_message.lower())

    def test_no_automation_credential_marks_ssh_unavailable(self):
        account, website = _account_website()
        check = DropletHealthCheck.objects.create(website_new=website)
        result = run_droplet_health_check(str(check.id))
        self.assertEqual(result.status, 'ssh_unavailable')
        self.assertIsNotNone(result.completed_at)

    def test_successful_run_populates_all_fields(self):
        account, website = _account_website()
        _give_automation_credential(account, website)
        check = DropletHealthCheck.objects.create(website_new=website)

        def fake_run(ssh_arg, cmd, check=False, **kw):
            if cmd.startswith('df -h'):
                return 0, ('Filesystem Size Used Avail Use% Mounted\n'
                           '/dev/vda1 48G 6G 42G 20% /\n'), ''
            if cmd.startswith('free -m'):
                return 0, 'Mem: 1967 800 200\n', ''
            if 'is-active' in cmd and 'fail2ban' not in cmd:
                return 0, 'active\n', ''
            if 'is-active fail2ban' in cmd:
                return 0, 'active\n', ''
            if 'supervisorctl status' in cmd:
                return 0, 'app RUNNING pid 1, uptime 0:01:00\n', ''
            if cmd.startswith('apt list'):
                return 0, 'Listing...\npkg1 1.0\n', ''
            if 'ufw status' in cmd:
                return 0, 'Status: active\n', ''
            if 'find /var/www' in cmd:
                return 0, '', ''  # no venvs — keeps this test simple
            return -1, '', ''

        with patch('paramiko.SSHClient') as mock_ssh_cls, \
                patch('vault.ssh_ops._load_private_key') as mock_load_key, \
                patch('vault.ssh_ops.run_remote', side_effect=fake_run):
            mock_ssh_cls.return_value = MagicMock()
            mock_load_key.return_value = MagicMock()
            result = run_droplet_health_check(str(check.id))

        self.assertEqual(result.status, 'complete')
        self.assertEqual(result.disk_usage_percent, 20)
        self.assertEqual(result.pending_os_updates_count, 1)
        self.assertEqual(result.services_down, [])
        self.assertTrue(result.raw_pip_audit.get('skipped'))
        self.assertIsNotNone(result.completed_at)


class ScheduleSweepTests(TestCase):

    def test_wordpress_site_never_queued(self):
        from reporting.tasks import check_droplet_health_schedule
        _account_website(build_platform='wordpress')
        with patch(
                'reporting.tasks.run_droplet_health_check_task.delay'
        ) as mock_delay:
            check_droplet_health_schedule()
        mock_delay.assert_not_called()
        self.assertEqual(DropletHealthCheck.objects.count(), 0)

    def test_first_check_due_30_days_after_droplet_creation(self):
        from reporting.tasks import check_droplet_health_schedule
        account, website = _account_website(
            droplet_created_at=timezone.now() - timedelta(days=31))
        with patch(
                'reporting.tasks.run_droplet_health_check_task.delay'
        ) as mock_delay:
            mock_delay.return_value.id = 'task-1'
            check_droplet_health_schedule()
        self.assertEqual(
            DropletHealthCheck.objects.filter(website_new=website).count(),
            1)

    def test_not_due_yet_skipped(self):
        from reporting.tasks import check_droplet_health_schedule
        account, website = _account_website(
            droplet_created_at=timezone.now() - timedelta(days=5))
        with patch(
                'reporting.tasks.run_droplet_health_check_task.delay'
        ) as mock_delay:
            check_droplet_health_schedule()
        mock_delay.assert_not_called()

    def test_subsequent_check_due_30_days_after_last_completed(self):
        from reporting.tasks import check_droplet_health_schedule
        account, website = _account_website(
            droplet_created_at=timezone.now() - timedelta(days=400))
        DropletHealthCheck.objects.create(
            website_new=website, status='complete',
            completed_at=timezone.now() - timedelta(days=31))
        with patch(
                'reporting.tasks.run_droplet_health_check_task.delay'
        ) as mock_delay:
            mock_delay.return_value.id = 'task-2'
            check_droplet_health_schedule()
        self.assertEqual(
            DropletHealthCheck.objects.filter(website_new=website).count(),
            2)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class V2DropletAuditViewTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='v2dropletstaff', email='v2dropletstaff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)

    def setUp(self):
        self.client.force_login(self.staff)

    @patch('reporting.tasks.run_droplet_health_check_task.delay')
    def test_run_droplet_audit_queues_check(self, mock_delay):
        mock_delay.return_value.id = 'task-abc'
        account, website = _account_website()

        r = self.client.post(
            reverse('admin_dashboard:v2_website_run_droplet_audit',
                    args=[website.id]))
        self.assertEqual(r.status_code, 302)

        check = DropletHealthCheck.objects.get(website_new=website)
        self.assertEqual(check.celery_task_id, 'task-abc')

    def test_run_droplet_audit_blocked_for_wordpress(self):
        account, website = _account_website(build_platform='wordpress')
        r = self.client.post(
            reverse('admin_dashboard:v2_website_run_droplet_audit',
                    args=[website.id]),
            follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(DropletHealthCheck.objects.count(), 0)

    def test_run_droplet_audit_blocked_without_droplet(self):
        account, website = _account_website()
        website.do_droplet_id = ''
        website.save(update_fields=['do_droplet_id'])
        r = self.client.post(
            reverse('admin_dashboard:v2_website_run_droplet_audit',
                    args=[website.id]),
            follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(DropletHealthCheck.objects.count(), 0)

    def test_droplet_check_detail_renders(self):
        account, website = _account_website()
        check = DropletHealthCheck.objects.create(
            website_new=website, status='complete',
            disk_usage_percent=42,
            raw_disk={'stdout': 'df output here'})
        r = self.client.get(
            reverse('admin_dashboard:v2_droplet_check_detail',
                    args=[check.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'df output here')
