"""
Coverage for the `fix_stuck_onboarding_status` report-only command,
written alongside the `_on_intake_submitted` fix (clients/views.py) —
see clients/tests_intake_submit.py for the fix itself.
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from clients.account_models import Account, Website

User = get_user_model()


class FixStuckOnboardingStatusTests(TestCase):

    def setUp(self):
        u = User.objects.create_user(
            username='stuckuser', email='stuck@example.com', password='x')
        self.account = Account.objects.filter(user=u).first() or (
            Account.objects.create(user=u, name='Stuck Co'))
        self.account.websites.all().delete()

    def _run(self):
        out = StringIO()
        call_command('fix_stuck_onboarding_status', stdout=out)
        return out.getvalue()

    def test_reports_website_with_invalid_status(self):
        website = Website.objects.create(
            account=self.account, name='Stuck Site')
        Website.objects.filter(pk=website.pk).update(
            onboarding_status='onboarding_complete')

        output = self._run()

        self.assertIn(str(website.pk), output)
        self.assertIn("onboarding_status='onboarding_complete'", output)
        self.assertIn('1 Website(s)', output)

    def test_reports_account_with_invalid_status(self):
        Account.objects.filter(pk=self.account.pk).update(
            onboarding_status='onboarding_complete')

        output = self._run()

        self.assertIn(str(self.account.pk), output)
        self.assertIn('1 Account(s)', output)

    def test_clean_data_reports_none_found(self):
        Website.objects.create(
            account=self.account, name='Fine Site',
            onboarding_status='intake_complete')

        output = self._run()

        self.assertIn('0 Website(s), 0 Account(s)', output)

    def test_command_never_writes(self):
        website = Website.objects.create(
            account=self.account, name='Untouched Site')
        Website.objects.filter(pk=website.pk).update(
            onboarding_status='onboarding_complete')

        self._run()

        website.refresh_from_db()
        self.assertEqual(website.onboarding_status, 'onboarding_complete')
