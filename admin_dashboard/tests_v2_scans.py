"""
v2 vulnerability-scan views — completing the port from v1
(admin_dashboard/views_scans.py, which stays untouched) plus the two
parity bugs website_run_scan had before this fix: it never set
target_ip and never captured celery_task_id, so a v2-created scan
couldn't be port-scanned by IP or cancelled from scan_detail.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from clients.account_models import Account, Website
from reporting.models import VulnerabilityFinding, VulnerabilityScan

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class V2ScansTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='v2scanstaff', email='v2scanstaff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)

        u = User.objects.create_user(
            username='scanclient', email='scanclient@example.com',
            password=None, is_active=True)
        cls.account = Account.objects.create(
            user=u, name='Scan Client Co', contact_name='Pat Client')
        cls.website = Website.objects.create(
            account=cls.account, name='Scan Client Site',
            build_platform='custom', status='active',
            do_droplet_ip='198.51.100.7',
            url='https://scanclient.example.com')

    def setUp(self):
        self.client.force_login(self.staff)

    # ── website_run_scan parity fix ─────────────────────────────────────

    @patch('reporting.tasks.run_vulnerability_scan_task.delay')
    def test_run_scan_captures_target_ip_and_celery_task_id(self, mock_delay):
        mock_delay.return_value.id = 'fake-task-id-123'

        r = self.client.post(
            reverse('admin_dashboard:v2_website_run_scan',
                    args=[self.website.id]),
            {'scan_type': 'quick'})
        self.assertEqual(r.status_code, 302)

        scan = VulnerabilityScan.objects.get(website_new=self.website)
        self.assertEqual(scan.target_ip, '198.51.100.7')
        self.assertEqual(scan.target_url, 'https://scanclient.example.com')
        self.assertEqual(scan.celery_task_id, 'fake-task-id-123')
        mock_delay.assert_called_once_with(str(scan.id))

    @patch('reporting.tasks.run_vulnerability_scan_task.delay',
           side_effect=RuntimeError('broker unreachable'))
    def test_run_scan_survives_broker_failure(self, mock_delay):
        r = self.client.post(
            reverse('admin_dashboard:v2_website_run_scan',
                    args=[self.website.id]),
            {'scan_type': 'quick'}, follow=True)
        self.assertEqual(r.status_code, 200)
        scan = VulnerabilityScan.objects.get(website_new=self.website)
        self.assertEqual(scan.target_ip, '198.51.100.7')
        self.assertEqual(scan.celery_task_id, '')

    # ── scan_detail ──────────────────────────────────────────────────────

    def test_scan_detail_renders_and_marks_reviewed(self):
        scan = VulnerabilityScan.objects.create(
            website_new=self.website, scan_type='full', status='complete',
            critical_count=1, been_reviewed=False)
        VulnerabilityFinding.objects.create(
            scan=scan, severity='critical', tool='nmap',
            title='Open Redis port with no auth')

        r = self.client.get(
            reverse('admin_dashboard:v2_scan_detail', args=[scan.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Open Redis port with no auth')

        scan.refresh_from_db()
        self.assertTrue(scan.been_reviewed)
        self.assertIsNotNone(scan.reviewed_at)

    # ── scan_cancel ──────────────────────────────────────────────────────

    def test_cancel_marks_cancelled_and_revokes_task(self):
        scan = VulnerabilityScan.objects.create(
            website_new=self.website, scan_type='full', status='running',
            celery_task_id='task-to-revoke')

        with patch('AspiredWebsitesRevamped.celery.app.control.revoke') as mock_revoke:
            r = self.client.post(
                reverse('admin_dashboard:v2_scan_cancel', args=[scan.id]),
                follow=True)
        self.assertEqual(r.status_code, 200)
        mock_revoke.assert_called_once_with(
            'task-to-revoke', terminate=True, signal='SIGTERM')

        scan.refresh_from_db()
        self.assertEqual(scan.status, 'cancelled')
        self.assertIsNotNone(scan.completed_at)

    def test_cancel_on_already_complete_scan_is_a_noop(self):
        scan = VulnerabilityScan.objects.create(
            website_new=self.website, scan_type='full', status='complete')
        r = self.client.post(
            reverse('admin_dashboard:v2_scan_cancel', args=[scan.id]),
            follow=True)
        self.assertEqual(r.status_code, 200)
        scan.refresh_from_db()
        self.assertEqual(scan.status, 'complete')

    # ── PDF generate / download ──────────────────────────────────────────

    def test_generate_pdf_streams_attachment(self):
        scan = VulnerabilityScan.objects.create(
            website_new=self.website, scan_type='full', status='complete')

        with patch('reporting.scan_runner.generate_scan_pdf') as mock_gen:
            def _fake_generate(scan_id):
                s = VulnerabilityScan.objects.get(id=scan_id)
                s.pdf_path = f'scans/{self.website.id}/fake.html'
                s.save(update_fields=['pdf_path'])
                return s.pdf_path
            mock_gen.side_effect = _fake_generate

            import os
            from django.conf import settings
            abs_dir = os.path.join(
                settings.MEDIA_ROOT, 'scans', str(self.website.id))
            os.makedirs(abs_dir, exist_ok=True)
            with open(os.path.join(abs_dir, 'fake.html'), 'w') as fh:
                fh.write('<html>report</html>')

            r = self.client.get(
                reverse('admin_dashboard:v2_scan_generate_pdf',
                        args=[scan.id]))
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r['Content-Disposition'])

    def test_download_pdf_404s_when_not_generated(self):
        scan = VulnerabilityScan.objects.create(
            website_new=self.website, scan_type='full', status='complete')
        r = self.client.get(
            reverse('admin_dashboard:v2_scan_download_pdf', args=[scan.id]))
        self.assertEqual(r.status_code, 404)

    # ── send to client ───────────────────────────────────────────────────

    def test_send_report_emails_client_and_flips_sent_flag(self):
        scan = VulnerabilityScan.objects.create(
            website_new=self.website, scan_type='full', status='complete',
            critical_count=0, high_count=0)

        import os
        from django.conf import settings
        abs_dir = os.path.join(
            settings.MEDIA_ROOT, 'scans', str(self.website.id))
        os.makedirs(abs_dir, exist_ok=True)
        rel_path = os.path.join(
            'scans', str(self.website.id), 'existing.html')
        with open(os.path.join(settings.MEDIA_ROOT, rel_path), 'w') as fh:
            fh.write('<html>report</html>')
        scan.pdf_path = rel_path
        scan.save(update_fields=['pdf_path'])

        r = self.client.post(
            reverse('admin_dashboard:v2_scan_send_report', args=[scan.id]),
            follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('scanclient@example.com', mail.outbox[0].to)

        scan.refresh_from_db()
        self.assertTrue(scan.sent_to_client)
        self.assertIsNotNone(scan.sent_at)

    def test_send_report_blocked_with_no_account_email(self):
        u2 = User.objects.create_user(
            username='noemailowner', email='', password=None,
            is_active=True)
        account2 = Account.objects.create(user=u2, name='No Email Co')
        website2 = Website.objects.create(
            account=account2, name='No Email Site', build_platform='custom')
        scan = VulnerabilityScan.objects.create(
            website_new=website2, scan_type='full', status='complete')

        r = self.client.post(
            reverse('admin_dashboard:v2_scan_send_report', args=[scan.id]),
            follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    # ── finding status update ────────────────────────────────────────────

    def test_finding_status_update_accepts_risk_with_note(self):
        scan = VulnerabilityScan.objects.create(
            website_new=self.website, scan_type='full', status='complete')
        finding = VulnerabilityFinding.objects.create(
            scan=scan, severity='medium', tool='nikto', title='Old TLS cipher')

        r = self.client.post(
            reverse('admin_dashboard:v2_finding_status_update',
                    args=[finding.id]),
            {'status': 'accepted_risk', 'acceptance_note': 'Legacy client tool'},
            follow=True)
        self.assertEqual(r.status_code, 200)

        finding.refresh_from_db()
        self.assertEqual(finding.status, 'accepted_risk')
        self.assertEqual(finding.acceptance_note, 'Legacy client tool')
        self.assertTrue(finding.accepted_by)

    def test_finding_status_update_rejects_invalid_status(self):
        scan = VulnerabilityScan.objects.create(
            website_new=self.website, scan_type='full', status='complete')
        finding = VulnerabilityFinding.objects.create(
            scan=scan, severity='low', tool='manual', title='Whatever')

        r = self.client.post(
            reverse('admin_dashboard:v2_finding_status_update',
                    args=[finding.id]),
            {'status': 'not-a-real-status'})
        self.assertEqual(r.status_code, 400)

    # ── fleet-wide list ──────────────────────────────────────────────────

    def test_scans_list_renders_and_filters_by_website(self):
        VulnerabilityScan.objects.create(
            website_new=self.website, scan_type='full', status='complete')
        r = self.client.get(reverse('admin_dashboard:v2_scans_list'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Scan Client Site')

        r2 = self.client.get(
            reverse('admin_dashboard:v2_scans_list'),
            {'website': str(self.website.id)})
        self.assertEqual(r2.status_code, 200)
