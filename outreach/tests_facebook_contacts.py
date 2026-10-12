"""
Tests for the Facebook -> email chain.

Every fixture below is a real shape observed on the 2026-10-10 HVAC run.
The two that matter most are the actor's split row format (which once
silently discarded a whole run's results) and the national name
collisions, which name-matching alone cannot separate.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings

from outreach import facebook_contacts as fb
from outreach.models import Lead


def make_lead(**kw):
    defaults = dict(
        firm_name='Cool Air Supply', city='San Antonio', state='TX',
        phone='(210) 630-9654', business_type='HVAC', website='',
        email='', facebook_url='')
    defaults.update(kw)
    return Lead.objects.create(**defaults)


class PageUrlTests(TestCase):

    def test_accepts_both_business_page_formats(self):
        self.assertTrue(fb.is_business_page_url(
            'https://www.facebook.com/CoolAirSupplySA/'))
        # The /p/Name-id format is the modern business page.
        self.assertTrue(fb.is_business_page_url(
            'https://www.facebook.com/p/RS-Hvac-and-Refrigeration-Llc-1007'))

    def test_rejects_things_that_are_not_pages(self):
        # Each of these was assigned to a lead on the real run.
        for url in (
            'https://m.facebook.com/public/Austin-Dash',          # directory
            'https://m.facebook.com/people/Elite-Air-Care/6156',  # directory
            'https://www.facebook.com/blanca.lund/photos/-stay',  # a post
            'https://www.facebook.com/guevaraair/posts/guevara',  # a post
            'https://www.facebook.com/groups/423360964477235',    # a group
        ):
            self.assertFalse(fb.is_business_page_url(url), url)

    def test_rejects_non_facebook_hosts(self):
        self.assertFalse(fb.is_business_page_url('https://instagram.com/x'))
        self.assertFalse(fb.is_business_page_url(''))


class ItemShapeTests(TestCase):
    """The actor emits two row shapes and only one carries 'url'."""

    def test_success_rows_put_the_url_in_pageurl(self):
        item = {'url': None,
                'pageUrl': 'https://www.facebook.com/thecomfortfit/',
                'facebookUrl': 'https://www.facebook.com/thecomfortfit/',
                'title': 'Comfort Fit', 'email': 'thecomfortfit@yahoo.com'}
        self.assertEqual(fb.item_page_url(item),
                         'https://www.facebook.com/thecomfortfit/')

    def test_failure_rows_put_the_url_in_url(self):
        item = {'url': 'https://www.facebook.com/austinanderson2200/',
                'pageUrl': None, 'error': 'page not found'}
        self.assertEqual(fb.item_page_url(item),
                         'https://www.facebook.com/austinanderson2200/')

    def test_reading_only_url_would_match_failures_only(self):
        # Guards the actual bug: joining on item['url'] matched the 4
        # error rows and silently dropped all 56 successes.
        success = {'url': None, 'pageUrl': 'https://www.facebook.com/a/'}
        self.assertEqual(success.get('url'), None)
        self.assertTrue(fb.item_page_url(success))

    def test_normalise_collapses_mobile_and_trailing_slash(self):
        a = fb.normalise_page_url('https://m.facebook.com/p/Austin-Major/')
        b = fb.normalise_page_url('https://www.facebook.com/p/Austin-Major')
        self.assertEqual(a, b)


class ClassifyTests(TestCase):

    def test_phone_match_is_confident(self):
        lead = make_lead()
        item = {'title': 'Cool Air Supply', 'phone': '(210) 630-9654',
                'email': 'coolairsupplysa@gmail.com'}
        verdict, email, _ = fb.classify_contact(item, lead)
        self.assertEqual(verdict, 'confident')
        self.assertEqual(email, 'coolairsupplysa@gmail.com')

    def test_name_match_without_phone_needs_review(self):
        lead = make_lead(firm_name='Thomson Heating and Air', phone='')
        item = {'title': "Thompson's Heating & Air", 'phone': None,
                'email': 'support@thompsonshvac.com'}
        verdict, _, reason = fb.classify_contact(item, lead)
        self.assertEqual(verdict, 'review')
        self.assertIn('name match only', reason)

    def test_rejects_a_federal_address(self):
        # 'Air Force Solutions' matched a US Air Force page.
        lead = make_lead(firm_name='Air Force Solutions')
        item = {'title': 'TSgt Kyler Gardipee - Air Force',
                'email': 'kyler.gardipee@us.af.mil'}
        verdict, _, reason = fb.classify_contact(item, lead)
        self.assertEqual(verdict, 'reject')
        self.assertIn('federal', reason)

    def test_rejects_foreign_domains(self):
        lead = make_lead(firm_name='Thermacool Heating')
        for addr in ('j.cretella@thermacool.co.uk',
                     'airborne@outlook.com.au'):
            verdict, _, _ = fb.classify_contact(
                {'title': 'Thermacool Ltd', 'email': addr}, lead)
            self.assertEqual(verdict, 'reject', addr)

    def test_rejects_a_same_name_business_in_another_state(self):
        # A real collision: an Austin TX lead matched a page for a
        # genuine 'Austin Heating & Cooling' in Michigan. The name token
        # matches perfectly, and the page lists no phone, so the state
        # reference is the ONLY thing separating them.
        lead = make_lead(firm_name='Austin Heating & Cooling',
                         city='Austin', phone='(512) 555-0100')
        item = {'title': 'Austin Heating & Cooling, Michigan | Dimondale MI',
                'phone': None,
                'email': 'info@austinheatingcooling.com'}
        verdict, _, reason = fb.classify_contact(item, lead)
        self.assertEqual(verdict, 'reject')
        self.assertIn('another state', reason)

    def test_rejects_an_unrelated_page_title(self):
        # No phone on either side, so the title is all there is. 'Texas
        # Heat A/C' vs a cheerleading page shares the token 'heat', so
        # this one survives into 'review' rather than being rejected --
        # the phone rule is what catches it when a phone exists, and a
        # human catches it when one does not.
        lead = make_lead(firm_name='Texas Heat A/C', phone='')
        item = {'title': 'South TX Heat Football Cheer',
                'phone': None, 'email': 'cheer@example.com'}
        verdict, _, _ = fb.classify_contact(item, lead)
        self.assertEqual(verdict, 'review')

    def test_truly_unrelated_title_is_rejected_outright(self):
        lead = make_lead(firm_name='Palomo Air', phone='')
        item = {'title': 'Landata Geo Services', 'phone': None,
                'email': 'x@landata.example'}
        verdict, _, reason = fb.classify_contact(item, lead)
        self.assertEqual(verdict, 'reject')
        self.assertIn('does not match', reason)

    def test_page_with_no_email_is_none_not_reject(self):
        lead = make_lead()
        verdict, email, _ = fb.classify_contact(
            {'title': 'Cool Air Supply', 'email': None}, lead)
        self.assertEqual(verdict, 'none')
        self.assertEqual(email, '')

    def test_phone_match_beats_a_mismatched_title(self):
        # The page is the right business trading under another name.
        lead = make_lead(firm_name='Affordable HVAC Repair San Antonio')
        item = {'title': 'Home AC Repair | San Antonio',
                'phone': '(210) 630-9654',
                'email': 'homeacrepair@gmail.com'}
        verdict, _, _ = fb.classify_contact(item, lead)
        self.assertEqual(verdict, 'confident')


class ApplyTests(TestCase):

    def test_stores_by_tier_and_flags_the_uncertain(self):
        a = make_lead(firm_name='Cool Air Supply',
                      facebook_url='https://www.facebook.com/CoolAirSupplySA/')
        b = make_lead(firm_name='Thomson Heating and Air', phone='',
                      facebook_url='https://www.facebook.com/thompsonshvac/')
        items = [
            {'pageUrl': 'https://www.facebook.com/CoolAirSupplySA/',
             'title': 'Cool Air Supply', 'phone': '(210) 630-9654',
             'email': 'coolairsupplysa@gmail.com'},
            {'pageUrl': 'https://www.facebook.com/thompsonshvac/',
             'title': "Thompson's Heating & Air",
             'email': 'support@thompsonshvac.com'},
        ]
        stats = fb.apply_contacts(items, [a, b])
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(stats['confident'], 1)
        self.assertEqual(stats['review'], 1)
        self.assertEqual(a.email, 'coolairsupplysa@gmail.com')
        self.assertFalse(a.needs_review)
        self.assertEqual(b.email, 'support@thompsonshvac.com')
        self.assertTrue(b.needs_review)

    def test_rejected_email_is_never_stored(self):
        lead = make_lead(firm_name='Air Force Solutions',
                         facebook_url='https://www.facebook.com/afsol/')
        stats = fb.apply_contacts(
            [{'pageUrl': 'https://www.facebook.com/afsol/',
              'title': 'Air Force', 'email': 'a@us.af.mil'}], [lead])
        lead.refresh_from_db()
        self.assertEqual(stats['reject'], 1)
        self.assertEqual(lead.email, '')

    def test_error_rows_are_counted_not_crashed_on(self):
        lead = make_lead(facebook_url='https://www.facebook.com/x/')
        stats = fb.apply_contacts(
            [{'url': 'https://www.facebook.com/x/', 'error': 'not found'}],
            [lead])
        self.assertEqual(stats['error'], 1)

    def test_matches_a_mobile_url_to_a_www_lead(self):
        lead = make_lead(
            facebook_url='https://m.facebook.com/p/Austin-Major-Air-100054/')
        stats = fb.apply_contacts(
            [{'pageUrl': 'https://www.facebook.com/p/Austin-Major-Air-100054',
              'title': 'Cool Air Supply', 'phone': '(210) 630-9654',
              'email': 'x@example.com'}], [lead])
        self.assertEqual(stats['confident'], 1)


class FetchGuardTests(TestCase):

    @override_settings(APIFY_TOKEN='')
    def test_missing_token_refuses_before_any_http(self):
        with self.assertRaises(fb.FacebookContactError):
            fb.fetch_page_contacts(['https://www.facebook.com/x/'])

    @override_settings(APIFY_TOKEN='tok')
    def test_empty_url_list_does_not_start_a_run(self):
        items, ledger = fb.fetch_page_contacts([])
        self.assertEqual(items, [])
        self.assertIsNone(ledger)

    @override_settings(APIFY_TOKEN='tok', APIFY_MAX_TOTAL_CHARGE_USD=0.5)
    def test_duplicate_urls_are_collapsed_before_billing(self):
        # The actor rejects duplicate inputs with HTTP 400, and a
        # duplicate would mean paying twice for one page anyway.
        seen = {}

        def fake_start(token, urls, timeout):
            seen['urls'] = urls
            return {'id': 'r1', 'status': 'SUCCEEDED',
                    'defaultDatasetId': 'd1', 'usageTotalUsd': 0.02}

        with patch.object(fb, '_start_and_wait', fake_start), \
                patch.object(fb, '_fetch_dataset', return_value=[]):
            _, ledger = fb.fetch_page_contacts([
                'https://www.facebook.com/a/',
                'https://www.facebook.com/a/',
                'https://www.facebook.com/b/'])
        self.assertEqual(len(seen['urls']), 2)
        self.assertEqual(ledger.results_requested, 2)

    @override_settings(APIFY_TOKEN='tok', APIFY_MAX_TOTAL_CHARGE_USD=0.5)
    def test_run_is_recorded_against_the_shared_apify_budget(self):
        from outreach.models import ApifyRun
        with patch.object(fb, '_start_and_wait', return_value={
                    'id': 'r1', 'status': 'SUCCEEDED',
                    'defaultDatasetId': 'd1', 'usageTotalUsd': 0.03}), \
                patch.object(fb, '_fetch_dataset', return_value=[]):
            fb.fetch_page_contacts(['https://www.facebook.com/a/'])
        run = ApifyRun.objects.latest('started_at')
        self.assertEqual(run.status, 'succeeded')
        self.assertTrue(run.billable)


class PhoneMismatchTests(TestCase):
    """A differing phone is evidence AGAINST, not absent evidence.

    This is what separates a trade-word coincidence from a real match.
    "Texas Heat A/C" scores a name hit against "South TX Heat Football
    Cheer" on the token 'heat' alone; only the phone distinguishes them.
    """

    def test_differing_phones_reject_even_with_a_name_token_hit(self):
        lead = make_lead(firm_name='Texas Heat A/C', phone='(210) 555-1111')
        verdict, _, reason = fb.classify_contact(
            {'title': 'South TX Heat Football Cheer',
             'phone': '(210) 999-0000', 'email': 'cheer@example.com'}, lead)
        self.assertEqual(verdict, 'reject')
        self.assertIn('differs', reason)

    def test_absent_page_phone_still_allows_a_name_review(self):
        # Missing is not mismatched. Most pages carry no phone, and
        # rejecting those would discard the majority of real matches.
        lead = make_lead(firm_name='Thomson Heating and Air',
                         phone='(210) 555-1111')
        verdict, _, _ = fb.classify_contact(
            {'title': "Thompson's Heating & Air", 'phone': None,
             'email': 'support@thompsonshvac.com'}, lead)
        self.assertEqual(verdict, 'review')

    def test_absent_lead_phone_still_allows_a_name_review(self):
        lead = make_lead(firm_name='Thomson Heating and Air', phone='')
        verdict, _, _ = fb.classify_contact(
            {'title': "Thompson's Heating & Air", 'phone': '(210) 999-0000',
             'email': 'support@thompsonshvac.com'}, lead)
        self.assertEqual(verdict, 'review')


class FindPagesTests(TestCase):
    """Page discovery, including the collision rule."""

    def _hit(self, url, title, desc=''):
        return {'url': url, 'title': title, 'description': desc}

    @override_settings(BRAVE_SEARCH_API_KEY='k')
    def test_two_leads_claiming_one_page_clears_both(self):
        # Observed live: facebook.com/austinairconditioning was matched
        # to two different businesses. Two businesses cannot share one
        # page, so at least one is wrong and nothing says which.
        a = make_lead(firm_name='Austin Air Conditioning Experts',
                      city='Austin', phone='(737) 910-4717')
        b = make_lead(firm_name='Austin Air Conditioning Pros',
                      city='Austin', phone='(512) 645-1120')
        url = 'https://www.facebook.com/austinairconditioning/'
        with patch('outreach.enricher._brave_search',
                          return_value=[self._hit(url, 'Austin Air '
                                                  'Conditioning')]), \
                patch('time.sleep'):
            found, checked = fb.find_facebook_pages([a, b])
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.facebook_url, '')
        self.assertEqual(b.facebook_url, '')
        # And the count must not claim a find it then threw away.
        self.assertEqual(found, 0)
        self.assertEqual(checked, 2)

    @override_settings(BRAVE_SEARCH_API_KEY='k')
    def test_stores_a_clean_match(self):
        lead = make_lead()
        url = 'https://www.facebook.com/CoolAirSupplySA/'
        with patch('outreach.enricher._brave_search', return_value=[
                    self._hit(url, 'Cool Air Supply',
                              'Call (210) 630-9654')]), \
                patch('time.sleep'):
            found, _ = fb.find_facebook_pages([lead])
        lead.refresh_from_db()
        self.assertEqual(found, 1)
        self.assertEqual(lead.facebook_url, url)

    @override_settings(BRAVE_SEARCH_API_KEY='k')
    def test_skips_leads_that_already_have_a_page(self):
        lead = make_lead(facebook_url='https://www.facebook.com/x/')
        with patch('outreach.enricher._brave_search') as search:
            found, checked = fb.find_facebook_pages([lead])
        search.assert_not_called()
        self.assertEqual((found, checked), (0, 0))

    @override_settings(BRAVE_SEARCH_API_KEY='')
    def test_missing_brave_key_is_a_clear_error(self):
        with self.assertRaises(fb.FacebookContactError):
            fb.find_facebook_pages([make_lead()])


class BatchingTests(TestCase):
    """The actor silently truncates a large input list.

    Measured 2026-10-12: 194 pages submitted, run finished in 11
    seconds, 54 rows returned, no error reported anywhere. Nothing in
    the response distinguishes "no contact info on this page" from
    "this page was never looked at", so the truncation reads downstream
    as a poor hit rate rather than a failure.
    """

    @override_settings(APIFY_TOKEN='tok', APIFY_MAX_TOTAL_CHARGE_USD=0.5)
    def test_large_input_is_split_into_batches(self):
        urls = [f'https://www.facebook.com/page{i}/' for i in range(120)]
        sizes = []

        def fake_start(token, batch, timeout):
            sizes.append(len(batch))
            return {'id': f'r{len(sizes)}', 'status': 'SUCCEEDED',
                    'defaultDatasetId': 'd1', 'usageTotalUsd': 0.01}

        with patch.object(fb, '_start_and_wait', fake_start), \
                patch.object(fb, '_fetch_dataset', return_value=[]):
            fb.fetch_page_contacts(urls)

        self.assertEqual(sizes, [50, 50, 20])
        self.assertTrue(all(s <= fb.FB_MAX_PAGES_PER_RUN for s in sizes))

    @override_settings(APIFY_TOKEN='tok', APIFY_MAX_TOTAL_CHARGE_USD=0.5)
    def test_rows_from_every_batch_are_returned(self):
        urls = [f'https://www.facebook.com/page{i}/' for i in range(75)]
        calls = {'n': 0}

        def fake_fetch(token, ds):
            calls['n'] += 1
            return [{'pageUrl': f'https://www.facebook.com/b{calls["n"]}/',
                     'title': 'x', 'email': 'a@b.com'}]

        with patch.object(fb, '_start_and_wait', return_value={
                    'id': 'r', 'status': 'SUCCEEDED',
                    'defaultDatasetId': 'd', 'usageTotalUsd': 0.01}), \
                patch.object(fb, '_fetch_dataset', fake_fetch):
            items, _ = fb.fetch_page_contacts(urls)
        # Two batches, one row each -- not just the last batch's rows.
        self.assertEqual(len(items), 2)

    @override_settings(APIFY_TOKEN='tok', APIFY_MAX_TOTAL_CHARGE_USD=0.5)
    def test_small_input_still_runs_as_one_batch(self):
        sizes = []

        def fake_start(token, batch, timeout):
            sizes.append(len(batch))
            return {'id': 'r', 'status': 'SUCCEEDED',
                    'defaultDatasetId': 'd', 'usageTotalUsd': 0.01}

        with patch.object(fb, '_start_and_wait', fake_start), \
                patch.object(fb, '_fetch_dataset', return_value=[]):
            fb.fetch_page_contacts(
                [f'https://www.facebook.com/p{i}/' for i in range(10)])
        self.assertEqual(sizes, [10])
