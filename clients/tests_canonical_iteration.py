"""
Scheduled work must not silently skip canonical-only accounts.

Every `for client in ClientProfile.objects.filter(...)` loop in the
scheduler drops an Account that has no legacy profile — the shape every
account created after the cutover takes. The task then reports success
having done less work than it should have.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from clients.account_models import Account
from clients.canonical_iteration import profiles_with_coverage_report
from clients.models import ClientProfile


User = get_user_model()


class CoverageReportTests(TestCase):

    def setUp(self):
        user = User.objects.create_user(
            username='covered', email='covered@example.com',
            password='test-pass-123')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Covered Firm', status='active')

    def test_it_yields_exactly_what_the_old_queryset_yielded(self):
        result = profiles_with_coverage_report(
            'test_task', status='active', is_tester=False)
        self.assertEqual(list(result), [self.profile])

    def test_no_alert_when_every_account_has_a_profile(self):
        with patch('core.system_alerts.record_alert') as alert:
            list(profiles_with_coverage_report('test_task', status='active'))
        alert.assert_not_called()

    def test_a_canonical_only_account_is_reported(self):
        """The account is still not processed — the helper cannot invent
        a ClientProfile — but the run no longer claims full coverage."""
        user = User.objects.create_user(
            username='canonicalonly', email='canonicalonly@example.com',
            password='test-pass-123')
        Account.objects.create(user=user, name='Canonical Only Ltd')

        with patch('core.system_alerts.record_alert') as alert:
            result = list(
                profiles_with_coverage_report('test_task', status='active'))

        self.assertEqual(result, [self.profile])
        self.assertEqual(alert.call_count, 1)
        kwargs = alert.call_args.kwargs
        self.assertEqual(kwargs['severity'], 'error')
        self.assertIn('Canonical Only Ltd', kwargs['detail'])
        self.assertIn('test_task', kwargs['source'])

    def test_the_alert_names_the_task_that_skipped_them(self):
        user = User.objects.create_user(
            username='canonicalonly2', email='co2@example.com',
            password='test-pass-123')
        Account.objects.create(user=user, name='Another Canonical Ltd')

        with patch('core.system_alerts.record_alert') as alert:
            list(profiles_with_coverage_report(
                'calculate_all_health_scores', status='active'))

        self.assertIn(
            'calculate_all_health_scores', alert.call_args.kwargs['message'])

    def test_a_failure_to_alert_never_breaks_the_task(self):
        user = User.objects.create_user(
            username='canonicalonly3', email='co3@example.com',
            password='test-pass-123')
        Account.objects.create(user=user, name='Third Canonical Ltd')

        with patch('core.system_alerts.record_alert',
                   side_effect=RuntimeError('alerting is down')):
            result = list(
                profiles_with_coverage_report('test_task', status='active'))

        self.assertEqual(result, [self.profile])


class HealthScoreTaskTests(TestCase):

    def test_it_reports_skipped_accounts(self):
        from clients.tasks import calculate_all_health_scores

        user = User.objects.create_user(
            username='hs-legacy', email='hs-legacy@example.com',
            password='test-pass-123')
        ClientProfile.objects.create(
            user=user, firm_name='Health Firm', status='active')

        orphan_user = User.objects.create_user(
            username='hs-canonical', email='hs-canonical@example.com',
            password='test-pass-123')
        Account.objects.create(user=orphan_user, name='Health Canonical Ltd')

        with patch('core.system_alerts.record_alert') as alert:
            result = calculate_all_health_scores()

        self.assertIn('health score', result)
        self.assertEqual(alert.call_count, 1)
