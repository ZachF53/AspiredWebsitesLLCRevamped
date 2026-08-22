"""
Tests for Apify lead sourcing (COLD_OUTREACH_AGENT.md §3).

Weighted toward the money paths. The account is on a $5/month plan and
the actor's own fetch_count default is 100000 — an unclamped run would
bill roughly $200 against it. Nothing here calls the real API.
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from outreach import apify_source, spend
from outreach.apify_source import (
    ApifyQuotaReached,
    build_actor_input,
    estimate_cost_usd,
    map_contact_to_lead,
)
from outreach.models import ApifyRun, Lead, OutreachSettings

# One real row from the account's own prior dataset, so the mapper is
# tested against the shape the actor actually emits.
REAL_ROW = {
    'city': 'Lutz', 'state': 'Florida', 'country': 'United States',
    'company_name': 'Jmi Resource (formerly 5-star Staffing Solutions)',
    'company_city': "Land O' Lakes", 'company_state': 'Florida',
    'company_phone': '+1 813-909-9466',
    'company_website': 'https://www.jmiresource.com',
    'company_full_address': '16703 Early Riser Ave, Land O Lakes, FL 34638',
    'company_linkedin': 'https://www.linkedin.com/company/5-star-staffing',
    'company_size': 58,
    'email': 'Christina@JmiResource.com',
    'full_name': 'Christina Davenport',
    'first_name': 'Christina', 'last_name': 'Davenport',
    'industry': 'Staffing & Recruiting',
    'job_title': 'Owner/president',
    'headline': 'President At Jmi Resource',
    'linkedin': 'https://www.linkedin.com/in/christina-davenport',
    'seniority_level': 'owner',
}


class CostTests(TestCase):

    def test_estimate_matches_published_pricing(self):
        # $0.02 start + 50 x $0.002
        self.assertEqual(estimate_cost_usd(50), Decimal('0.120'))
        self.assertEqual(estimate_cost_usd(100), Decimal('0.220'))

    def test_zero_results_still_costs_the_start_event(self):
        self.assertEqual(estimate_cost_usd(0), Decimal('0.02'))

    def test_default_quota_fits_the_monthly_plan(self):
        """1 run/day x 50 leads must stay inside the $5 plan."""
        cfg = OutreachSettings.load()
        daily = estimate_cost_usd(cfg.apify_max_results_per_run)
        monthly = daily * cfg.apify_max_runs_per_day * 30
        self.assertLessEqual(
            monthly, Decimal('5.00'),
            f'defaults cost ${monthly}/month against a $5 plan')


class BudgetGuardTests(TestCase):

    def setUp(self):
        cfg = OutreachSettings.load()
        cfg.apify_max_runs_per_day = 5
        cfg.apify_max_results_per_run = 50
        cfg.save()

    def test_budget_status_counts_prior_runs(self):
        ApifyRun.objects.create(
            actor_id='x', estimated_cost_usd=Decimal('1.00'))
        ApifyRun.objects.create(
            actor_id='x', estimated_cost_usd=Decimal('0.50'),
            actual_cost_usd=Decimal('0.75'))
        s = apify_source.budget_status()
        # actual wins over estimate where present
        self.assertEqual(s['spent_usd'], Decimal('1.75'))
        self.assertFalse(s['exhausted'])

    def test_refused_runs_do_not_count_against_budget(self):
        ApifyRun.objects.create(
            actor_id='x', status='refused',
            estimated_cost_usd=Decimal('4.00'))
        self.assertEqual(apify_source.budget_status()['spent_usd'],
                         Decimal('0'))

    @override_settings(APIFY_TOKEN='t', APIFY_MONTHLY_BUDGET_USD=5.0)
    def test_run_refused_when_budget_would_be_exceeded(self):
        ApifyRun.objects.create(
            actor_id='x', estimated_cost_usd=Decimal('4.95'))
        with self.assertRaises(ApifyQuotaReached) as ctx:
            apify_source.run_lead_search('law firm', 'Austin')
        self.assertIn('quota reached', str(ctx.exception).lower())
        self.assertTrue(
            ApifyRun.objects.filter(status='refused').exists(),
            'a refusal must be recorded, not silently dropped')

    @override_settings(APIFY_TOKEN='t')
    def test_run_refused_when_daily_quota_used(self):
        cfg = OutreachSettings.load()
        cfg.apify_max_runs_per_day = 1
        cfg.save()
        ApifyRun.objects.create(actor_id='x')  # today
        with self.assertRaises(ApifyQuotaReached):
            apify_source.run_lead_search('law firm', 'Austin')

    @override_settings(APIFY_TOKEN='')
    def test_missing_token_is_a_clear_error(self):
        from outreach.apify_source import ApifyError
        with self.assertRaises(ApifyError) as ctx:
            apify_source.run_lead_search('law firm', 'Austin')
        self.assertIn('APIFY_TOKEN', str(ctx.exception))

    @override_settings(APIFY_TOKEN='t')
    def test_zero_result_ceiling_disables_sourcing(self):
        cfg = OutreachSettings.load()
        cfg.apify_max_results_per_run = 0
        cfg.save()
        with self.assertRaises(ApifyQuotaReached):
            apify_source.run_lead_search('law firm', 'Austin')

    @override_settings(APIFY_TOKEN='t', APIFY_MONTHLY_BUDGET_USD=5.0)
    def test_caller_cannot_exceed_the_per_run_ceiling(self):
        """A caller asking for 5000 must be clamped, not obeyed."""
        cfg = OutreachSettings.load()
        cfg.apify_max_results_per_run = 50
        cfg.save()
        captured = {}

        def fake_start(token, run_input, timeout):
            captured.update(run_input)
            return {'id': 'r1', 'defaultDatasetId': 'd1',
                    'status': 'SUCCEEDED'}

        with patch.object(apify_source, '_start_and_wait', fake_start), \
                patch.object(apify_source, '_fetch_dataset', return_value=[]):
            apify_source.run_lead_search(
                'law firm', 'Austin', max_results=5000)

        self.assertEqual(captured['fetch_count'], 50)


class ActorInputTests(TestCase):

    def test_fetch_count_is_always_explicit(self):
        """The actor defaults fetch_count to 100000. Never inherit it."""
        payload = build_actor_input('law firm', 'Austin', fetch_count=25)
        self.assertEqual(payload['fetch_count'], 25)
        self.assertIn('fetch_count', payload)

    def test_city_and_keywords_map_to_real_schema_fields(self):
        payload = build_actor_input('personal injury', 'Round Rock',
                                    fetch_count=10)
        self.assertEqual(payload['contact_city'], ['Round Rock'])
        self.assertEqual(payload['company_keywords'], ['personal injury'])

    def test_targets_decision_makers_by_default(self):
        titles = build_actor_input('x', 'y', fetch_count=1)[
            'contact_job_title']
        self.assertIn('owner', titles)
        self.assertIn('partner', titles)

    def test_job_titles_can_be_overridden(self):
        payload = build_actor_input(
            'x', 'y', fetch_count=1, job_titles=['dentist'])
        self.assertEqual(payload['contact_job_title'], ['dentist'])


class ContactMappingTests(TestCase):

    def test_maps_a_real_row(self):
        out = map_contact_to_lead(REAL_ROW)
        self.assertEqual(
            out['firm_name'],
            'Jmi Resource (formerly 5-star Staffing Solutions)')
        self.assertEqual(out['attorney_name'], 'Christina Davenport')
        self.assertEqual(out['email'], 'christina@jmiresource.com')
        self.assertEqual(out['phone'], '+1 813-909-9466')
        self.assertEqual(out['website'], 'https://www.jmiresource.com')
        self.assertEqual(out['business_type'], 'Staffing & Recruiting')

    def test_prefers_company_location_over_contact_location(self):
        out = map_contact_to_lead(REAL_ROW)
        self.assertEqual(out['city'], "Land O' Lakes")
        self.assertEqual(out['state'], 'Florida')

    def test_email_is_lowercased_for_suppression_matching(self):
        self.assertEqual(
            map_contact_to_lead(REAL_ROW)['email'],
            'christina@jmiresource.com')

    def test_job_title_kept_as_context_for_the_drafter(self):
        note = map_contact_to_lead(REAL_ROW)['notes']
        self.assertIn('Owner/president', note)

    def test_row_without_company_name_is_dropped(self):
        self.assertIsNone(map_contact_to_lead({'email': 'a@b.com'}))
        self.assertIsNone(map_contact_to_lead({'company_name': '  '}))

    def test_non_dict_is_dropped(self):
        self.assertIsNone(map_contact_to_lead('nope'))

    def test_mapped_row_imports_end_to_end(self):
        """The real payoff: a sourced contact becomes a contactable Lead."""
        from outreach.pipeline import import_leads
        with patch('outreach.tasks.enrich_lead_task'):
            result = import_leads(
                [map_contact_to_lead(REAL_ROW)], source='apify')
        self.assertEqual(result['imported'], 1)
        lead = Lead.objects.get()
        self.assertEqual(lead.email, 'christina@jmiresource.com')
        self.assertEqual(lead.source, 'apify')

    def test_apify_leads_arrive_addressable(self):
        """Contrast with Places, which never returned an email at all —
        that was the bottleneck on the whole funnel."""
        self.assertTrue(map_contact_to_lead(REAL_ROW)['email'])


class LedgerTests(TestCase):

    @override_settings(APIFY_TOKEN='t', APIFY_MONTHLY_BUDGET_USD=5.0)
    def test_cost_recorded_before_the_run_survives_a_crash(self):
        """A run that dies mid-flight still consumed compute."""
        cfg = OutreachSettings.load()
        cfg.apify_max_results_per_run = 50
        cfg.save()

        from outreach.apify_source import ApifyError
        with patch.object(apify_source, '_start_and_wait',
                          side_effect=RuntimeError('boom')):
            with self.assertRaises(ApifyError):
                apify_source.run_lead_search('law firm', 'Austin')

        run = ApifyRun.objects.get()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.estimated_cost_usd, Decimal('0.120'))
        self.assertIn('boom', run.error)

    @override_settings(APIFY_TOKEN='t', APIFY_MONTHLY_BUDGET_USD=5.0)
    def test_successful_run_records_results(self):
        cfg = OutreachSettings.load()
        cfg.apify_max_results_per_run = 50
        cfg.save()

        with patch.object(
            apify_source, '_start_and_wait',
            return_value={'id': 'run1', 'defaultDatasetId': 'ds1',
                          'status': 'SUCCEEDED', 'usageTotalUsd': 0.11},
        ), patch.object(
            apify_source, '_fetch_dataset', return_value=[REAL_ROW],
        ):
            leads, ledger = apify_source.run_lead_search(
                'law firm', 'Austin', label='TX law')

        self.assertEqual(len(leads), 1)
        self.assertEqual(ledger.status, 'succeeded')
        self.assertEqual(ledger.results_returned, 1)
        self.assertEqual(ledger.apify_run_id, 'run1')
        self.assertEqual(ledger.actual_cost_usd, Decimal('0.11'))

    def test_apify_ledger_is_visible_to_the_quota_gate(self):
        """spend.apify_runs_today() must now find the model."""
        self.assertEqual(spend.apify_runs_today(), 0)
        ApifyRun.objects.create(actor_id='x')
        self.assertEqual(spend.apify_runs_today(), 1)

    def test_apify_spend_never_touches_the_claude_ledger(self):
        """The two pools stay independent — that is the whole point."""
        ApifyRun.objects.create(
            actor_id='x', estimated_cost_usd=Decimal('4.00'))
        self.assertEqual(spend.spent_today(), Decimal('0'))
        self.assertTrue(spend.check_spend_allowed()[0])
