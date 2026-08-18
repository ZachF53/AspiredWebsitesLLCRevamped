"""
The nightly and monthly beats must actually do work.

Every one of these tasks reported success while doing nothing:

- `calculate_all_health_scores` walked the legacy table and handed each
  ClientProfile to `calculate_client_health`, which returns
  `ClientHealthScore(website_new=client)`. Assigning a ClientProfile to a
  Website FK raises ValueError; the task's broad `except Exception`
  logged "calc failed" and moved on, so it wrote **zero** rows on every
  nightly run. The dashboard health band and the churn-risk email had
  been dead the whole time.

- `run_monthly_intelligence` and `run_monthly_competitor_gaps` queued
  legacy ClientProfile ids into per-client tasks that resolve their
  argument as a Website id. Each queued task looked up a Website that did
  not exist and returned "Website <id> not found".

The common failure is that "did nothing" and "had nothing to do" are
indistinguishable from the outside: both return quietly, both look like a
healthy cron run. So these tests assert on the *effect* — rows written,
task ids queued — rather than that the call did not raise.

The fixtures are canonical-only, which is both the post-drop world and
the shape of every client created since the cutover.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from clients.account_models import Account, Website
from clients.models import ClientHealthScore

User = get_user_model()


def _account(username, name, **kwargs):
    user = User.objects.create_user(
        username=username, email=f'{username}@example.com',
        password='test-pass-123')
    return Account.objects.create(user=user, name=name, **kwargs)


class HealthScoreCoverageTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.account = _account('healthowner', 'Health Co', status='active')
        cls.first = Website.objects.create(
            account=cls.account, name='Health Site One', status='active')
        cls.second = Website.objects.create(
            account=cls.account, name='Health Site Two', status='active')

    def test_it_writes_a_score_for_every_active_site(self):
        """The regression that mattered: this wrote nothing at all."""
        from clients.tasks import calculate_all_health_scores

        result = calculate_all_health_scores()

        self.assertEqual(ClientHealthScore.objects.count(), 2, result)
        scored = set(
            ClientHealthScore.objects.values_list('website_new_id', flat=True))
        self.assertEqual(scored, {self.first.pk, self.second.pk})

    def test_each_score_is_attached_to_its_own_site(self):
        """Per-site, not per-account. Averaging a failing site against a
        healthy one puts a client heading for churn in the healthy band."""
        from clients.tasks import calculate_all_health_scores

        calculate_all_health_scores()
        for site in (self.first, self.second):
            with self.subTest(site=site.name):
                self.assertTrue(
                    ClientHealthScore.objects
                    .filter(website_new=site).exists())

    def test_a_tester_account_is_left_alone(self):
        from clients.tasks import calculate_all_health_scores

        tester = _account('testeracct', 'Internal Tester',
                          status='active', is_tester=True)
        Website.objects.create(
            account=tester, name='Tester Site', status='active')

        calculate_all_health_scores()
        self.assertFalse(
            ClientHealthScore.objects.filter(
                website_new__account=tester).exists())

    def test_an_inactive_account_is_left_alone(self):
        from clients.tasks import calculate_all_health_scores

        paused = _account('pausedacct', 'Paused Co', status='paused')
        Website.objects.create(
            account=paused, name='Paused Site', status='active')

        calculate_all_health_scores()
        self.assertFalse(
            ClientHealthScore.objects.filter(
                website_new__account=paused).exists())

    def test_the_churn_alert_dedupes_on_the_site(self):
        """It filtered on the legacy `client` column, which new scores do
        not set — so it never found a prior alert and would have re-sent
        the churn email every night."""
        from clients.tasks import _fire_churn_alert

        score = ClientHealthScore.objects.create(
            website_new=self.first, score=10, health_status='critical',
            churn_risk=True)
        ClientHealthScore.objects.create(
            website_new=self.first, score=12, health_status='critical',
            churn_risk=True)

        with patch('clients.tasks.send_mail') as send:
            _fire_churn_alert(self.first, score)
        send.assert_not_called()

    def test_the_churn_alert_sends_the_first_time(self):
        from clients.tasks import _fire_churn_alert

        score = ClientHealthScore.objects.create(
            website_new=self.first, score=10, health_status='critical',
            churn_risk=True)

        with patch('clients.tasks.send_mail') as send:
            _fire_churn_alert(self.first, score)
        send.assert_called_once()
        self.assertIn('Health Site One', send.call_args.args[0])


class MonthlyBeatCoverageTests(TestCase):
    """Both beats queued ids the per-client task could not resolve."""

    @classmethod
    def setUpTestData(cls):
        cls.account = _account('beatowner', 'Beat Co', status='active')
        cls.site = Website.objects.create(
            account=cls.account, name='Beat Site', status='active')

    def test_intelligence_queues_website_ids(self):
        from clients.tasks import run_monthly_intelligence

        with patch('clients.tasks.run_intelligence_for_client'
                   '.apply_async') as queued:
            result = run_monthly_intelligence()

        queued.assert_called_once()
        self.assertEqual(
            queued.call_args.kwargs['args'], [str(self.site.id)], result)

    def test_competitor_gaps_only_queues_sites_that_have_competitors(self):
        from clients.models import ClientCompetitor
        from clients.tasks import run_monthly_competitor_gaps

        with patch('clients.tasks.run_competitor_gap_analysis'
                   '.apply_async') as queued:
            run_monthly_competitor_gaps()
        queued.assert_not_called()

        ClientCompetitor.objects.create(
            website_new=self.site, name='Rival LLC',
            domain='rival.example.com')

        with patch('clients.tasks.run_competitor_gap_analysis'
                   '.apply_async') as queued:
            run_monthly_competitor_gaps()

        queued.assert_called_once()
        self.assertEqual(
            queued.call_args.kwargs['args'], [str(self.site.id)])

    def test_a_site_with_two_competitors_is_queued_once(self):
        """`competitors_new__isnull=False` is a join, so without
        `.distinct()` a site fans out one row per competitor and its
        analysis is queued — and billed to the Claude API — twice."""
        from clients.models import ClientCompetitor
        from clients.tasks import run_monthly_competitor_gaps

        for n in range(2):
            ClientCompetitor.objects.create(
                website_new=self.site, name=f'Rival {n}',
                domain=f'rival{n}.example.com')

        with patch('clients.tasks.run_competitor_gap_analysis'
                   '.apply_async') as queued:
            run_monthly_competitor_gaps()

        self.assertEqual(queued.call_count, 1)
