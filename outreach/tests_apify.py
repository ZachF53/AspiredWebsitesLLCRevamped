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

# REAL_ROW is a real row but a 58-person staffing company, so the ICP
# screen rejects it on size — correctly. Tests that exercise the ledger
# and mapping paths rather than the screen use this ICP-compliant
# variant, so a screen change cannot quietly turn them green or red for
# reasons that have nothing to do with what they assert.
ICP_ROW = dict(REAL_ROW, company_size=12)


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
            apify_source, '_fetch_dataset', return_value=[ICP_ROW],
        ):
            leads, ledger = apify_source.run_lead_search(
                'law firm', 'Austin', label='TX law')

        self.assertEqual(len(leads), 1)
        self.assertEqual(ledger.status, 'succeeded')
        self.assertEqual(ledger.results_returned, 1)
        self.assertEqual(ledger.results_rejected, 0)
        self.assertEqual(ledger.apify_run_id, 'run1')
        self.assertEqual(ledger.actual_cost_usd, Decimal('0.11'))

    @override_settings(APIFY_TOKEN='t', APIFY_MONTHLY_BUDGET_USD=5.0)
    def test_screened_out_rows_are_counted_on_the_ledger(self):
        """A run that returns 2 rows and keeps 1 must not read as a
        clean run. The count is what makes a bad list visible."""
        cfg = OutreachSettings.load()
        cfg.apify_max_results_per_run = 50
        cfg.save()

        oversized = dict(REAL_ROW, company_size=3000)
        with patch.object(
            apify_source, '_start_and_wait',
            return_value={'id': 'run2', 'defaultDatasetId': 'ds2',
                          'status': 'SUCCEEDED'},
        ), patch.object(
            apify_source, '_fetch_dataset',
            return_value=[ICP_ROW, oversized],
        ):
            leads, ledger = apify_source.run_lead_search(
                'law firm', 'Austin', label='TX law')

        self.assertEqual(len(leads), 1)
        self.assertEqual(ledger.results_returned, 2)
        self.assertEqual(ledger.results_rejected, 1)

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


class ActorRefusalTests(TestCase):
    """The actor can run, bill, and refuse — verified live 2026-08-22.

    code_crafter/leads-finder blocks API-triggered runs on the FREE plan
    and writes a single {"error": ...} row instead of leads. The run still
    reports SUCCEEDED and still charges the $0.02 start event, so an
    undetected refusal would look like "ran fine, found nothing" every
    night while quietly spending money.
    """

    FREE_PLAN_ERROR = [{
        'error': 'Users on the free Apify plan can run the actor through '
                 'the UI and not via other methods.'
    }]

    def test_refusal_row_is_detected(self):
        from outreach.apify_source import (
            ApifyActorRefused,
            _raise_if_actor_refused,
        )
        with self.assertRaises(ApifyActorRefused):
            _raise_if_actor_refused(self.FREE_PLAN_ERROR)

    def test_real_leads_are_not_mistaken_for_a_refusal(self):
        from outreach.apify_source import _raise_if_actor_refused
        _raise_if_actor_refused([REAL_ROW])  # must not raise

    def test_empty_dataset_is_not_a_refusal(self):
        from outreach.apify_source import _raise_if_actor_refused
        _raise_if_actor_refused([])  # a genuine zero-result search

    def test_row_with_both_error_and_data_is_not_a_refusal(self):
        from outreach.apify_source import _raise_if_actor_refused
        _raise_if_actor_refused([dict(REAL_ROW, error='partial warning')])

    @override_settings(APIFY_TOKEN='t', APIFY_MONTHLY_BUDGET_USD=5.0)
    def test_refused_run_is_recorded_as_failed_with_real_cost(self):
        """It billed. The budget must reflect money actually spent."""
        from outreach.apify_source import ApifyActorRefused
        cfg = OutreachSettings.load()
        cfg.apify_max_results_per_run = 50
        cfg.apify_max_runs_per_day = 5
        cfg.save()

        with patch.object(
            apify_source, '_start_and_wait',
            return_value={'id': 'r9', 'defaultDatasetId': 'd9',
                          'status': 'SUCCEEDED'},
        ), patch.object(
            apify_source, '_fetch_dataset',
            return_value=self.FREE_PLAN_ERROR,
        ):
            with self.assertRaises(ApifyActorRefused):
                apify_source.run_lead_search('law firm', 'Austin')

        run = ApifyRun.objects.get()
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.actual_cost_usd, Decimal('0.02'))
        self.assertIn('free Apify plan', run.error)


