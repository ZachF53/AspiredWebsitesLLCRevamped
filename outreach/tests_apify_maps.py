"""
Tests for outreach/apify_maps.py — the Google Maps / no-website source.

PLACE_ROW below is a real row captured from a live 10-place probe run of
compass/crawler-google-places on 2026-10-10 (San Antonio, "HVAC
contractor", website=withoutWebsite), trimmed to the fields the mapper
and screens actually read. Using real field names and real value TYPES
matters here: ``state`` arrives as 'Texas' and not 'TX', ``website``
arrives as None and not '', and a mapper written against guesses would
have been wrong about both.
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings

from outreach import apify_maps
from outreach.apify_source import ApifyError, ApifyQuotaReached
from outreach.models import ApifyRun, Lead, OutreachSettings
from outreach.scoring import score_lead


PLACE_ROW = {
    'title': 'San Antonio Air Conditioning & Heating',
    'address': '414 Live Oak St, San Antonio, TX 78202',
    'street': '414 Live Oak St',
    'city': 'San Antonio',
    'state': 'Texas',
    'postalCode': '78202',
    'countryCode': 'US',
    'phone': '(726) 263-1902',
    'phoneUnformatted': '+17262631902',
    'website': None,
    'totalScore': 5,
    'reviewsCount': 1,
    'categoryName': 'HVAC contractor',
    'categories': ['HVAC contractor', 'Air conditioning contractor'],
    'neighborhood': 'Dignowity Hill',
    'claimThisBusiness': False,
    'permanentlyClosed': False,
    'temporarilyClosed': False,
    'placeId': 'ChIJu4GOtBj3XIYRtldSyU1BMQQ',
    'url': 'https://www.google.com/maps/search/?api=1&query=San+Antonio',
    'openingHours': [{'day': 'Monday', 'hours': 'Open 24 hours'}],
}


class CostTests(TestCase):

    @override_settings(APIFY_MAPS_COST_PER_PLACE_USD=0.003,
                       APIFY_MAPS_COST_PER_FILTER_USD=0.001)
    def test_cost_counts_filters_per_place(self):
        # 1,000 places, one filter: $3.00 of places + $1.00 of filter.
        self.assertEqual(apify_maps.estimate_cost_usd(1000, 1),
                         Decimal('4.000'))
        # A second filter is another $1.00 — filters are not free, and
        # billing them per place is the part that is easy to forget.
        self.assertEqual(apify_maps.estimate_cost_usd(1000, 2),
                         Decimal('5.000'))

    @override_settings(APIFY_MAPS_COST_PER_PLACE_USD=0.003,
                       APIFY_MAPS_COST_PER_FILTER_USD=0.001)
    def test_zero_filters_is_places_only(self):
        self.assertEqual(apify_maps.estimate_cost_usd(100, 0),
                         Decimal('0.300'))


class ActorInputTests(TestCase):

    def test_no_website_filter_is_the_default(self):
        payload, n = apify_maps.build_actor_input(
            niche='HVAC contractor', city='San Antonio', state='TX',
            max_places=50)
        self.assertEqual(payload['website'], 'withoutWebsite')
        self.assertEqual(payload['maxCrawledPlacesPerSearch'], 50)
        self.assertEqual(payload['searchStringsArray'], ['HVAC contractor'])
        self.assertEqual(payload['locationQuery'], 'San Antonio, TX')
        # website + skipClosedPlaces
        self.assertEqual(n, 2)

    def test_place_count_is_always_explicit(self):
        # The guard that matters: this actor bills per place, so the
        # count must never be left to the actor's own default.
        payload, _ = apify_maps.build_actor_input(
            niche='x', city='y', max_places=7)
        self.assertEqual(payload['maxCrawledPlacesPerSearch'], 7)

    def test_all_places_drops_the_website_filter_and_its_cost(self):
        payload, n = apify_maps.build_actor_input(
            niche='x', city='y', website_filter=apify_maps.WEBSITE_ANY,
            skip_closed=False)
        self.assertNotIn('website', payload)
        self.assertEqual(n, 0)

    def test_min_stars_counts_as_a_filter(self):
        _, n = apify_maps.build_actor_input(
            niche='x', city='y', skip_closed=False, min_stars='four')
        self.assertEqual(n, 2)  # website + stars


class ScreenTests(TestCase):

    def test_real_row_passes(self):
        self.assertEqual(apify_maps.screen_place(PLACE_ROW), '')

    def test_rejects_a_place_that_has_a_website(self):
        # The server-side filter did this on 2026-10-10. This asserts we
        # would NOTICE if it ever silently stopped — otherwise we would
        # pay for has-website places and hand them the +4 "no website"
        # score that drives prioritisation.
        row = dict(PLACE_ROW, website='https://example.com')
        why = apify_maps.screen_place(row)
        self.assertIn('Has a website', why)

    def test_with_website_filter_inverts_the_check(self):
        why = apify_maps.screen_place(
            PLACE_ROW, website_filter=apify_maps.WEBSITE_WITH)
        self.assertIn('no website', why)

    def test_rejects_closed_places(self):
        self.assertIn('Permanently closed', apify_maps.screen_place(
            dict(PLACE_ROW, permanentlyClosed=True)))
        self.assertIn('Temporarily closed', apify_maps.screen_place(
            dict(PLACE_ROW, temporarilyClosed=True)))

    def test_rejects_nameless_row(self):
        self.assertIn('No business name', apify_maps.screen_place(
            dict(PLACE_ROW, title='')))

    def test_rejects_row_with_no_contact_channel_at_all(self):
        row = dict(PLACE_ROW, phone='', phoneUnformatted='', website=None)
        self.assertIn('No phone', apify_maps.screen_place(row))

    def test_state_screen_handles_the_full_name_the_actor_sends(self):
        # The actor sends 'Texas'; the target is 'TX'. A naive compare
        # would reject a correct row.
        self.assertEqual(
            apify_maps.screen_place(PLACE_ROW, target_state='TX'), '')
        self.assertIn(
            'targeting GA',
            apify_maps.screen_place(PLACE_ROW, target_state='GA'))

    def test_service_area_business_with_no_state_is_kept(self):
        # 45 of 100 rows on the real 2026-10-10 San Antonio run looked
        # exactly like this: no address, no city, no state, just a name
        # and a 210 phone. Rejecting them for "unknown state" threw away
        # the smallest operators in the batch, which is the ICP.
        row = dict(PLACE_ROW, state=None, city=None, address=None,
                   postalCode=None)
        self.assertEqual(
            apify_maps.screen_place(row, target_state='TX'), '')

    def test_known_wrong_state_is_still_rejected(self):
        # Relaxing the check for MISSING data must not relax it for data
        # that is present and wrong.
        row = dict(PLACE_ROW, state='California')
        self.assertIn(
            'targeting TX',
            apify_maps.screen_place(row, target_state='TX'))

    def test_screen_places_reports_rejections_rather_than_dropping(self):
        rows = [PLACE_ROW, dict(PLACE_ROW, title='Closed Co',
                                permanentlyClosed=True)]
        kept, rejected = apify_maps.screen_places(rows)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(rejected), 1)
        self.assertIn('Permanently closed', rejected[0][1])


class MapperTests(TestCase):

    def test_maps_real_row_onto_pipeline_keys(self):
        lead = apify_maps.map_place_to_lead(
            PLACE_ROW, business_type='HVAC')
        self.assertEqual(lead['firm_name'],
                         'San Antonio Air Conditioning & Heating')
        self.assertEqual(lead['city'], 'San Antonio')
        self.assertEqual(lead['phone'], '(726) 263-1902')
        self.assertEqual(lead['business_type'], 'HVAC')
        self.assertTrue(lead['has_google_business'])

    def test_state_is_normalised_to_two_letters(self):
        # 'Texas' -> 'TX'. The Lead model, the campaign segment gate and
        # the ICP screens all compare on the abbreviation.
        lead = apify_maps.map_place_to_lead(PLACE_ROW)
        self.assertEqual(lead['state'], 'TX')

    def test_review_count_uses_the_key_scoring_reads(self):
        # A 'reviewsCount' key here would score every lead as 0 reviews.
        lead = apify_maps.map_place_to_lead(PLACE_ROW)
        self.assertEqual(lead['google_review_count'], 1)
        self.assertEqual(lead['google_rating'], 5)

    def test_never_produces_an_email(self):
        # The load-bearing invariant of this whole module. Maps has no
        # email, and a guessed info@ would be an unverified address
        # entering an email pipeline.
        lead = apify_maps.map_place_to_lead(PLACE_ROW)
        self.assertEqual(lead.get('email', ''), '')

    def test_website_stays_blank_so_scoring_awards_the_four_points(self):
        lead = apify_maps.map_place_to_lead(PLACE_ROW)
        self.assertEqual(lead['website'], '')
        score, temperature = score_lead(lead)
        # +4 no website, +1 review count under 10, +1 no social.
        self.assertGreaterEqual(score, 4)
        self.assertIn(temperature, ('warm', 'hot'))

    def test_service_area_row_takes_the_searched_city_and_state(self):
        # No address is not "no location". The fallback records the
        # metro Google returns the business FOR, which is what a
        # campaign segment needs, without inventing a street address.
        row = dict(PLACE_ROW, state=None, city=None, address=None)
        lead = apify_maps.map_place_to_lead(
            row, business_type='HVAC',
            fallback_city='San Antonio', fallback_state='TX')
        self.assertEqual(lead['city'], 'San Antonio')
        self.assertEqual(lead['state'], 'TX')
        # The address itself stays blank rather than being fabricated.
        self.assertEqual(lead['address'], '')

    def test_real_city_beats_the_fallback(self):
        lead = apify_maps.map_place_to_lead(
            PLACE_ROW, fallback_city='Austin', fallback_state='GA')
        self.assertEqual(lead['city'], 'San Antonio')
        self.assertEqual(lead['state'], 'TX')

    def test_contact_name_is_blank_not_the_company_name(self):
        # Maps returns a business. Putting the company name in the
        # contact-name field would make copy address a company as a
        # person.
        lead = apify_maps.map_place_to_lead(PLACE_ROW)
        self.assertEqual(lead['attorney_name'], '')

    def test_notes_carry_checkable_context_only(self):
        notes = apify_maps.map_place_to_lead(PLACE_ROW)['notes']
        self.assertIn('HVAC contractor', notes)
        self.assertIn('5 stars, 1 reviews', notes)
        self.assertIn('24-hour', notes)

    def test_unclaimed_listing_is_recorded(self):
        notes = apify_maps.map_place_to_lead(
            dict(PLACE_ROW, claimThisBusiness=True))['notes']
        self.assertIn('UNCLAIMED', notes)

    def test_nameless_row_maps_to_none(self):
        self.assertIsNone(apify_maps.map_place_to_lead(
            dict(PLACE_ROW, title='   ')))
        self.assertIsNone(apify_maps.map_place_to_lead('not a dict'))


class GuardTests(TestCase):

    @override_settings(APIFY_TOKEN='')
    def test_missing_token_refuses_before_any_http(self):
        with self.assertRaises(ApifyError):
            apify_maps.run_maps_search('HVAC contractor', 'San Antonio')

    @override_settings(APIFY_TOKEN='tok', APIFY_MONTHLY_BUDGET_USD=0.01,
                       APIFY_MAPS_COST_PER_PLACE_USD=0.003,
                       APIFY_MAPS_COST_PER_FILTER_USD=0.001)
    def test_budget_guard_refuses_and_records_a_refused_run(self):
        cfg = OutreachSettings.load()
        cfg.apify_max_results_per_run = 100
        cfg.save()
        with self.assertRaises(ApifyQuotaReached):
            apify_maps.run_maps_search(
                'HVAC contractor', 'San Antonio', state='TX')
        run = ApifyRun.objects.latest('started_at')
        self.assertEqual(run.status, 'refused')
        # A refused run must not count against the month's spend.
        self.assertEqual(run.actual_cost_usd, None)

    @override_settings(APIFY_TOKEN='tok')
    def test_zero_max_results_disables_sourcing(self):
        cfg = OutreachSettings.load()
        cfg.apify_max_results_per_run = 0
        cfg.save()
        with self.assertRaises(ApifyQuotaReached):
            apify_maps.run_maps_search('HVAC contractor', 'San Antonio')

    @override_settings(APIFY_TOKEN='tok', APIFY_MONTHLY_BUDGET_USD=50.0,
                       APIFY_MAX_TOTAL_CHARGE_USD=0.10,
                       APIFY_MAPS_MIN_MAX_CHARGE_USD=0.50)
    def test_charge_ceiling_is_floored_to_the_actor_minimum(self):
        # Sending maxTotalChargeUsd below the actor's minimum is an
        # outright HTTP 400, so a too-low setting must be raised rather
        # than passed through and discovered at runtime.
        cfg = OutreachSettings.load()
        cfg.apify_max_results_per_run = 10
        cfg.save()
        seen = {}

        def fake_post(url, **kw):
            seen.update(kw.get('params') or {})
            raise RuntimeError('stop here')

        with patch('outreach.apify_maps.requests.post', fake_post):
            with self.assertRaises(ApifyError):
                apify_maps.run_maps_search(
                    'HVAC contractor', 'San Antonio', max_results=10)
        self.assertEqual(seen.get('maxTotalChargeUsd'), 0.50)


class EndToEndTests(TestCase):

    @override_settings(APIFY_TOKEN='tok', APIFY_MONTHLY_BUDGET_USD=50.0)
    def test_successful_run_imports_and_ledgers(self):
        cfg = OutreachSettings.load()
        cfg.apify_max_results_per_run = 10
        cfg.save()

        run_obj = {'id': 'RUN1', 'status': 'SUCCEEDED',
                   'defaultDatasetId': 'DS1', 'usageTotalUsd': 0.0152}
        rows = [PLACE_ROW,
                dict(PLACE_ROW, title='Has A Site LLC',
                     website='https://hasasite.example')]

        with patch.object(apify_maps, '_start_and_wait',
                          return_value=run_obj), \
                patch.object(apify_maps, '_fetch_dataset',
                             return_value=rows):
            leads, ledger = apify_maps.run_maps_search(
                'HVAC contractor', 'San Antonio', state='TX',
                max_results=10, business_type='HVAC')

        # The has-website row is billed but screened out, and the ledger
        # says so rather than letting 2-returned/1-kept read as clean.
        self.assertEqual(len(leads), 1)
        self.assertEqual(ledger.results_returned, 2)
        self.assertEqual(ledger.results_rejected, 1)
        self.assertEqual(ledger.status, 'succeeded')
        self.assertEqual(ledger.actual_cost_usd, Decimal('0.0152'))

        from outreach.pipeline import import_leads
        res = import_leads(leads, source='google_maps',
                           business_type_override='HVAC')
        self.assertEqual(res['imported'], 1)
        lead = Lead.objects.get()
        self.assertEqual(lead.business_type, 'HVAC')
        self.assertEqual(lead.source, 'google_maps')
        self.assertEqual(lead.email, '')
        self.assertEqual(lead.website, '')
        self.assertEqual(lead.state, 'TX')
        self.assertEqual(lead.google_review_count, 1)

    @override_settings(APIFY_TOKEN='tok')
    def test_dataset_import_is_free_and_does_not_charge_the_budget(self):
        with patch.object(apify_maps, '_fetch_dataset',
                          return_value=[PLACE_ROW]):
            leads, ledger = apify_maps.import_from_dataset(
                'DS9', target_state='TX', business_type='HVAC')
        self.assertEqual(len(leads), 1)
        self.assertEqual(ledger.estimated_cost_usd, Decimal('0'))
        self.assertEqual(ledger.status, 'succeeded')

    @override_settings(APIFY_TOKEN='tok')
    def test_dataset_import_requires_an_id(self):
        with self.assertRaises(ApifyError):
            apify_maps.import_from_dataset('   ')


class RunsPerDayCountingTests(TestCase):
    """``spend.apify_runs_today`` must count only billable actor runs.

    Observed 2026-10-10: 6 ApifyRun rows for the day of which 3 were real
    runs, and an authorised $0.44 scrape was refused as "5 of 5 used".
    """

    def _row(self, **kw):
        from outreach.models import ApifyRun
        defaults = dict(
            actor_id='compass~crawler-google-places',
            status='succeeded', apify_run_id='REAL1',
            estimated_cost_usd=Decimal('0.50'))
        defaults.update(kw)
        return ApifyRun.objects.create(**defaults)

    def test_real_run_counts(self):
        from outreach import spend
        self._row()
        self.assertEqual(spend.apify_runs_today(), 1)

    def test_refused_run_does_not_count(self):
        # Nothing started and nothing billed. Counting it is also
        # self-reinforcing: each refusal after the cap adds another row.
        from outreach import spend
        self._row(status='refused', apify_run_id='',
                  estimated_cost_usd=Decimal('0.50'))
        self.assertEqual(spend.apify_runs_today(), 0)

    def test_free_dataset_import_does_not_count(self):
        # A dataset read starts no actor and costs nothing.
        from outreach import spend
        self._row(billable=False, apify_run_id='',
                  estimated_cost_usd=Decimal('0'))
        self.assertEqual(spend.apify_runs_today(), 0)

    def test_in_flight_run_still_counts(self):
        # A real run has no apify_run_id until Apify assigns one. It must
        # still occupy a slot — an earlier version of this guard inferred
        # "not billable" from the missing id and would have let an extra
        # paid run start.
        from outreach import spend
        self._row(status='running', apify_run_id='',
                  estimated_cost_usd=Decimal('0.50'))
        self.assertEqual(spend.apify_runs_today(), 1)

    def test_bare_row_counts_because_billable_defaults_true(self):
        # A row created with nothing but an actor id is a real run as far
        # as the quota is concerned. Failing open here would be the
        # dangerous direction.
        from outreach.models import ApifyRun
        from outreach import spend
        ApifyRun.objects.create(actor_id='x')
        self.assertEqual(spend.apify_runs_today(), 1)

    def test_dataset_import_sets_billable_false(self):
        from outreach import spend
        with override_settings(APIFY_TOKEN='tok'):
            with patch.object(apify_maps, '_fetch_dataset',
                              return_value=[PLACE_ROW]):
                _, ledger = apify_maps.import_from_dataset('DS7')
        self.assertFalse(ledger.billable)
        self.assertEqual(spend.apify_runs_today(), 0)

    def test_failed_run_that_billed_still_counts(self):
        from outreach import spend
        self._row(status='failed', apify_run_id='REAL2',
                  estimated_cost_usd=Decimal('0.50'))
        self.assertEqual(spend.apify_runs_today(), 1)


class CategoryFilterTests(TestCase):
    """Filtering on Google's OWN category, not a free-text phrase.

    2026-10-11: the phrase "air conditioning repair" returned AUTOMOTIVE
    A/C shops. 42 of 66 imported leads were tyre places, transmissions,
    collision repair and mobile mechanics -- all stamped HVAC because
    the operator passed --business-type HVAC. Google categorises those
    as "Auto repair shop", never as an HVAC trade, so the category
    filter makes that failure impossible rather than merely unlikely.
    """

    def test_category_filter_is_sent_and_counted(self):
        payload, n = apify_maps.build_actor_input(
            niche='HVAC contractor', city='San Antonio', state='TX',
            max_places=100, categories=apify_maps.HVAC_CATEGORIES)
        self.assertIn('categoryFilterWords', payload)
        self.assertIn('hvac contractor', payload['categoryFilterWords'])
        # website + skipClosed + category. It costs $0.001/place more and
        # the count must reflect that or the estimate under-reports.
        self.assertEqual(n, 3)

    def test_no_categories_means_no_filter_and_no_charge(self):
        payload, n = apify_maps.build_actor_input(
            niche='x', city='y', skip_closed=False, categories=None)
        self.assertNotIn('categoryFilterWords', payload)
        self.assertEqual(n, 1)   # website only

    def test_screen_rejects_an_automotive_place(self):
        # The exact shape that got through on 2026-10-11.
        row = dict(PLACE_ROW, title='CD Tire and Auto Repair',
                   categoryName='Auto repair shop',
                   categories=['Auto repair shop', 'Tire shop'])
        why = apify_maps.screen_place(
            row, categories=apify_maps.HVAC_CATEGORIES)
        self.assertIn('not an HVAC trade', why)

    def test_screen_keeps_a_real_hvac_place(self):
        self.assertEqual(
            apify_maps.screen_place(
                PLACE_ROW, categories=apify_maps.HVAC_CATEGORIES), '')

    def test_screen_accepts_a_secondary_hvac_category(self):
        # categoryName is the primary label; a place whose primary is
        # something odd but which also lists an HVAC category is kept.
        row = dict(PLACE_ROW, categoryName='Contractor',
                   categories=['Contractor', 'Heating contractor'])
        self.assertEqual(
            apify_maps.screen_place(
                row, categories=apify_maps.HVAC_CATEGORIES), '')

    def test_category_screen_is_off_when_no_categories_requested(self):
        row = dict(PLACE_ROW, categoryName='Auto repair shop',
                   categories=['Auto repair shop'])
        self.assertEqual(apify_maps.screen_place(row), '')

    def test_missing_category_data_does_not_reject(self):
        # Absence of a measurement is not evidence of a problem -- the
        # same rule the state screen follows.
        row = dict(PLACE_ROW, categoryName=None, categories=[])
        self.assertEqual(
            apify_maps.screen_place(
                row, categories=apify_maps.HVAC_CATEGORIES), '')


class CategoryEnumTests(TestCase):
    """categoryFilterWords is a STRICT enum, not free text.

    'HVAC contractor' in Title Case is rejected with HTTP 400
    invalid-input before the run starts -- it failed all 7 cities on
    2026-10-11. The validation is strict enough that a wrong value costs
    nothing, but a typo silently costs a whole batch of work.
    """

    def test_every_category_is_lowercase(self):
        for c in apify_maps.HVAC_CATEGORIES:
            self.assertEqual(c, c.lower(), f'{c!r} must be lowercase')

    def test_the_automotive_category_is_excluded(self):
        # The enum offers 'auto air conditioning service'. Including it
        # would re-import the tyre shops and transmission places that
        # the category filter exists to exclude.
        self.assertNotIn('auto air conditioning service',
                         apify_maps.HVAC_CATEGORIES)
        for c in apify_maps.HVAC_CATEGORIES:
            self.assertNotIn('auto', c)

    def test_the_core_trade_categories_are_present(self):
        for want in ('hvac contractor', 'air conditioning contractor',
                     'heating contractor'):
            self.assertIn(want, apify_maps.HVAC_CATEGORIES)

    def test_screen_accepts_the_lowercase_enum_values(self):
        # The post-fetch re-check compares case-insensitively, so it must
        # still pass with the enum spelling.
        row = dict(PLACE_ROW, categoryName='HVAC contractor')
        self.assertEqual(
            apify_maps.screen_place(
                row, categories=apify_maps.HVAC_CATEGORIES), '')

    def test_screen_rejects_automotive_ac(self):
        row = dict(PLACE_ROW, title='Joe Auto AC',
                   categoryName='Auto air conditioning service',
                   categories=['Auto air conditioning service'])
        self.assertIn('not an HVAC trade', apify_maps.screen_place(
            row, categories=apify_maps.HVAC_CATEGORIES))


class NotesPersistenceTests(TestCase):
    """import_leads must persist the mapper's notes and linkedin_url.

    It did not, and the loss was silent: a dict key the creator never
    reads looks exactly like a source that did not supply one. 532 leads
    were imported on 2026-10-11 with every Google-Maps detail discarded
    -- category, rating summary, unclaimed-profile flag, placeId and the
    Maps URL -- which only surfaced because an export column came out
    empty for every row.
    """

    def test_notes_survive_import(self):
        from outreach.pipeline import import_leads
        lead_dict = apify_maps.map_place_to_lead(
            PLACE_ROW, business_type='HVAC')
        self.assertTrue(lead_dict['notes'], 'mapper must build notes')
        import_leads([lead_dict], source='google_maps',
                     business_type_override='HVAC')
        saved = Lead.objects.get()
        self.assertEqual(saved.notes, lead_dict['notes'])
        self.assertIn('Maps:', saved.notes)
        self.assertIn('Category:', saved.notes)

    def test_linkedin_url_survives_import(self):
        from outreach.pipeline import import_leads
        import_leads([{
            'firm_name': 'Someone LLC',
            'linkedin_url': 'https://www.linkedin.com/company/someone',
            'notes': 'Title: Owner',
        }], source='apify', business_type_override='HVAC')
        saved = Lead.objects.get()
        self.assertEqual(saved.linkedin_url,
                         'https://www.linkedin.com/company/someone')
        self.assertEqual(saved.notes, 'Title: Owner')

    def test_missing_notes_is_blank_not_an_error(self):
        from outreach.pipeline import import_leads
        import_leads([{'firm_name': 'No Notes Co'}], source='google_maps',
                     business_type_override='HVAC')
        self.assertEqual(Lead.objects.get().notes, '')
