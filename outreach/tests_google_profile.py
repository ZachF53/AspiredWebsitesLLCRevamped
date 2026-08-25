"""
Tests for the Places join and the two spend-ledger fixes.

The matching tests carry the most weight in this file. What this join
writes ends up in a sentence like "1,640 Google reviews at a 4.8-star
average" in the first line of a cold email — so a wrong match is not a
data-quality problem, it is a confident falsehood told to a stranger.
Every test below that asserts a REJECTION is protecting against that.
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from outreach import google_profile, verify
from outreach.models import Lead, OutreachSettings
from reporting.models import AISpendDay, ClaudeUsage


def make_lead(**kw):
    defaults = {
        'firm_name': 'Gamez Law Firm', 'email': 'a@gamezlaw.com',
        'city': 'San Antonio', 'state': 'TX', 'phone': '210-892-2222',
        'website': 'https://gamezlaw.com',
        'email_verification_status': verify.VALID,
        'source': 'apify',
    }
    defaults.update(kw)
    return Lead.objects.create(**defaults)


def place(**kw):
    d = {
        'displayName': {'text': 'Gamez Law Firm'},
        'formattedAddress': '1500 Main St, San Antonio, TX 78205, USA',
        'nationalPhoneNumber': '(210) 892-2222',
        'websiteUri': 'https://gamezlaw.com/',
        'rating': 4.8, 'userRatingCount': 1640,
        'businessStatus': 'OPERATIONAL',
    }
    d.update(kw)
    return d


class MatchingTests(TestCase):
    """A match must be positively established, never merely un-contradicted."""

    def test_same_website_domain_matches(self):
        ok, why = google_profile.match_reason(place(), make_lead())
        self.assertTrue(ok)
        self.assertIn('website domain', why)

    def test_different_website_domain_is_evidence_against(self):
        """Two real, different domains means two different businesses —
        not 'no information'."""
        ok, why = google_profile.match_reason(
            place(websiteUri='https://someotherfirm.com'), make_lead())
        self.assertFalse(ok)
        self.assertIn('website mismatch', why)

    def test_phone_matches_when_no_websites(self):
        ok, why = google_profile.match_reason(
            place(websiteUri=''), make_lead(website=''))
        self.assertTrue(ok)
        self.assertEqual(why, 'phone match')

    def test_different_phone_is_rejected(self):
        ok, why = google_profile.match_reason(
            place(websiteUri='', nationalPhoneNumber='(512) 555-0000'),
            make_lead(website=''))
        self.assertFalse(ok)
        self.assertEqual(why, 'phone mismatch')

    def test_name_and_city_together_match(self):
        ok, why = google_profile.match_reason(
            place(websiteUri='', nationalPhoneNumber=''),
            make_lead(website='', phone=''))
        self.assertTrue(ok)
        self.assertIn('city', why)

    def test_same_name_in_the_wrong_city_is_rejected(self):
        """'Smith Law' exists in every state. Without the city check this
        is the single likeliest way to attach a stranger's reviews to the
        wrong firm."""
        ok, why = google_profile.match_reason(
            place(websiteUri='', nationalPhoneNumber='',
                  formattedAddress='9 Peachtree St, Atlanta, GA 30303, USA'),
            make_lead(website='', phone=''))
        self.assertFalse(ok)
        self.assertIn('not in Places address', why)

    def test_a_different_firm_in_the_right_city_is_rejected(self):
        ok, why = google_profile.match_reason(
            place(displayName={'text': 'Rodriguez Immigration Attorneys'},
                  websiteUri='', nationalPhoneNumber=''),
            make_lead(website='', phone=''))
        self.assertFalse(ok)
        self.assertIn('below', why)

    def test_similarity_floor_is_stricter_than_the_enricher(self):
        """0.80 here vs 0.65 in the enricher. The enricher is choosing a
        URL to fetch; this is choosing a claim to make."""
        self.assertGreater(google_profile.NAME_SIMILARITY_FLOOR, 0.65)


class FetchTests(TestCase):

    def _search(self, places):
        return patch.object(google_profile, '_search', return_value=places)

    def test_a_match_copies_the_rating_across(self):
        lead = make_lead()
        with self._search([place()]):
            out = google_profile.fetch_profile(lead)

        lead.refresh_from_db()
        self.assertTrue(out['matched'])
        self.assertEqual(lead.google_review_count, 1640)
        self.assertEqual(lead.google_rating, Decimal('4.8'))
        self.assertTrue(lead.has_google_business)

    def test_a_miss_is_still_stamped(self):
        """Otherwise an unlisted firm is re-queried, and re-billed, on
        every run forever."""
        lead = make_lead()
        with self._search([]):
            google_profile.fetch_profile(lead)

        lead.refresh_from_db()
        self.assertIsNotNone(lead.google_profile_checked_at)
        self.assertEqual(lead.google_review_count, 0)

    def test_a_rejected_hit_records_why(self):
        lead = make_lead()
        with self._search([place(websiteUri='https://elsewhere.com')]):
            google_profile.fetch_profile(lead)

        lead.refresh_from_db()
        self.assertIn('rejected top hit', lead.google_profile_note)
        self.assertEqual(lead.google_review_count, 0)

    def test_an_api_failure_is_not_stamped(self):
        """An outage says nothing about this firm. Stamping it would skip
        the lead permanently on the strength of a network blip."""
        lead = make_lead()
        with patch.object(google_profile, '_search',
                          side_effect=google_profile.PlacesError('503')):
            out = google_profile.fetch_profile(lead)

        lead.refresh_from_db()
        self.assertIn('error', out)
        self.assertIsNone(lead.google_profile_checked_at)

    def test_closed_businesses_are_skipped(self):
        lead = make_lead()
        with self._search([place(businessStatus='CLOSED_PERMANENTLY')]):
            out = google_profile.fetch_profile(lead)
        self.assertFalse(out['matched'])

    def test_a_listing_with_no_reviews_writes_no_rating(self):
        lead = make_lead()
        with self._search([place(rating=None, userRatingCount=0)]):
            out = google_profile.fetch_profile(lead)

        lead.refresh_from_db()
        self.assertFalse(out['matched'])
        self.assertEqual(lead.google_review_count, 0)
        self.assertIn('no reviews yet', lead.google_profile_note)


class QualificationTests(TestCase):
    """A paid lookup is only ever spent on a lead worth contacting."""

    def test_unverified_leads_do_not_qualify(self):
        make_lead(email_verification_status=verify.ROLE)
        self.assertEqual(google_profile.qualified_leads(), [])

    def test_leads_held_for_review_do_not_qualify(self):
        make_lead(needs_review=True)
        self.assertEqual(google_profile.qualified_leads(), [])

    def test_inbound_leads_do_not_qualify(self):
        make_lead(source='contact_form')
        self.assertEqual(google_profile.qualified_leads(), [])

    def test_leads_without_an_email_do_not_qualify(self):
        make_lead(email='')
        self.assertEqual(google_profile.qualified_leads(), [])

    def test_already_checked_leads_do_not_qualify(self):
        make_lead(google_profile_checked_at=timezone.now())
        self.assertEqual(google_profile.qualified_leads(), [])

    def test_a_lead_that_already_has_reviews_does_not_qualify(self):
        """Nothing to buy — it can already open with a fact."""
        make_lead(google_review_count=200, google_rating=Decimal('4.7'))
        self.assertEqual(google_profile.qualified_leads(), [])

    def test_a_qualified_lead_does_qualify(self):
        lead = make_lead()
        self.assertEqual([l.pk for l in google_profile.qualified_leads()],
                         [lead.pk])

    def test_a_zero_cap_disables_lookups_entirely(self):
        cfg = OutreachSettings.load()
        cfg.places_max_lookups_per_day = 0
        cfg.save()
        allowed, why = google_profile.check_allowed()
        self.assertFalse(allowed)
        self.assertIn('disabled', why)

    def test_the_daily_cap_stops_the_backfill(self):
        cfg = OutreachSettings.load()
        cfg.places_max_lookups_per_day = 1
        cfg.save()
        for i in range(3):
            make_lead(email=f'a{i}@f.com', firm_name=f'Firm {i}')

        with patch.object(google_profile, '_search', return_value=[]) as s:
            google_profile.backfill(limit=10)

        self.assertEqual(s.call_count, 1)


class IcebreakerFactTests(TestCase):
    """What the join buys: a warm fact the opener can use."""

    def test_strong_rating_cites_stars_and_count(self):
        from outreach.icebreaker import warm_facts
        lead = make_lead(google_review_count=1640,
                         google_rating=Decimal('4.8'))
        keys = dict(warm_facts(lead))
        self.assertIn('reviews', keys)
        self.assertIn('4.8', keys['reviews'])

    def test_high_volume_but_middling_rating_cites_count_only(self):
        """A busy firm at 4.2 is worth opening with. Quoting the stars
        back at them would be a criticism wearing a statistic."""
        from outreach.icebreaker import warm_facts
        lead = make_lead(google_review_count=200,
                         google_rating=Decimal('4.2'))
        facts = dict(warm_facts(lead))
        self.assertIn('review_volume', facts)
        self.assertIn('200', facts['review_volume'])
        self.assertNotIn('4.2', facts['review_volume'])

    def test_a_poor_rating_produces_no_review_fact_at_all(self):
        from outreach.icebreaker import warm_facts
        lead = make_lead(google_review_count=300,
                         google_rating=Decimal('3.1'))
        facts = dict(warm_facts(lead))
        self.assertNotIn('reviews', facts)
        self.assertNotIn('review_volume', facts)

    def test_a_handful_of_reviews_is_not_an_opener(self):
        from outreach.icebreaker import warm_facts
        lead = make_lead(google_review_count=3, google_rating=Decimal('5.0'))
        self.assertNotIn('reviews', dict(warm_facts(lead)))


class SpendLedgerTests(TestCase):
    """The two fixes found while reviewing drafts on 2026-08-25."""

    def test_pipeline_calls_now_count_toward_the_daily_cap(self):
        """The bug: 77 icebreaker calls ran while spent_today() said $0
        because it only summed AIEmployeeRun rows."""
        from outreach import spend

        self.assertEqual(spend.spent_today(), Decimal('0'))
        ClaudeUsage.record('claude-sonnet-5', 1_000_000, 1_000_000)

        # $2/Mtok in + $10/Mtok out = $12
        self.assertEqual(spend.spent_today(), Decimal('12'))

    def test_the_cap_actually_refuses_once_pipeline_spend_exceeds_it(self):
        from outreach import spend

        cfg = OutreachSettings.load()
        cfg.daily_ai_spend_cap_usd = Decimal('5')
        cfg.save()
        ClaudeUsage.record('claude-sonnet-5', 1_000_000, 1_000_000)

        allowed, why = spend.check_spend_allowed()
        self.assertFalse(allowed)
        self.assertIn('Spend cap reached', why)

    def test_last_request_at_moves_on_every_call(self):
        """auto_now never fires through QuerySet.update(), so the widget
        showed a live system as last used days ago."""
        ClaudeUsage.record('claude-sonnet-5', 10, 10)
        row = ClaudeUsage.objects.get(model='claude-sonnet-5')
        first = row.last_request_at

        ClaudeUsage.record('claude-sonnet-5', 10, 10)
        row.refresh_from_db()
        self.assertGreater(row.last_request_at, first)

    def test_an_unpriced_model_does_not_crash_the_ledger(self):
        AISpendDay.record('some-future-model', 1000, 1000)
        self.assertEqual(AISpendDay.objects.get().cost_usd, Decimal('0'))

    def test_a_zero_token_call_is_not_recorded(self):
        AISpendDay.record('claude-sonnet-5', 0, 0)
        self.assertFalse(AISpendDay.objects.exists())

    def test_spend_accumulates_across_calls(self):
        for _ in range(3):
            ClaudeUsage.record('claude-sonnet-5', 500_000, 0)
        row = AISpendDay.objects.get()
        self.assertEqual(row.request_count, 3)
        self.assertEqual(row.cost_usd, Decimal('3'))  # 3 x 0.5Mtok x $2