class DatasetImportTests(TestCase):
    """The free-plan workaround: run it in the UI, import the dataset.

    Reading a dataset is free, so this costs nothing beyond whatever the
    UI run already charged.
    """

    @override_settings(APIFY_TOKEN='t')
    def test_imports_and_maps_a_ui_dataset(self):
        with patch.object(
                apify_source, '_fetch_dataset', return_value=[ICP_ROW]):
            leads, ledger = apify_source.import_from_dataset(
                'ds123', label='UI run')
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]['email'], 'christina@jmiresource.com')
        self.assertEqual(ledger.status, 'succeeded')
        self.assertEqual(ledger.dataset_id, 'ds123')

    @override_settings(APIFY_TOKEN='t')
    def test_ui_import_applies_the_same_icp_screen(self):
        """A dataset pasted in by hand comes from the identical actor
        and is no more trustworthy for it. A filter that guards only one
        door is not a filter."""
        with patch.object(
                apify_source, '_fetch_dataset', return_value=[REAL_ROW]):
            leads, ledger = apify_source.import_from_dataset(
                'ds124', label='UI run')
        self.assertEqual(leads, [])
        self.assertEqual(ledger.results_rejected, 1)

    @override_settings(APIFY_TOKEN='t')
    def test_import_does_not_charge_the_monthly_budget(self):
        """The UI run already paid. Counting it again would double-bill."""
        with patch.object(
                apify_source, '_fetch_dataset', return_value=[REAL_ROW]):
            _, ledger = apify_source.import_from_dataset('ds123')
        self.assertEqual(ledger.estimated_cost_usd, Decimal('0'))
        self.assertEqual(apify_source.budget_status()['spent_usd'],
                         Decimal('0'))

    @override_settings(APIFY_TOKEN='t')
    def test_import_surfaces_a_refusal_dataset(self):
        from outreach.apify_source import ApifyActorRefused
        with patch.object(
            apify_source, '_fetch_dataset',
            return_value=ActorRefusalTests.FREE_PLAN_ERROR,
        ):
            with self.assertRaises(ApifyActorRefused):
                apify_source.import_from_dataset('bad')
        self.assertEqual(ApifyRun.objects.get().status, 'failed')

    @override_settings(APIFY_TOKEN='t')
    def test_blank_dataset_id_is_rejected(self):
        from outreach.apify_source import ApifyError
        with self.assertRaises(ApifyError):
            apify_source.import_from_dataset('   ')

    @override_settings(APIFY_TOKEN='')
    def test_missing_token_is_rejected(self):
        from outreach.apify_source import ApifyError
        with self.assertRaises(ApifyError):
            apify_source.import_from_dataset('ds123')


