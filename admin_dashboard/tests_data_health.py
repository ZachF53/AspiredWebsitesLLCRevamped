"""
Data Health dashboard.

Surfaces checks that previously only existed as management commands on the
server. The risk with a page like this is that it drifts from the command
it summarises and starts reassuring you about a system that is broken, so
these tests assert it reflects real state rather than merely rendering.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from clients.account_models import Account, Website
from clients.models import ClientProfile


User = get_user_model()


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class DataHealthTests(TestCase):

    def setUp(self):
        self.staff = User.objects.create_user(
            username='dhstaff', email='dhstaff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)
        self.client.force_login(self.staff)

    def _get(self):
        response = self.client.get('/admin-dashboard/data-health/')
        self.assertEqual(response.status_code, 200)
        return response

    def test_requires_staff(self):
        self.client.logout()
        response = self.client.get('/admin-dashboard/data-health/')
        self.assertIn(response.status_code, (302, 403))

    def test_clean_system_reports_clean(self):
        html = self._get().content.decode()
        self.assertIn('Everything is clean', html)

    def test_unverified_payment_is_surfaced(self):
        user = User.objects.create_user(
            username='dhclient', email='dhclient@example.com',
            password='test-pass-123')
        profile = ClientProfile.objects.create(
            user=user, firm_name='Health Check Firm')
        site = profile.migrated_account.websites.get()
        Website.objects.filter(pk=site.pk).update(
            payment_status='fully_paid')

        html = self._get().content.decode()
        self.assertIn('Health Check Firm', html)
        self.assertIn('need', html)
        self.assertNotIn('Everything is clean', html)

    def test_parity_findings_are_listed_with_their_codes(self):
        """The page must show the same codes the gate reports, so it
        cannot quietly disagree with the command."""
        user = User.objects.create_user(
            username='dhparity', email='dhparity@example.com',
            password='test-pass-123')
        profile = ClientProfile.objects.create(
            user=user, firm_name='Parity Gap Firm')
        # Break parity: a profile with no Account.
        profile.migrated_account.delete()

        html = self._get().content.decode()
        self.assertIn('client-profile-missing-account', html)

    def test_it_uses_the_same_audit_function_as_the_gate(self):
        with patch('clients.parity.audit_account_website_parity') as audit:
            audit.return_value.findings = []
            audit.return_value.error_count = 0
            audit.return_value.warning_count = 0
            audit.return_value.operational_count = 0
            audit.return_value.counts = {
                'legacy_client_profiles': 0, 'legacy_projects': 0,
                'accounts': 0, 'websites': 0,
            }
            self._get()
        audit.assert_called_once()

    def test_failed_sync_jobs_are_surfaced(self):
        from sync.models import SyncJob

        SyncJob.objects.create(
            target='moonieful', event_type='client_updated',
            status='failed', attempts=5,
            last_error='connection refused')

        html = self._get().content.decode()
        self.assertIn('client_updated', html)
        self.assertIn('connection refused', html)

    def test_open_alerts_are_surfaced(self):
        from core.models import SystemAlert

        SystemAlert.objects.create(
            severity='error', source='test.source',
            message='Something needs looking at')

        html = self._get().content.decode()
        self.assertIn('test.source', html)
        self.assertIn('Something needs looking at', html)

    def test_cutover_progress_counts_accounts_without_a_profile(self):
        user = User.objects.create_user(
            username='dhcanonical', email='dhcanonical@example.com',
            password='test-pass-123')
        Account.objects.create(user=user, name='Canonical Only Health')

        html = self._get().content.decode()
        self.assertIn('have no legacy profile', html)

    def test_it_is_reachable_from_the_sidebar(self):
        from admin_dashboard.navigation import all_items

        labels = {item.label for item in all_items()}
        self.assertIn('Data Health', labels)
