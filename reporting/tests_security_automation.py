"""
Monthly security report automation — recipient predicate, stale-scan
handling, file-integrity diffing, the five-section summary, per-scan
auto-send, the pre-summary sweep and the management command.
"""

import io
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from clients.account_models import Account, Website
from reporting.file_integrity import (
    build_manifest_command, diff_manifests, parse_sha256sum_output,
    run_file_integrity,
)
from reporting.models import (
    DropletHealthCheck, FileIntegrityBaseline, SecuritySummaryReport,
    VulnerabilityFinding, VulnerabilityScan,
)
from reporting.security_eligibility import (
    is_security_report_eligible, security_report_websites,
)
from reporting.security_summary import (
    NOT_HOSTED_MSG, SCAN_MISSING_MSG, generate_security_summary,
    run_security_summaries,
)

User = get_user_model()
_seq = 0

REPORT_MONTH = date(2026, 9, 1)
# Send date for REPORT_MONTH — freshness is measured from here.
SEND_TIME = timezone.make_aware(datetime(2026, 10, 1, 7, 30))

H1 = 'a' * 64
H2 = 'b' * 64
H3 = 'c' * 64


def _site(*, platform='custom', droplet_ip=None, maintenance_active=True,
          package='', hosting_sub='', account_status='active',
          site_status='active', email=True, url='https://example.com'):
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'secauto{_seq}',
        email=f'secauto{_seq}@example.com' if email else '',
        password='x')
    account = Account.objects.create(
        user=u, name=f'Auto Co {_seq}', contact_name='Pat Client',
        status=account_status)
    website = Website.objects.create(
        account=account, name=f'Auto Site {_seq}', build_platform=platform,
        status=site_status, url=url, do_droplet_ip=droplet_ip,
        maintenance_active=maintenance_active, package=package,
        stripe_hosting_subscription_id=hosting_sub)
    return website


def _dt(d, hour=12):
    return timezone.make_aware(datetime.combine(d, datetime.min.time())
                               + timedelta(hours=hour))


def _full_scan(website, completed_at, **kw):
    defaults = dict(
        status='complete', completed_at=completed_at,
        raw_ssl={'grade': 'A'},
        raw_http={
            'headers': {'present': ['Strict-Transport-Security'],
                        'missing': ['Content-Security-Policy']},
            'certificate': {'valid': True,
                            'not_after': '2026-12-01T00:00:00+00:00',
                            'days_remaining': 70, 'issuer': "Let's Encrypt"},
        },
        raw_wpscan={'findings': [], 'skipped': True,
                    'reason': 'WordPress not detected on this site'},
    )
    defaults.update(kw)
    return VulnerabilityScan.objects.create(website_new=website, **defaults)


class _TempMediaMixin:
    def setUp(self):
        super().setUp()
        self._media = tempfile.mkdtemp(prefix='secauto-media-')
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)
        super().tearDown()


# ── 1. Recipient predicate ────────────────────────────────────────────────

class EligibilityTests(TestCase):

    def test_maintenance_active_is_eligible(self):
        self.assertTrue(is_security_report_eligible(_site()))

    def test_no_plan_is_not_eligible(self):
        self.assertFalse(is_security_report_eligible(
            _site(maintenance_active=False)))

    def test_active_maintenance_plan_on_site_is_eligible(self):
        from clients.service_models import MaintenancePlan
        site = _site(maintenance_active=False)
        MaintenancePlan.objects.create(
            account=site.account, website=site, tier_slug='hvac-full-plan',
            status='active')
        self.assertTrue(is_security_report_eligible(site))

    def test_account_level_active_plan_covers_site(self):
        from clients.service_models import MaintenancePlan
        site = _site(maintenance_active=False)
        MaintenancePlan.objects.create(
            account=site.account, website=None,
            tier_slug='maintenance-growth', status='active')
        self.assertTrue(is_security_report_eligible(site))

    def test_ended_plan_is_not_eligible(self):
        from clients.service_models import MaintenancePlan
        site = _site(maintenance_active=False)
        MaintenancePlan.objects.create(
            account=site.account, website=site, tier_slug='hvac-full-plan',
            status='ended')
        self.assertFalse(is_security_report_eligible(site))

    def test_hosting_security_with_live_subscription_is_eligible(self):
        site = _site(maintenance_active=False,
                     package='hvac_hosting_security', hosting_sub='sub_123')
        self.assertTrue(is_security_report_eligible(site))

    def test_paid_package_without_live_subscription_is_not_eligible(self):
        # Cancelled: webhook cleared the sub id but left the package.
        site = _site(maintenance_active=False, package='hvac_full_plan')
        self.assertFalse(is_security_report_eligible(site))

    def test_comped_maintenance_is_eligible(self):
        site = _site(maintenance_active=False)
        site.account.comp_maintenance_package = 'maintenance_essentials'
        site.account.save()
        self.assertTrue(is_security_report_eligible(site))

    def test_inactive_account_or_site_is_not_eligible(self):
        self.assertFalse(is_security_report_eligible(
            _site(account_status='paused')))
        self.assertFalse(is_security_report_eligible(
            _site(site_status='archived')))

    def test_queryset_is_distinct(self):
        from clients.service_models import MaintenancePlan
        site = _site()
        for _ in range(2):
            MaintenancePlan.objects.create(
                account=site.account, website=site,
                tier_slug='hvac-full-plan', status='active')
        self.assertEqual(
            list(security_report_websites().filter(pk=site.pk)), [site])