class BusinessTypeNormalisationTests(TestCase):
    """Apollo says "law practice"; the TX campaign asks for "Law Firm".

    Without the map, instantly.push_leads' segment gate rejects a list
    that is entirely correct, and the failure looks like "0 leads
    eligible" with no clue why.
    """

    def test_apollo_legal_industries_map_to_law_firm(self):
        from outreach.apify_source import normalise_business_type
        for raw in ('law practice', 'Law Practice', 'legal services',
                    'LEGAL SERVICES', 'law firm'):
            with self.subTest(raw=raw):
                self.assertEqual(normalise_business_type(raw), 'Law Firm')

    def test_unmapped_industry_passes_through_titled(self):
        """An unmapped value is still information. Blanking it would let
        the lead sail through a segment check instead of failing it."""
        from outreach.apify_source import normalise_business_type
        self.assertEqual(
            normalise_business_type('marketing and advertising'),
            'Marketing And Advertising')

    def test_blank_stays_blank(self):
        from outreach.apify_source import normalise_business_type
        self.assertEqual(normalise_business_type(''), '')
        self.assertEqual(normalise_business_type(None), '')

    def test_legal_staffing_is_not_mapped_to_law_firm(self):
        """The mapping is exact, not fuzzy, on purpose -- "Legal Staffing"
        is precisely the category the law campaign excludes."""
        from outreach.apify_source import normalise_business_type
        self.assertNotEqual(
            normalise_business_type('legal staffing'), 'Law Firm')

    def test_mapped_lead_clears_the_tx_law_segment_gate(self):
        """End to end: an Apollo row must survive the gate it would
        otherwise fail."""
        from outreach import instantly
        from outreach.apify_source import map_contact_to_lead
        from outreach.models import Lead, OutreachCampaign

        mapped = map_contact_to_lead({
            'company_name': 'Chen Law Group',
            'full_name': 'Sarah Chen',
            'email': 'sarah@chenlawgroup.com',
            'industry': 'law practice',
            'company_state': 'TX',
        })
        self.assertEqual(mapped['business_type'], 'Law Firm')

        lead = Lead.objects.create(
            firm_name=mapped['firm_name'], email=mapped['email'],
            business_type=mapped['business_type'], state=mapped['state'],
            source='apify')
        campaign = OutreachCampaign.objects.create(
            name='TX Law', slug='tx-law-seg', niche='law firm',
            business_type='Law Firm', state='TX',
            instantly_campaign_id='c1', active=True)
        self.assertEqual(instantly.segment_mismatch(lead, campaign), '')


