"""
The guard that stops a directory listing being stored as a lead's site.

WHY THIS EXISTS
---------------
``_matches_business`` asks "is this search hit ABOUT the right
business?". For a directory listing the answer is a confident yes — a
MapQuest page for Cool Air Supply carries Cool Air Supply's real name
AND real phone number, so the phone test (the strongest signal there)
actively confirms it.

So verification built to catch the WRONG BUSINESS cannot catch the wrong
KIND OF PAGE, and on a 25-lead HVAC sample run on 2026-10-10 that put 14
directory / data-broker / lead-aggregator URLs into ``Lead.website``,
plus 9 aggregator-owned social profiles.

That is worse than finding nothing. These leads are selected for having
NO website, so a false one silently costs the lead 4 scoring points and
invites outreach copy referring to a site the owner does not have.

Every URL below is a real one observed in that sample.
"""

from django.test import TestCase

from outreach.enricher import _domain_matches_name, _social_matches_name


class DomainMatchesNameTests(TestCase):

    def test_rejects_the_real_directory_hits(self):
        # Every one of these was stored as the business's own website.
        cases = [
            ('mapquest.com', 'Cool Air Supply'),
            ('mapquest.com', 'RS HVAC & Refrigeration'),
            ('www.zoominfo.com', 'Rathco A/C Heating Services'),
            ('us.centralindex.com', 'K&R Refrigeration and HVAC inc'),
            ('www.procore.com', 'JP Mechanical, LLC'),
            ('www.ibegin.com', 'Rep-Air Ac and Heating'),
            ('mykukun.com', 'Home Care Heating & Cooling'),
            ('americanhomequotes.com', "White's Heating & Air Conditioning"),
            ('austintexaspermitsearch.com', 'Juarez Brothers Maintanence'),
        ]
        for domain, firm in cases:
            ok, why = _domain_matches_name(domain, firm)
            self.assertFalse(ok, f'{domain} should not match {firm}: {why}')

    def test_known_limitation_a_trade_word_in_the_domain_fools_it(self):
        """The name guard alone is NOT sufficient. This is why the
        blocklist still exists.

        ``_GENERIC_NAME_TOKENS`` strips law/dental/medical boilerplate
        but carries no HVAC trade vocabulary, so 'contractor' and 'hvac'
        read as distinctive. A directory with one of those in its own
        domain therefore passes.

        Widening the generic list is NOT the fix: it is shared with
        ``_matches_business``, and stripping 'hvac'/'refrigeration'
        would leave "RS HVAC & Refrigeration" with only the 2-character
        token 'rs' and fail closed on a genuine match.

        So the two mechanisms are complementary, and a directory that
        slips past this one must be caught by DIRECTORY_DOMAINS.
        """
        ok, _ = _domain_matches_name(
            'contractorsjunction.com', 'B7 Contractor Services LLC')
        self.assertTrue(ok, 'documents the limitation; see the blocklist')

    def test_the_blocklist_covers_what_the_name_guard_misses(self):
        from outreach.enricher import _is_directory_domain
        self.assertTrue(_is_directory_domain('contractorsjunction.com'))
        self.assertTrue(_is_directory_domain('mapquest.com'))
        self.assertTrue(_is_directory_domain('zoominfo.com'))

    def test_accepts_a_genuine_business_domain(self):
        ok, why = _domain_matches_name(
            'eliteaustinac.com', 'Austin HVAC Repair Experts-HVAC')
        self.assertTrue(ok, why)
        self.assertIn('austin', why)

    def test_ignores_punctuation_in_the_domain(self):
        # elite-austin-ac.com and eliteaustinac.com are the same claim.
        ok, _ = _domain_matches_name(
            'elite-austin-ac.com', 'Austin HVAC Repair Experts')
        self.assertTrue(ok)

    def test_unrelated_domain_is_rejected_for_a_trade_named_business(self):
        # thecoolestservice.com was stored for a business literally named
        # "Hvac services". 'services' IS stripped as generic, leaving
        # only 'hvac', which that domain does not contain.
        ok, _ = _domain_matches_name('thecoolestservice.com', 'Hvac services')
        self.assertFalse(ok)

    def test_business_whose_every_token_is_too_short_fails_closed(self):
        # 'Air Services' -> 'services' is generic, 'air' is 3 chars and
        # the floor is 4, so nothing is left to match on and the guard
        # refuses rather than accepting anything.
        ok, why = _domain_matches_name('anything.com', 'Air Services')
        self.assertFalse(ok)
        self.assertIn('does not carry any name token', why)

    def test_business_with_only_generic_tokens_fails_closed(self):
        ok, why = _domain_matches_name('anything.com', 'The Law Firm LLC')
        self.assertFalse(ok)
        self.assertIn('no distinctive tokens', why)

    def test_short_tokens_do_not_match(self):
        # A 2-3 char token like 'jp' or 'b7' appears inside unrelated
        # domains constantly, so only tokens of 4+ chars count.
        ok, _ = _domain_matches_name('jpmorganchase.com', 'JP Mechanical')
        self.assertFalse(ok)


class SocialMatchesNameTests(TestCase):

    def test_rejects_an_aggregators_own_social_page(self):
        # The aggregator's page quotes the business's name and phone, so
        # _matches_business confirms it. The handle is what gives it away.
        cases = [
            ('https://www.facebook.com/americanhomequotes',
             "White's Heating & Air Conditioning"),
            ('https://facebook.com/mykukun', 'Home Care Heating & Cooling'),
            ('https://www.instagram.com/mykukun/',
             'Home Care Heating & Cooling'),
            ('https://www.instagram.com/procoretech', 'JP Mechanical, LLC'),
            ('https://www.facebook.com/thecoolestservice/',
             'Local Heating & Air Conditioning'),
        ]
        for url, firm in cases:
            ok, why = _social_matches_name(url, firm)
            self.assertFalse(ok, f'{url} should not match {firm}: {why}')

    def test_accepts_a_genuine_business_handle(self):
        cases = [
            ('https://www.facebook.com/CoolAirSupplySA/', 'Cool Air Supply'),
            ('https://www.facebook.com/homeacrepairsa/',
             'Affordable HVAC Repair San Antonio'),
            ('https://www.facebook.com/jpmechanicalhvacservices/',
             'JP Mechanical, LLC'),
            ('https://www.facebook.com/p/KR-refrigeration-HVAC-615739288',
             'K&R Refrigeration and HVAC inc'),
            ('https://www.instagram.com/eliteaustinacplumb/',
             'Austin HVAC Repair Experts-HVAC'),
        ]
        for url, firm in cases:
            ok, why = _social_matches_name(url, firm)
            self.assertTrue(ok, f'{url} should match {firm}: {why}')

    def test_only_the_handle_is_considered_not_the_host(self):
        # facebook.com is the host on every one of these, so a match must
        # come from the path or it would match everything.
        ok, _ = _social_matches_name(
            'https://www.facebook.com/', 'Facebook Heating')
        self.assertFalse(ok)
