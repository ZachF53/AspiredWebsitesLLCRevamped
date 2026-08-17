"""
Brand consistency regression tests.

Each test corresponds to a defect found in the fresh-buyer review recorded
in `BRAND_REMEDIATION_HANDOFF.md`. They exist to stop a corrected public
claim silently coming back — through a template edit, a reseed, or a copy
change made without checking the database.

These tests assert the *absence* of unsupported claims and the *presence*
of database-driven values. They deliberately do not assert any business
fact that `docs/brand_fact_matrix.md` still lists as PENDING.
"""

from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase, override_settings


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class SocialPlanSourceOfTruthTests(TestCase):
    """The digital-marketing page hardcoded plan names, prices and channel
    counts that contradicted the seeded ServiceTier rows, and hardcoded
    prices at all — which CLAUDE.md forbids."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_social_plans_render_from_active_service_tiers(self):
        from billing.pricing_models import ServiceTier

        html = self.client.get('/services/digital-marketing/').content.decode()
        tiers = ServiceTier.get_active('social_media')
        self.assertTrue(tiers.exists(), 'seed_pricing produced no social tiers')

        for tier in tiers:
            with self.subTest(slug=tier.slug):
                self.assertIn(tier.name, html)
                # Every entitlement shown comes from a TierFeature row.
                for feature in tier.features.all():
                    self.assertIn(feature.text, html)

    TEMPLATE = 'public/templates/public/service_digital_marketing.html'

    def _template_source(self):
        with open(self.TEMPLATE, encoding='utf-8') as handle:
            return handle.read()

    def test_page_does_not_hardcode_social_prices(self):
        """A price in the template — including one inside JSON-LD — is a
        second source of truth the pricing admin cannot update."""
        source = self._template_source()
        found = [literal for literal in ('$399', '$699', '$999')
                 if literal in source]
        self.assertEqual(found, [], (
            f'{self.TEMPLATE} hardcodes {found}; render from ServiceTier '
            'instead.'))

    def test_page_does_not_contradict_database_channel_counts(self):
        """It advertised one channel on Basic and two on Standard while
        the database said two and three."""
        source = self._template_source().lower()
        found = [phrase for phrase in
                 ('one channel,', 'two channels,', 'three+ channels')
                 if phrase in source]
        self.assertEqual(found, [], (
            f'{self.TEMPLATE} hardcodes entitlements {found}; these come '
            'from TierFeature rows.'))

    def test_price_change_flows_through_to_the_service_page(self):
        """Proves the page really is database-driven rather than
        coincidentally matching the seed."""
        from billing.pricing_models import ServiceTier

        tier = ServiceTier.get_active('social_media').first()
        tier.price = Decimal('1234.00')
        tier.save(update_fields=['price'])

        html = self.client.get('/services/digital-marketing/').content.decode()
        self.assertIn('1,234', html)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class UnsupportedClaimTests(TestCase):
    """Claims the review found broader than the evidence behind them."""

    PAGES = [
        '/', '/services/web-design/', '/services/seo/',
        '/services/digital-marketing/', '/for-law-firms/',
        '/services/seo/law-firm-seo/',
        '/services/web-design/law-firm-web-design/',
    ]

    def _html(self, path):
        response = self.client.get(path)
        self.assertEqual(
            response.status_code, 200,
            f'{path} returned {response.status_code}; the scan must not '
            'silently skip a page that could still carry the claim.')
        return response.content.decode().lower()

    def test_no_fortune_500_comparison(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                self.assertNotIn('fortune 500', self._html(path))

    def test_no_fear_or_disparagement_copy(self):
        banned = ('weaponize', 'donations to meta', 'money set on fire',
                  'bar complaint waiting to happen')
        for path in self.PAGES:
            html = self._html(path)
            for phrase in banned:
                with self.subTest(path=path, phrase=phrase):
                    self.assertNotIn(phrase, html)

    def test_intake_is_described_precisely(self):
        """"Privileged intake" overstates the legal status of a
        pre-engagement website submission."""
        for path in self.PAGES:
            with self.subTest(path=path):
                self.assertNotIn('privileged intake', self._html(path))

    def test_aspired_does_not_claim_to_verify_bar_compliance(self):
        """The law-firm hub claimed Aspired verifies bar advertising
        compliance before launch, contradicting the service page and
        overstating what Aspired can be responsible for."""
        html = self._html('/for-law-firms/')
        self.assertNotIn('we verify before anything goes live', html)
        self.assertNotIn('compliant with state bar advertising guidelines',
                         html)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class PolicyConsistencyTests(TestCase):
    """Terms, the refund policy and the signed contract used to disagree
    about jurisdiction and refunds. Owner decisions (2026-08-16): governing
    law is Georgia, and the 30-day build guarantee is real and advertised.
    """

    def test_governing_law_is_georgia_everywhere(self):
        for path in ('/terms/', '/refund-policy/'):
            html = self.client.get(path).content.decode().lower()
            with self.subTest(path=path):
                self.assertIn('georgia', html)
                self.assertNotIn('state of texas', html)
                self.assertNotIn('san antonio, texas', html)

    def test_contract_templates_agree_on_georgia(self):
        from clients import contract_template

        source = open(contract_template.__file__, encoding='utf-8').read()
        self.assertIn('State of Georgia', source)
        self.assertNotIn('State of Texas', source)

    def test_build_guarantee_is_stated_consistently(self):
        """The pricing badge, Terms and the refund policy must describe
        the same guarantee the contract grants."""
        from clients import contract_template

        contract = open(
            contract_template.__file__, encoding='utf-8').read().lower()
        self.assertIn('30-day money-back guarantee', contract)

        for path in ('/pricing/', '/terms/', '/refund-policy/'):
            html = self.client.get(path).content.decode().lower()
            with self.subTest(path=path):
                self.assertIn('30', html)
                self.assertIn('money-back guarantee', html)

    def test_refund_policy_does_not_contradict_the_guarantee(self):
        """It previously said the deposit was refundable for only 7 days
        while the contract granted 30 days from signing."""
        html = self.client.get('/refund-policy/').content.decode().lower()
        guarantee = html.find('30-day money-back guarantee')
        seven_day = html.find('7 days from payment')
        self.assertNotEqual(guarantee, -1)
        self.assertTrue(
            seven_day == -1 or guarantee < seven_day,
            'The 30-day guarantee must be stated before the milestone '
            'treatment it takes precedence over.')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class LocationStatementTests(TestCase):
    """Approved 2026-08-16: "Based in Georgia. Serving clients nationwide."

    Before this, the site gave three different answers to "where is this
    company?" — the About page said San Antonio and Atlanta, two meta
    descriptions said the same, and the footer and structured data said
    Warner Robins, Georgia.
    """

    def test_no_page_claims_a_texas_or_atlanta_base(self):
        """San Antonio and Atlanta are service markets, not bases."""
        for path in ('/', '/about/', '/contact/'):
            html = self.client.get(path).content.decode().lower()
            with self.subTest(path=path):
                self.assertNotIn('based in san antonio', html)
                self.assertNotIn('based in san antonio, tx', html)
                self.assertNotIn('san antonio and atlanta', html)

    def test_about_page_states_the_approved_location(self):
        from core.site_facts import LOCATION_STATEMENT

        html = self.client.get('/about/').content.decode()
        self.assertIn(LOCATION_STATEMENT, html)

    def test_meta_descriptions_use_the_approved_wording(self):
        for path in ('/about/', '/contact/'):
            html = self.client.get(path).content.decode().lower()
            with self.subTest(path=path):
                self.assertIn('georgia', html)
                self.assertNotIn('based in san antonio and atlanta', html)

    def test_city_and_state_statements_do_not_conflict(self):
        """"Warner Robins, GA" in the footer is a more specific true
        statement than "Based in Georgia" — both may stand. What must
        never appear is a base outside Georgia."""
        html = self.client.get('/about/').content.decode()
        self.assertIn('Georgia', html)
        self.assertNotIn('Based in San Antonio', html)

    def test_credential_pills_do_not_contradict_the_location_statement(self):
        """The About sidebar listed "San Antonio, TX" and "Atlanta, GA"
        as location pills, two paragraphs above the approved statement
        saying the business is based in Georgia."""
        html = self.client.get('/about/').content.decode()
        self.assertNotIn('San Antonio, TX', html)
        self.assertNotIn('Atlanta, GA', html)
        self.assertIn('Based in Georgia', html)

    def test_law_firm_metadata_does_not_promise_bar_compliance(self):
        html = self.client.get('/for-law-firms/').content.decode().lower()
        self.assertNotIn('state bar compliant', html)

    def test_the_contact_page_states_the_approved_location(self):
        """The contact page listed "San Antonio, TX · Atlanta, GA" under a
        "Locations" heading -- the most literal possible contradiction of
        the approved statement, on the page a prospect checks precisely to
        find out where the company is."""
        from core.site_facts import LOCATION_STATEMENT

        html = self.client.get('/contact/').content.decode()
        self.assertIn(LOCATION_STATEMENT, html)
        self.assertNotIn('San Antonio, TX', html)
        self.assertNotIn('Atlanta, GA', html)


class ContractLocationTests(TestCase):
    """The contract is the highest-stakes place a location claim appears.

    Both generated contracts carried the header "Aspired Websites LLC --
    San Antonio, TX & Atlanta, GA". A wrong location on a marketing page
    is a credibility problem; the same wrong location on a document the
    client signs is a term of an executed agreement. The public-page
    tests above never looked at contract text, so this drift survived
    every previous sweep.
    """

    def _tier(self, slug='website-essential'):
        from billing.pricing_models import ServiceTier

        return ServiceTier.objects.create(
            slug=slug, name='Essential Website Build', price=2500,
            pages_included=5, practice_areas_included=2, timeline_weeks=3,
        )

    class _Client:
        contact_name = 'Test Person'
        firm_name = 'Test Firm LLC'

    def test_build_contract_header_uses_the_approved_location(self):
        from clients.contract_template import generate_contract_text
        from core.site_facts import LOCATION_STATEMENT

        self._tier()
        text = generate_contract_text(self._Client(), 'website-essential')
        self.assertIn(LOCATION_STATEMENT, text)
        self.assertNotIn('San Antonio', text)
        self.assertNotIn('Atlanta', text)

    def test_combined_contract_header_uses_the_approved_location(self):
        from clients.contract_template import generate_combined_contract_text
        from core.site_facts import LOCATION_STATEMENT

        tier = self._tier()
        text = generate_combined_contract_text(
            self._Client(), [{'service_type': 'build', 'tier': tier}])
        self.assertIn(LOCATION_STATEMENT, text)
        self.assertNotIn('San Antonio', text)
        self.assertNotIn('Atlanta', text)

    def test_contract_governing_law_matches_the_approved_state(self):
        """Georgia, approved 2026-08-16 and already in both templates."""
        from clients.contract_template import generate_contract_text
        from core.site_facts import GOVERNING_LAW_STATE

        self._tier()
        text = generate_contract_text(self._Client(), 'website-essential')
        self.assertIn(f'State of {GOVERNING_LAW_STATE}', text)


class TemplateCommentHygieneTests(TestCase):
    """CLAUDE.md hard rule: `{# ... #}` is single-line only. A wrapped
    comment is not treated as a comment and its text leaks into the
    rendered page. This has shipped to the user repeatedly."""

    def test_no_multiline_hash_comments_in_templates(self):
        import pathlib

        offenders = []
        for path in pathlib.Path('.').rglob('*.html'):
            if 'node_modules' in path.parts or 'myvenv' in path.parts:
                continue
            for number, line in enumerate(
                    path.read_text(encoding='utf-8',
                                   errors='replace').splitlines(), 1):
                if '{#' in line and '#}' not in line:
                    offenders.append(f'{path}:{number}')
        self.assertEqual(offenders, [], (
            'Multiline {# #} comments leak into rendered HTML. Convert '
            'them to {% comment %}...{% endcomment %}.'))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class FounderPortraitTests(TestCase):
    """Owner approved publishing the portrait on 2026-08-16. It replaced
    an initials placeholder, so it is real content, not decoration."""

    def test_portrait_renders_with_accessible_alt_text(self):
        html = self.client.get('/about/').content.decode()
        self.assertIn('founder-zachery-long.jpg', html)
        self.assertIn(
            'alt="Zachery Long, founder of Aspired Websites LLC"', html)

    def test_portrait_is_not_hidden_from_assistive_technology(self):
        """The initials placeholder was aria-hidden because it carried no
        information. A real photograph of the founder does."""
        html = self.client.get('/about/').content.decode()
        block = html[html.find('bio-photo'):html.find('bio-name')]
        self.assertNotIn('aria-hidden', block)

    def test_initials_placeholder_is_gone(self):
        html = self.client.get('/about/').content.decode()
        self.assertNotIn('bio-photo__initials', html)

    def test_portrait_reserves_its_space(self):
        """Without width/height the bio text jumps when the image lands."""
        html = self.client.get('/about/').content.decode()
        self.assertIn('width="400" height="500"', html)

    def test_portrait_asset_exists_and_is_reasonably_sized(self):
        import pathlib

        path = pathlib.Path('core/static/images/founder-zachery-long.jpg')
        self.assertTrue(path.exists())
        self.assertLess(
            path.stat().st_size, 120_000,
            'The About page should not ship a heavyweight portrait.')