# ── 2. File integrity (pure) ──────────────────────────────────────────────

class FileIntegrityPureTests(TestCase):

    def test_parse_sha256sum_output(self):
        out = (f'{H1}  /var/www/app/manage.py\n'
               f'{H2} */var/www/app/core/views.py\n'
               'garbage line\n'
               f'{"z" * 64}  /bad/hash\n')
        self.assertEqual(parse_sha256sum_output(out), {
            '/var/www/app/manage.py': H1,
            '/var/www/app/core/views.py': H2,
        })

    def test_diff_manifests(self):
        baseline = {'/a.py': H1, '/b.py': H2, '/gone.py': H3}
        current = {'/a.py': H1, '/b.py': H3, '/new.php': H2}
        self.assertEqual(diff_manifests(baseline, current), {
            'added': ['/new.php'],
            'removed': ['/gone.py'],
            'changed': ['/b.py'],
        })

    def test_diff_identical_is_empty(self):
        m = {'/a.py': H1}
        self.assertEqual(diff_manifests(m, dict(m)),
                         {'added': [], 'removed': [], 'changed': []})

    def test_custom_command_prunes_data_dirs(self):
        cmd = build_manifest_command('custom')
        self.assertIn('/var/www', cmd)
        for name in ('media', 'static', 'logs', 'venv', '__pycache__',
                     '.git'):
            self.assertIn(f'-name {name}', cmd)
        self.assertIn('-prune', cmd)
        self.assertIn('sha256sum', cmd)

    def test_wordpress_command_scopes_core_and_php(self):
        cmd = build_manifest_command('wordpress', '/var/www/html')
        self.assertIn('/var/www/html/wp-admin', cmd)
        self.assertIn('/var/www/html/wp-includes', cmd)
        self.assertIn('/var/www/html/wp-content/plugins', cmd)
        self.assertIn('/var/www/html/wp-content/themes', cmd)
        self.assertIn("-name '*.php'", cmd)


