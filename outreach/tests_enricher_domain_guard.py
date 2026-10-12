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

    def test_trade_word_directories_are_now_rejected(self):
        """The 2026-10-11 fix. Each of these was stored as a lead's own
        website by the previous version, which rejected 0 of 30."""
        for domain, firm in (
            ('autorepairscore.com', 'Santiago Auto Repair'),
            ('simpletire.com', 'CPS Tire & Muffler Shop'),
            ('automd.com', 'Budget Auto Repair'),
            ('mysanantonio.com', 'San Antonio AC Repair'),
            ('tireshopnearme.net', 'Menchaca Tire Shop'),
            ('maps.roadtrippers.com', 'On The Road Again'),
            ('heating-equipment-cleaning.com', 'Top Shot AC and Heating'),
        ):
            ok, why = _domain_matches_name(domain, firm)
            self.assertFalse(ok, f'{domain} should not match {firm}: {why}')

    def test_superseded_limitation_trade_word_in_the_domain(self):
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
        # Superseded: 'contractor' is now in _DOMAIN_GENERIC_TOKENS, so
        # this is rejected rather than merely blocklisted.
        ok, _ = _domain_matches_name(
            'contractorsjunction.com', 'B7 Contractor Services LLC')
        self.assertFalse(ok)

    def test_the_blocklist_covers_what_the_name_guard_misses(self):
        from outreach.enricher import _is_directory_domain
        self.assertTrue(_is_directory_domain('contractorsjunction.com'))
        self.assertTrue(_is_directory_domain('mapquest.com'))
        self.assertTrue(_is_directory_domain('zoominfo.com'))

    def test_accepts_a_genuine_business_domain(self):
        # The surname is what identifies the business.
        ok, why = _domain_matches_name(
            'whitehvac.com', "White's Heating & Air Conditioning")
        self.assertTrue(ok, why)
        self.assertIn('white', why)

    def test_ignores_punctuation_in_the_domain(self):
        # blue-sky-hvac.com and blueskyhvac.com are the same claim.
        ok, _ = _domain_matches_name(
            'blue-sky-heating.com', 'Blue Sky Heating and Air')
        self.assertTrue(ok)

    def test_the_cost_of_strictness_is_real_false_negatives(self):
        """Documents what the 2026-10-11 tightening gives up.

        Both of these are GENUINE business domains that are now
        rejected, because every token of the business name is a trade
        word, a qualifier or a city:

            eliteaustinac.com   <- "Austin HVAC Repair Experts"
            thompsonshvac.com   <- "Thomson Heating and Air" (and the
                                   spelling differs besides)

        That is the accepted trade: before tightening, the guard
        rejected 0 of 30 directory domains, including a NEWS SITE
        (mysanantonio.com) stored as a lead's own website. For leads
        sourced precisely BECAUSE they have no website, a guessed one
        is pure downside -- losing a real site costs some enrichment
        detail, keeping a false one corrupts the score and the pitch.
        """
        for domain, firm in (
            ('eliteaustinac.com', 'Austin HVAC Repair Experts'),
            ('thompsonshvac.com', 'Thomson Heating and Air'),
        ):
            ok, _ = _domain_matches_name(domain, firm)
            self.assertFalse(ok, f'{domain} is a known false negative')

    def test_unrelated_domain_is_rejected_for_a_trade_named_business(self):
        # thecoolestservice.com was stored for a business literally named
        # "Hvac services". 'services' IS stripped as generic, leaving
        # only 'hvac', which that domain does not contain.
        ok, _ = _domain_matches_name('thecoolestservice.com', 'Hvac services')
        self.assertFalse(ok)

    def test_business_whose_every_token_is_generic_fails_closed(self):
        # 'Air Services' -> both words are trade vocabulary, so nothing
        # identifying is left and the guard refuses rather than
        # accepting whatever domain happened to rank first.
        ok, why = _domain_matches_name('anything.com', 'Air Services')
        self.assertFalse(ok)
        self.assertIn('entirely generic', why)

    def test_short_distinctive_token_still_cannot_match(self):
        # A surviving token under 4 chars is too collision-prone to
        # confirm a domain on.
        ok, why = _domain_matches_name('b7cps.com', 'B7 Cooling')
        self.assertFalse(ok)

    def test_business_with_only_generic_tokens_fails_closed(self):
        ok, why = _domain_matches_name('anything.com', 'The Law Firm LLC')
        self.assertFalse(ok)
        self.assertIn('generic', why)

    def test_business_of_only_trade_words_fails_closed(self):
        ok, why = _domain_matches_name('anything.com', 'Budget Auto Repair')
        self.assertFalse(ok)
        self.assertIn('entirely generic', why)

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