class ICPScreenTests(TestCase):
    """The screen that keeps money off rows we would never email.

    Every row here is real, from run XVpSSDrt47Y2uypQa (2026-08-29) —
    the smoke test that proved API runs work on the Starter plan and
    incidentally proved the list was 40% junk.
    """

    # Norton Rose Fulbright: a genuine law firm, ~3000 staff, and the
    # contact is a product manager. Apollo scores them seniority=owner.
    NORTON_ROSE = {
        'company_name': 'Norton Rose Fulbright',
        'full_name': 'Les Sierra', 'job_title': 'AI Product Owner',
        'seniority_level': 'owner', 'functional_level': 'product_management',
        'email': 'les.sierra@nortonrosefulbright.com',
        'industry': 'Law Practice', 'company_size': 3000,
        'company_city': 'Orlando', 'company_state': 'Florida',
        'company_phone': '+44 20 7283 6000',
    }
    # DJC Law: right state, right industry, real owner — 64 staff. This
    # one cleared every gate that existed before this screen.
    DJC_LAW = {
        'company_name': 'Djc Law', 'full_name': 'Dan Christensen',
        'job_title': 'Owner', 'seniority_level': 'owner',
        'email': 'dan@texasjustice.com', 'industry': 'Legal Services',
        'company_size': 64,
        'company_city': 'Austin', 'company_state': 'Texas',
    }
    # Hanna & Plaut: the shape we actually want.
    HANNA_PLAUT = {
        'company_name': 'Hanna & Plaut Llp', 'full_name': 'David Plaut',
        'job_title': 'Owner', 'seniority_level': 'owner',
        'email': 'dplaut@hannaplaut.com', 'industry': 'Law Practice',
        'company_size': 11,
        'company_city': 'Austin', 'company_state': 'Texas',
    }

    def test_oversized_firm_is_rejected(self):
        reason = apify_source.screen_contact(self.NORTON_ROSE)
        self.assertIn('3000', reason)

    def test_midsize_firm_below_the_year_clamp_is_rejected(self):
        """Regression: the size check first used ``_int_or_none``, which
        clamps to 1700..2100 because it parses founding years. Every
        realistic headcount came back None and the ceiling never fired —
        DJC Law's 64 sailed through while Norton Rose's 3000 did not."""
        reason = apify_source.screen_contact(self.DJC_LAW)
        self.assertIn('64', reason)
        self.assertEqual(apify_source._headcount_or_none(64), 64)
        self.assertEqual(apify_source._headcount_or_none('64'), 64)

    def test_product_owner_is_not_an_owner(self):
        """'owner' matched as a SUBSTRING of 'AI Product Owner'. The
        title screen must match whole tokens and know the phrase."""
        row = dict(self.NORTON_ROSE, company_size=8)
        self.assertIn('not a decision maker',
                      apify_source.screen_contact(row))

    def test_genuine_owner_at_a_small_firm_passes(self):
        self.assertEqual(apify_source.screen_contact(self.HANNA_PLAUT), '')

    def test_title_without_a_decision_maker_term_is_rejected(self):
        row = dict(self.HANNA_PLAUT, job_title='Associate Attorney At Law')
        # 'attorney' is a decision-maker token, so this one survives.
        self.assertEqual(apify_source.screen_contact(row), '')
        row = dict(self.HANNA_PLAUT, job_title='Marketing Coordinator')
        self.assertIn('no decision-maker term',
                      apify_source.screen_contact(row))

    def test_missing_headcount_does_not_reject(self):
        """Absent size is missing data, not evidence of a small firm.
        Rejecting on it would discard good firms Apollo has no count
        for; the other checks still apply."""
        row = dict(self.HANNA_PLAUT)
        row.pop('company_size')
        self.assertEqual(apify_source.screen_contact(row), '')

    def test_state_is_enforced_before_the_paid_stages(self):
        # Florida row, TX campaign — caught here rather than at push,
        # after MillionVerifier and a Claude call have been paid for.
        self.assertIn('targeting TX', apify_source.screen_contact(
            dict(self.NORTON_ROSE, company_size=5, job_title='Owner'),
            target_state='TX'))
        self.assertEqual(apify_source.screen_contact(
            self.HANNA_PLAUT, target_state='TX'), '')
        # Long form and abbreviation are the same state.
        self.assertEqual(apify_source.screen_contact(
            dict(self.HANNA_PLAUT, company_state='TX'),
            target_state='Texas'), '')

    def test_no_target_state_means_no_state_constraint(self):
        self.assertEqual(
            apify_source.screen_contact(
                dict(self.NORTON_ROSE, company_size=5, job_title='Owner')),
            '')

    def test_screen_contacts_partitions_and_counts(self):
        rows = [self.NORTON_ROSE, self.DJC_LAW, self.HANNA_PLAUT]
        kept, rejected = apify_source.screen_contacts(rows, target_state='TX')
        self.assertEqual([r['company_name'] for r in kept],
                         ['Hanna & Plaut Llp'])
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(reason for _, reason in rejected))


class ActorInputICPTests(TestCase):

    def test_size_buckets_are_sent_so_big_firms_are_never_billed(self):
        payload = build_actor_input('law firm', 'Austin', 'TX',
                                    fetch_count=50)
        self.assertEqual(payload['size'], ['1-10', '11-20'])

    def test_size_buckets_do_not_exceed_the_icp_ceiling(self):
        """The bucket list and the post-fetch ceiling must agree, or the
        actor bills for rows the screen then throws away."""
        top = apify_source.ICP_SIZE_BUCKETS[-1]
        self.assertEqual(int(top.split('-')[1]),
                         apify_source.ICP_MAX_COMPANY_SIZE)

    def test_state_is_not_sent_to_the_actor(self):
        """contact_location's enum is countries only — there is no
        US-state input. Sending one would be silently ignored."""
        payload = build_actor_input('law firm', 'Austin', 'TX')
        self.assertEqual(payload['contact_location'], ['united states'])
        self.assertNotIn('TX', str(payload.get('contact_location')))

    def test_fetch_count_is_still_always_explicit(self):
        self.assertEqual(
            build_actor_input('law firm', 'Austin', fetch_count=25)
            ['fetch_count'], 25)


