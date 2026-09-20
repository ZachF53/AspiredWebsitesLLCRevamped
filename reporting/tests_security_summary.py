"""
Monthly 1-page security summary — reporting.security_summary,
reporting.tasks.send_security_summaries, and the v2/portal download
views. Standalone artifact — separate from the multi-page performance
MonthlyReport, and separate from the per-scan security_report email.
"""

from datetime import date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account, Website
from reporting.models import (
    DropletHealthCheck, SecuritySummaryReport, VulnerabilityFinding,
    VulnerabilityScan,
)
from reporting.security_summary import generate_security_summary

User = get_user_model()
_seq = 0


def _account_website(build_platform='custom'):
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'sec{_seq}', email=f'sec{_seq}@example.com', password='x')
    account = Account.objects.create(
        user=u, name=f'Sec Co {_seq}', contact_name='Pat Client')
    website = Website.objects.create(
        account=account, name=f'Sec Site {_seq}', build_platform=build_platform,
        status='active', onboarding_status='intake_complete')
    return account, website


def _dt(d):
    """Timezone-aware datetime at midnight on date `d`."""
    return timezone.make_aware(datetime.combine(d, datetime.min.time()))


REPORT_MONTH = date(2026, 9, 1)


class GenerateSecuritySummaryTests(TestCase):

    def test_one_row_per_site_per_month_idempotent(self):
        account, website = _account_website()
        r1 = generate_security_summary(website, REPORT_MONTH)
        r2 = generate_security_summary(website, REPORT_MONTH)
        self.assertEqual(r1.id, r2.id)
        self.assertEqual(
            SecuritySummaryReport.objects.filter(website_new=website).count(),
            1)

    def test_normalises_report_month_to_first_of_month(self):
        account, website = _account_website()
        report = generate_security_summary(website, date(2026, 9, 15))
        self.assertEqual(report.report_month, date(2026, 9, 1))

    def test_picks_latest_completed_scan_and_droplet_check(self):
        account, website = _account_website()
        old_scan = VulnerabilityScan.objects.create(
            website_new=website, status='complete',
            completed_at=_dt(date(2026, 8, 1)))
        new_scan = VulnerabilityScan.objects.create(
            website_new=website, status='complete',
            completed_at=_dt(date(2026, 9, 1)))
        # An incomplete scan must never be picked.
        VulnerabilityScan.objects.create(website_new=website, status='running')

        old_check = DropletHealthCheck.objects.create(
            website_new=website, status='complete',
            completed_at=_dt(date(2026, 8, 1)))
        new_check = DropletHealthCheck.objects.create(
            website_new=website, status='complete',
            completed_at=_dt(date(2026, 9, 1)))

        report = generate_security_summary(website, REPORT_MONTH)
        self.assertEqual(report.latest_scan_id, new_scan.id)
        self.assertEqual(report.latest_droplet_check_id, new_check.id)

    def test_open_finding_counts_reflect_current_status_not_scan_snapshot(self):
        account, website = _account_website()
        scan = VulnerabilityScan.objects.create(
            website_new=website, status='complete',
            critical_count=2,  # stale snapshot from scan time
            completed_at=_dt(REPORT_MONTH))
        VulnerabilityFinding.objects.create(
            scan=scan, severity='critical', tool='nmap',
            title='Finding A', status='open')
        VulnerabilityFinding.objects.create(
            scan=scan, severity='critical', tool='nmap',
            title='Finding B', status='resolved')

        report = generate_security_summary(website, REPORT_MONTH)
        # Only the still-open one counts, not scan.critical_count's 2.
        self.assertEqual(report.open_critical_count, 1)

    def test_wordpress_site_has_no_droplet_section(self):
        account, website = _account_website(build_platform='wordpress')
        VulnerabilityScan.objects.create(
            website_new=website, status='complete', completed_at=_dt(REPORT_MONTH))

        report = generate_security_summary(website, REPORT_MONTH)
        self.assertIsNone(report.latest_droplet_check)
        self.assertIsNone(report.disk_usage_percent)

    def test_overall_status_red_on_open_critical(self):
        account, website = _account_website()
        scan = VulnerabilityScan.objects.create(
            website_new=website, status='complete', completed_at=_dt(REPORT_MONTH))
        VulnerabilityFinding.objects.create(
            scan=scan, severity='critical', tool='nmap',
            title='Bad', status='open')

        report = generate_security_summary(website, REPORT_MONTH)
        self.assertEqual(report.overall_status, 'red')

    def test_overall_status_red_on_services_down(self):
        account, website = _account_website()
        DropletHealthCheck.objects.create(
            website_new=website, status='complete', completed_at=_dt(REPORT_MONTH),
            services_down=['nginx'])

        report = generate_security_summary(website, REPORT_MONTH)
        self.assertEqual(report.overall_status, 'red')

    def test_overall_status_green_when_clean(self):
        account, website = _account_website()
        VulnerabilityScan.objects.create(
            website_new=website, status='complete', completed_at=_dt(REPORT_MONTH))
        DropletHealthCheck.objects.create(
            website_new=website, status='complete', completed_at=_dt(REPORT_MONTH),
            disk_usage_percent=20, pending_os_updates_count=2,
            pip_audit_vulnerability_count=0, services_down=[])

        report = generate_security_summary(website, REPORT_MONTH)
        self.assertEqual(report.overall_status, 'green')

    def test_pdf_path_set_and_status_ready(self):
        account, website = _account_website()
        report = generate_security_summary(website, REPORT_MONTH)
        self.assertEqual(report.status, 'ready')
        self.assertNotEqual(report.pdf_path, '')