class FileIntegrityRunTests(TestCase):
    """run_file_integrity against a fake run_remote — no SSH."""

    def _run(self, website, sha_output):
        with patch('vault.ssh_ops.run_remote',
                   return_value=(0, sha_output, '')):
            return run_file_integrity(MagicMock(), website)

    def test_first_run_records_baseline_then_diffs(self):
        site = _site(droplet_ip='192.0.2.9')
        raw, count = self._run(site, f'{H1}  /var/www/a.py\n')
        self.assertEqual(raw['status'], 'baseline_recorded')
        self.assertEqual(count, 0)
        self.assertEqual(FileIntegrityBaseline.objects.filter(
            website=site).count(), 1)

        raw, count = self._run(
            site, f'{H2}  /var/www/a.py\n{H1}  /var/www/b.py\n')
        self.assertEqual(raw['status'], 'changed')
        self.assertEqual(count, 2)
        self.assertEqual(raw['changed'], ['/var/www/a.py'])
        self.assertEqual(raw['added'], ['/var/www/b.py'])

    def test_no_output_is_skipped(self):
        site = _site(droplet_ip='192.0.2.9')
        raw, count = self._run(site, '')
        self.assertEqual(raw['status'], 'skipped')
        self.assertIsNone(count)
        self.assertFalse(FileIntegrityBaseline.objects.exists())


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class AcceptBaselineViewTests(TestCase):

    def test_accept_creates_new_baseline(self):
        from django.urls import reverse
        staff = User.objects.create_user(
            username='secautostaff', email='s@example.com', password='x',
            is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        site = _site(droplet_ip='192.0.2.9')
        check = DropletHealthCheck.objects.create(
            website_new=site, status='complete',
            raw_file_integrity={'status': 'changed',
                                'manifest': {'/var/www/a.py': H2}})
        r = self.client.post(reverse(
            'admin_dashboard:v2_droplet_check_accept_baseline',
            args=[check.id]))
        self.assertEqual(r.status_code, 302)
        baseline = FileIntegrityBaseline.objects.get(website=site)
        self.assertEqual(baseline.manifest, {'/var/www/a.py': H2})
        self.assertEqual(baseline.accepted_by, 'secautostaff')

    def test_get_not_allowed(self):
        from django.urls import reverse
        staff = User.objects.create_user(
            username='secautostaff2', email='s2@example.com', password='x',
            is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        site = _site(droplet_ip='192.0.2.9')
        check = DropletHealthCheck.objects.create(website_new=site)
        r = self.client.get(reverse(
            'admin_dashboard:v2_droplet_check_accept_baseline',
            args=[check.id]))
        self.assertEqual(r.status_code, 405)


# ── 3. Summary content: five sections, stale handling ─────────────────────

class SummarySectionTests(_TempMediaMixin, TestCase):

    def _render(self, site):
        # Force the HTML fallback so the rendered text can be asserted on
        # every platform (WeasyPrint import fails -> .html sibling).
        with patch.dict(sys.modules, {'weasyprint': None}):
            report = generate_security_summary(site, REPORT_MONTH,
                                               now=SEND_TIME)
        self.assertTrue(report.pdf_path.endswith('.html'))
        with open(os.path.join(self._media, report.pdf_path),
                  encoding='utf-8') as fh:
            return report, fh.read()

    def test_hosted_site_renders_all_five_sections(self):
        from clients.models import UptimeAlert, UptimeRecord
        site = _site(droplet_ip='192.0.2.10')
        _full_scan(site, _dt(date(2026, 9, 27)))
        DropletHealthCheck.objects.create(
            website_new=site, status='complete',
            completed_at=_dt(date(2026, 9, 27)),
            pip_audit_vulnerability_count=0, raw_pip_audit={'venvs': {}},
            raw_file_integrity={'status': 'clean', 'file_count': 120})
        for i in range(9):
            r = UptimeRecord.objects.create(website_new=site, is_up=True)
            UptimeRecord.objects.filter(pk=r.pk).update(
                checked_at=_dt(date(2026, 9, 10 + i)))
        r = UptimeRecord.objects.create(website_new=site, is_up=False)
        UptimeRecord.objects.filter(pk=r.pk).update(
            checked_at=_dt(date(2026, 9, 20)))
        a = UptimeAlert.objects.create(website_new=site)
        UptimeAlert.objects.filter(pk=a.pk).update(
            alerted_at=_dt(date(2026, 9, 20)))

        report, html = self._render(site)
        for title in ('Dependency vulnerabilities', 'File integrity',
                      'SSL certificate', 'Security headers', 'Uptime'):
            self.assertIn(title, html)
        self.assertEqual(set(report.sections), {
            'dependencies', 'file_integrity', 'ssl', 'headers', 'uptime'})
        self.assertEqual(report.uptime_percent, 90.0)
        self.assertEqual(report.uptime_incident_count, 1)
        self.assertIn('90% uptime, 1 outage', html)
        self.assertIn('No unexpected changes across 120 files', html)
        self.assertIn('Missing: Content-Security-Policy', html)
        self.assertIn('December 1, 2026', html)
        self.assertIn('covered by your plan', html)
        self.assertFalse(report.scan_stale)

    def test_site_not_on_droplet_says_not_available(self):
        site = _site(droplet_ip=None)
        _full_scan(site, _dt(date(2026, 9, 27)))
        report, html = self._render(site)
        self.assertEqual(report.sections['file_integrity']['headline'],
                         NOT_HOSTED_MSG)
        self.assertEqual(report.sections['dependencies']['headline'],
                         NOT_HOSTED_MSG)
        self.assertIn('not hosted on Aspired servers', html)
        # External sections still reported.
        self.assertEqual(report.sections['ssl']['status'], 'good')

    def test_wordpress_dependencies_come_from_wpscan(self):
        site = _site(platform='wordpress')
        scan = _full_scan(site, _dt(date(2026, 9, 27)),
                          raw_wpscan={'findings': [{}], 'is_wordpress': True})
        VulnerabilityFinding.objects.create(
            scan=scan, tool='wpscan', severity='high', status='open',
            title='Plugin vulnerability (contact-form-7): XSS')
        report, html = self._render(site)
        dep = report.sections['dependencies']
        self.assertEqual(dep['status'], 'attention')
        self.assertIn('contact-form-7', html)

    def test_stale_scan_is_not_reported(self):
        site = _site()
        _full_scan(site, _dt(date(2026, 8, 1)))   # 61 days before send
        report, html = self._render(site)
        self.assertTrue(report.scan_stale)
        self.assertIsNone(report.latest_scan)
        self.assertEqual(report.sections['ssl']['headline'],
                         SCAN_MISSING_MSG)
        self.assertIn('could not be completed', html)
        self.assertNotEqual(report.overall_status, 'green')

    def test_scan_within_35_days_is_fresh(self):
        site = _site()
        scan = _full_scan(site, _dt(date(2026, 8, 28)))  # 34 days
        report, _ = self._render(site)
        self.assertFalse(report.scan_stale)
        self.assertEqual(report.latest_scan_id, scan.id)


# ── 4. The monthly run + command ──────────────────────────────────────────

class RunSecuritySummariesTests(_TempMediaMixin, TestCase):

    def test_sends_only_to_eligible_sites(self):
        paying = _site()
        _full_scan(paying, _dt(date(2026, 9, 27)))
        not_paying = _site(maintenance_active=False)
        _full_scan(not_paying, _dt(date(2026, 9, 27)))

        result = run_security_summaries(REPORT_MONTH, now=SEND_TIME)
        self.assertEqual(result['sent'], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(paying.account.user.email, mail.outbox[0].to)
        self.assertIn('covered by your plan', mail.outbox[0].body)
        self.assertNotIn(' I ', mail.outbox[0].body)
        self.assertFalse(SecuritySummaryReport.objects.filter(
            website_new=not_paying).exists())

    def test_stale_scan_still_sends_and_alerts_admin(self):
        from core.models import SystemAlert
        site = _site()
        _full_scan(site, _dt(date(2026, 7, 1)))
        result = run_security_summaries(REPORT_MONTH, now=SEND_TIME)
        self.assertEqual(result['sent'], 1)
        self.assertTrue(SystemAlert.objects.filter(
            source='reporting.security_summary').exists())
        # client summary + admin alert
        self.assertEqual(len(mail.outbox), 2)
        self.assertTrue(any('[Security summary]' in m.subject
                            for m in mail.outbox))

    def test_already_sent_is_not_resent(self):
        site = _site()
        _full_scan(site, _dt(date(2026, 9, 27)))
        run_security_summaries(REPORT_MONTH, now=SEND_TIME)
        result = run_security_summaries(REPORT_MONTH, now=SEND_TIME)
        self.assertEqual(result['sent'], 0)
        self.assertEqual(len(mail.outbox), 1)

    def test_command_dry_run_writes_and_sends_nothing(self):
        site = _site()
        _full_scan(site, _dt(date(2026, 9, 27)))
        out = io.StringIO()
        call_command('send_security_summaries', '--dry-run',
                     '--month', '2026-09', stdout=out)
        self.assertIn(site.name, out.getvalue())
        self.assertIn('DRY RUN', out.getvalue())
        self.assertFalse(SecuritySummaryReport.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_command_website_filter_and_send(self):
        a = _site()
        b = _site()
        _full_scan(a, _dt(date(2026, 9, 27)))
        _full_scan(b, _dt(date(2026, 9, 27)))
        out = io.StringIO()
        with patch('reporting.security_summary.timezone.now',
                   return_value=SEND_TIME):
            call_command('send_security_summaries', '--month', '2026-09',
                         '--website', str(a.id), stdout=out)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(a.account.user.email, mail.outbox[0].to)

    def test_command_rejects_bad_month(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('send_security_summaries', '--month', 'Sept')


# ── 5. Scheduling ─────────────────────────────────────────────────────────

class PreSummarySweepTests(TestCase):

    @patch('reporting.tasks.run_droplet_health_check_task.delay')
    @patch('reporting.tasks.run_vulnerability_scan_task.delay')
    def test_queues_stale_eligible_sites_including_no_droplet(
            self, mock_scan, mock_check):
        from reporting.tasks import queue_pre_summary_checks
        mock_scan.return_value.id = 't1'
        mock_check.return_value.id = 't2'
        no_droplet = _site(url='https://client-hosted.example')
        hosted = _site(droplet_ip='192.0.2.30')
        fresh = _site()
        VulnerabilityScan.objects.create(
            website_new=fresh, status='complete',
            completed_at=timezone.now() - timedelta(days=3))
        _site(maintenance_active=False)  # not paying — never scanned

        queue_pre_summary_checks()

        scan = VulnerabilityScan.objects.get(
            website_new=no_droplet, is_scheduled=True)
        self.assertEqual(scan.target_ip, '')
        self.assertEqual(scan.target_url, 'https://client-hosted.example')
        self.assertTrue(VulnerabilityScan.objects.filter(
            website_new=hosted, is_scheduled=True).exists())
        self.assertFalse(VulnerabilityScan.objects.filter(
            website_new=fresh, is_scheduled=True).exists())
        self.assertEqual(mock_scan.call_count, 2)
        # Droplet check only for the hosted custom build.
        self.assertEqual(DropletHealthCheck.objects.filter(
            website_new=hosted).count(), 1)
        self.assertEqual(DropletHealthCheck.objects.count(), 1)

    @patch('reporting.tasks.run_vulnerability_scan_task.delay')
    def test_in_flight_scan_not_duplicated(self, mock_scan):
        from reporting.tasks import queue_pre_summary_checks
        site = _site()
        VulnerabilityScan.objects.create(website_new=site, status='running')
        queue_pre_summary_checks()
        mock_scan.assert_not_called()


class ScanWithoutDropletTests(TestCase):

    def test_nmap_targets_hostname_when_no_ip(self):
        from reporting.scan_runner import run_full_scan
        site = _site()
        scan = VulnerabilityScan.objects.create(
            website_new=site, target_url='https://example.com/',
            target_ip='', scan_type='full')
        with patch('reporting.scan_runner.run_nmap_scan',
                   return_value={'findings': []}) as nmap, \
                patch('reporting.scan_runner.run_nikto_scan',
                      return_value={'findings': []}), \
                patch('reporting.scan_runner.run_ssl_scan',
                      return_value={'findings': [], 'grade': 'A'}), \
                patch('reporting.scan_runner.run_wpscan',
                      return_value={'findings': [], 'skipped': True}), \
                patch('reporting.scan_runner.run_http_security_check',
                      return_value={'headers': {'present': [],
                                                'missing': []}}), \
                patch('reporting.scan_runner.generate_scan_pdf'):
            run_full_scan(str(scan.id))
        nmap.assert_called_once()
        self.assertEqual(nmap.call_args[0][0], 'example.com')
        scan.refresh_from_db()
        self.assertEqual(scan.status, 'complete')
        self.assertEqual(scan.raw_nmap['target_kind'], 'hostname')
        self.assertIn('headers', scan.raw_http)


class PerScanAutoSendTests(_TempMediaMixin, TestCase):

    def test_website_scan_auto_sends_to_account_email(self):
        from reporting.scan_runner import _notify_admin_scan_complete
        site = _site()
        site.auto_send_scan_reports = True
        site.save(update_fields=['auto_send_scan_reports'])
        scan = VulnerabilityScan.objects.create(
            website_new=site, status='complete',
            completed_at=timezone.now(), critical_count=1,
            target_url='https://example.com')
        with patch.dict(sys.modules, {'weasyprint': None}):
            _notify_admin_scan_complete(scan)
        scan.refresh_from_db()
        self.assertTrue(scan.sent_to_client)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(site.account.user.email, mail.outbox[0].to)

    def test_website_scan_without_auto_send_alerts_admin(self):
        from reporting.scan_runner import _notify_admin_scan_complete
        site = _site()
        scan = VulnerabilityScan.objects.create(
            website_new=site, status='complete', critical_count=1)
        _notify_admin_scan_complete(scan)
        self.assertFalse(scan.sent_to_client)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('[Scan]', mail.outbox[0].subject)


class HeaderCheckTests(TestCase):

    def test_check_security_headers(self):
        from reporting.scanners import check_security_headers
        res = check_security_headers({
            'Strict-Transport-Security': 'max-age=1',
            'Content-Security-Policy': "frame-ancestors 'none'",
        })
        self.assertIn('X-Frame-Options', res['present'])
        self.assertIn('Referrer-Policy', res['missing'])
        self.assertEqual(len(res['present']) + len(res['missing']), 6)
