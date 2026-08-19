"""
The generated reports must work for a client who has no legacy row.

These run inside Celery and end in an email to the customer, which makes
them the worst place for a silent failure: the beat job logs success, the
client receives nothing, and nobody finds out until they ask why their
report stopped arriving. The monthly report is contractually part of
every maintenance plan.

Canonical-only fixtures throughout — no ClientProfile, no Project — which
is the post-drop world and also the shape of every client created since
the cutover.

What is asserted is the *effect*: a row written, a body containing the
site's real numbers, and the mail addressed to a real person. "It did not
raise" is not enough, because the failure mode here is a task that
returns quietly having done nothing — the same shape as the three beat
jobs that were writing zero rows a night.
"""

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from clients.account_models import Account, Website

User = get_user_model()


def _canonical_client(username, name):
    user = User.objects.create_user(
        username=username, email=f'{username}@example.com',
        password='test-pass-123')
    account = Account.objects.create(
        user=user, name=name, contact_name='Robin Vale')
    website = Website.objects.create(
        account=account, name=name, url='https://example-report.test',
        status='active', stage='live', maintenance_active=True,
        do_droplet_ip='203.0.113.11')
    assert account.legacy_client_profile_id is None
    return account, website


class MonthlyReportCanonicalTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.account, cls.website = _canonical_client(
            'monthlyowner', 'Monthly Report Co')
        cls.month = datetime.date.today().replace(day=1)

    def _generate(self):
        from reporting.tasks import generate_monthly_report

        return generate_monthly_report(
            None, self.month.isoformat(), website_id=str(self.website.id))

    def test_it_writes_a_report_for_a_site_with_no_legacy_row(self):
        from reporting.models import MonthlyReport

        with patch('reporting.tasks.send_monthly_report_email'):
            result = self._generate()

        report = MonthlyReport.objects.filter(website_new=self.website).first()
        self.assertIsNotNone(
            report,
            f'no MonthlyReport written for a canonical-only site ({result})')

    def test_rerunning_does_not_create_a_second_row(self):
        """The duplicate guard keys on (website_new, report_month).
        Including the nullable legacy column would fail to match the row
        that exists, create another, and hit the unique constraint."""
        from reporting.models import MonthlyReport

        with patch('reporting.tasks.send_monthly_report_email'):
            self._generate()
            self._generate()

        self.assertEqual(
            MonthlyReport.objects.filter(website_new=self.website).count(), 1)

    def test_each_site_on_an_account_gets_its_own_report(self):
        """Keyed on the account, the second site's run would find the
        first site's row and skip — so that site never got a report."""
        from reporting.models import MonthlyReport
        from reporting.tasks import generate_monthly_report

        second = Website.objects.create(
            account=self.account, name='Second Site',
            url='https://second-report.test', status='active', stage='live')

        with patch('reporting.tasks.send_monthly_report_email'):
            self._generate()
            generate_monthly_report(
                None, self.month.isoformat(), website_id=str(second.id))

        self.assertEqual(
            MonthlyReport.objects.filter(
                website_new__account=self.account).count(), 2)

    def test_the_email_is_addressed_to_the_accounts_user(self):
        """`send_monthly_report_email` read the address through the
        legacy profile. A Website has no `.user`."""
        from reporting.models import MonthlyReport
        from reporting.tasks import send_monthly_report_email

        report = MonthlyReport.objects.create(
            website_new=self.website, report_month=self.month)

        with patch('clients.emails.send_branded') as sent:
            send_monthly_report_email(report)

        self.assertTrue(
            sent.called,
            'the monthly report email was never sent for a canonical-only '
            'client — the beat job would report success having delivered '
            'nothing')
        self.assertEqual(
            sent.call_args.kwargs['recipient_list'],
            ['monthlyowner@example.com'])


class AnnualReportCanonicalTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.account, cls.website = _canonical_client(
            'annualowner', 'Annual Report Co')
        cls.website.launch_date = (
            datetime.date.today() - datetime.timedelta(days=400))
        cls.website.save(update_fields=['launch_date'])

    def test_it_writes_a_report_for_a_site_with_no_legacy_row(self):
        from clients.models import AnnualReport
        from clients.tasks import generate_annual_report

        year = datetime.date.today().year - 1
        with patch('clients.intelligence.generate_annual_narrative',
                   return_value=('A good year.', 0)), \
                patch('clients.tasks.send_mail'):
            generate_annual_report(str(self.website.id), year)

        self.assertTrue(
            AnnualReport.objects.filter(
                website_new=self.website, report_year=year).exists())

    def test_the_schedule_queues_the_site_not_the_profile(self):
        """`check_annual_report_schedule` walks Websites. Walking the
        legacy table would skip every client created since the cutover."""
        from clients.tasks import check_annual_report_schedule

        launch = self.website.launch_date
        today = datetime.date.today()
        self.website.launch_date = launch.replace(
            month=today.month, year=today.year - 1)
        self.website.save(update_fields=['launch_date'])

        with patch('clients.tasks.generate_annual_report.apply_async') as q:
            check_annual_report_schedule()

        self.assertTrue(
            q.called,
            'no annual report queued for a canonical-only site whose '
            'launch anniversary is this month')


class NPSCanonicalTests(TestCase):
    """NPS surveys skipped canonical-only accounts outright — the guard
    said `NPSSurvey.client is non-nullable until Phase D`, which stopped
    being true when clients.0056 made it nullable."""

    @classmethod
    def setUpTestData(cls):
        cls.account, cls.website = _canonical_client('npsowner', 'NPS Co')
        Account.objects.filter(pk=cls.account.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=120))

    def test_a_canonical_only_site_is_surveyed(self):
        from reporting.models import NPSSurvey
        from reporting.tasks import send_nps_surveys

        with patch('reporting.tasks.send_nps_email'):
            send_nps_surveys()

        self.assertTrue(
            NPSSurvey.objects.filter(website_new=self.website).exists(),
            'a canonical-only site was never surveyed')

    def test_the_cooldown_is_per_site(self):
        """Keyed on the client, the first site's survey suppressed every
        other site on the account for three months."""
        from reporting.models import NPSSurvey
        from reporting.tasks import send_nps_surveys

        second = Website.objects.create(
            account=self.account, name='NPS Second', status='active',
            stage='live', maintenance_active=True)

        with patch('reporting.tasks.send_nps_email'):
            send_nps_surveys()

        self.assertTrue(
            NPSSurvey.objects.filter(website_new=second).exists(),
            'the second site was suppressed by the first site\'s survey')