class IndustryFilterTests(TestCase):
    """`company_keywords` is free text and matches companies that SERVE
    the niche. A 2026-08-29 run for 'law firm' in TX returned a legal
    staffing agency and a legal marketing consultancy alongside real
    firms. Both were blocked at push — after being verified and
    personalised at our expense.
    """

    STAFFING = {
        'company_name': 'Newhouse + Noblin Llc', 'full_name': 'Russell Newhouse',
        'job_title': 'Owner', 'email': 'rnewhouse@nnlegalsearch.com',
        'industry': 'Staffing & Recruiting', 'company_size': 4,
        'company_city': 'Dallas', 'company_state': 'Texas',
    }
    MARKETING = {
        'company_name': 'Stacey E. Burke, P.c.', 'full_name': 'Stacey Burke',
        'job_title': 'Owner', 'email': 'stacey@staceyeburke.com',
        'industry': 'Marketing & Advertising', 'company_size': 6,
        'company_city': 'Houston', 'company_state': 'Texas',
    }
    LAW_FIRM = {
        'company_name': 'Toler Law Group Pc', 'full_name': 'Jeff Toler',
        'job_title': 'Owner', 'email': 'jtoler@tlgiplaw.com',
        'industry': 'Law Practice', 'company_size': 11,
        'company_city': 'Austin', 'company_state': 'Texas',
    }

    def test_law_firm_maps_to_the_actors_own_enum_values(self):
        self.assertEqual(
            apify_source.industries_for_business_type('Law Firm'),
            ['law practice', 'legal services'])

    def test_our_normalisation_spellings_are_never_sent(self):
        """_BUSINESS_TYPE_MAP also holds 'attorney', 'legal', 'law firm'.
        Those are inputs to OUR normalisation, not Apollo values — the
        actor would reject them as invalid enum members."""
        sent = apify_source.industries_for_business_type('Law Firm')
        for bogus in ('attorney', 'attorneys', 'legal', 'law firm'):
            self.assertNotIn(bogus, sent)

    def test_every_sourced_industry_normalises_back_to_the_target(self):
        """The invariant that keeps the actor filter and the push-time
        segment gate from ever disagreeing. If a value we ask the actor
        for normalises to a different business_type, that lead is fetched
        and paid for and then blocked at the gate."""
        for wanted in ('Law Firm', 'Accounting', 'Financial Services',
                       'Medical Practice'):
            for industry in apify_source.industries_for_business_type(wanted):
                self.assertEqual(
                    apify_source.normalise_business_type(industry), wanted,
                    f'{industry!r} would be sourced for {wanted!r} and then '
                    f'blocked at push')

    def test_unmapped_type_does_not_constrain(self):
        """No mapping means source broadly and screen, not source
        nothing."""
        self.assertEqual(
            apify_source.industries_for_business_type('Yoga Studio'), [])
        payload = build_actor_input('yoga', 'Austin', 'TX',
                                    business_type='Yoga Studio')
        self.assertNotIn('company_industry', payload)

    def test_industry_is_sent_to_the_actor(self):
        payload = build_actor_input('law firm', 'Austin', 'TX',
                                    business_type='Law Firm')
        self.assertEqual(payload['company_industry'],
                         ['law practice', 'legal services'])

    def test_adjacent_industries_are_screened_before_the_paid_stages(self):
        for row in (self.STAFFING, self.MARKETING):
            reason = apify_source.screen_contact(
                row, target_state='TX', target_business_type='Law Firm')
            self.assertIn('targeting Law Firm', reason)
        self.assertEqual(apify_source.screen_contact(
            self.LAW_FIRM, target_state='TX',
            target_business_type='Law Firm'), '')

    def test_no_target_type_means_no_industry_constraint(self):
        self.assertEqual(
            apify_source.screen_contact(self.STAFFING, target_state='TX'), '')