class SendSecuritySummariesTaskTests(TestCase):

    def test_sends_to_eligible_site_and_flips_status(self):
        from reporting.tasks import send_security_summaries

        account, website = _account_website()
        VulnerabilityScan.objects.create(
            website_new=website, status='complete',
            completed_at=_dt(REPORT_MONTH))

        with patch('reporting.tasks.timezone.localdate',
                   return_value=date(2026, 10, 5)):
            send_security_summaries()

        report = SecuritySummaryReport.objects.get(website_new=website)
        self.assertEqual(report.status, 'sent')
        self.assertIsNotNone(report.sent_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(account.user.email, mail.outbox[0].to)

    def test_site_with_no_scan_or_check_is_skipped(self):
        from reporting.tasks import send_security_summaries

        account, website = _account_website()
        with patch('reporting.tasks.timezone.localdate',
                   return_value=date(2026, 10, 5)):
            send_security_summaries()

        self.assertFalse(
            SecuritySummaryReport.objects.filter(website_new=website).exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_no_account_email_is_skipped_not_crashed(self):
        from reporting.tasks import send_security_summaries

        u = User.objects.create_user(
            username='noemailsec', email='', password=None, is_active=True)
        account = Account.objects.create(user=u, name='No Email Sec Co')
        website = Website.objects.create(
            account=account, name='No Email Sec Site', build_platform='custom')
        VulnerabilityScan.objects.create(
            website_new=website, status='complete', completed_at=_dt(REPORT_MONTH))

        with patch('reporting.tasks.timezone.localdate',
                   return_value=date(2026, 10, 5)):
            send_security_summaries()

        self.assertEqual(len(mail.outbox), 0)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class V2SecuritySummaryDownloadTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='v2secstaff', email='v2secstaff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)

    def setUp(self):
        self.client.force_login(self.staff)

    def test_download_serves_generated_pdf(self):
        account, website = _account_website()
        report = generate_security_summary(website, REPORT_MONTH)
        r = self.client.get(
            reverse('admin_dashboard:v2_security_summary_download',
                    args=[report.id]))
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r['Content-Disposition'])

    def test_download_404s_when_not_generated(self):
        account, website = _account_website()
        report = SecuritySummaryReport.objects.create(
            website_new=website, report_month=REPORT_MONTH)
        r = self.client.get(
            reverse('admin_dashboard:v2_security_summary_download',
                    args=[report.id]))
        self.assertEqual(r.status_code, 404)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class PortalSecuritySummaryTests(TestCase):

    def test_portal_only_shows_sent_summaries(self):
        account, website = _account_website()
        account.user.is_active = True
        account.user.set_password('testpass123')
        account.user.save()

        sent = generate_security_summary(website, REPORT_MONTH)
        sent.status = 'sent'
        sent.save(update_fields=['status'])
        generating = SecuritySummaryReport.objects.create(
            website_new=website, report_month=date(2026, 8, 1),
            status='generating')

        self.client.login(username=account.user.username, password='testpass123')
        r = self.client.get(reverse('clients:portal_security'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'September 2026')
        self.assertNotContains(r, 'August 2026')

    def test_download_blocked_for_other_account(self):
        account, website = _account_website()
        other_account, other_website = _account_website()
        other_account.user.is_active = True
        other_account.user.set_password('testpass123')
        other_account.user.save()

        report = generate_security_summary(website, REPORT_MONTH)
        report.status = 'sent'
        report.save(update_fields=['status'])

        self.client.login(
            username=other_account.user.username, password='testpass123')
        r = self.client.get(
            reverse('clients:portal_summary_download', args=[report.id]))
        self.assertEqual(r.status_code, 404)

    def test_download_blocked_when_not_sent(self):
        account, website = _account_website()
        account.user.is_active = True
        account.user.set_password('testpass123')
        account.user.save()

        report = generate_security_summary(website, REPORT_MONTH)  # status='ready'

        self.client.login(username=account.user.username, password='testpass123')
        r = self.client.get(
            reverse('clients:portal_summary_download', args=[report.id]))
        self.assertEqual(r.status_code, 404)
