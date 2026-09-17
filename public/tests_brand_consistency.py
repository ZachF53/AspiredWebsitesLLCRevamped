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

from django.core.management import call_command
from django.test import TestCase, override_settings


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class SocialPlanSourceOfTruthTests(TestCase):
    """The digital-marketing page hardcoded plan names, prices and channel
    counts that contradicted the seeded ServiceTier rows, and hardcoded
    prices at all — which CLAUDE.md forbids.

    Sept 2026 repositioning: /services/digital-marketing/ now 301s to
    service_web_design (social media isn't sold as a standalone product
    to HVAC contractors) rather than rendering — the two tests that
    exercised its live rendering are retired below. The template file
    itself is untouched (nothing here was deleted, just unrouted), so
    the source-hygiene checks against it still run and still matter if
    the page is ever revived.
    """

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

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

@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class UnsupportedClaimTests(TestCase):
    """Claims the review found broader than the evidence behind them."""

    PAGES = [
        '/', '/services/web-design/', '/services/review-automation/',
        '/pricing/', '/portfolio/',
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

    # test_aspired_does_not_claim_to_verify_bar_compliance retired
    # Sept 2026 — the claim it guarded against lived on the law-firm
    # hub (/for-law-firms/), which now 301s to service_web_design
    # (Sept 2026 repositioning) rather than rendering. The claim can't
    # appear on a live page that doesn't render.


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


class DeadInternalLinkTests(TestCase):
    """
    Guards the bug class caught twice now: an internal link pointing at
    a URL that itself 301s elsewhere (public/urls.py's
    _RETIRED_TO_WEB_DESIGN). First in service_web_design.html's
    Specialist Builds section; then, in a fresh session, in
    location_city.html's value tiles and in three published Insights
    articles' bodies and CTA buttons. Both times it survived because
    nothing asserted "no live content links to a retired URL" — this
    does, across every place that kind of link can hide: City rows,
    Article bodies/CTAs, CaseStudy rows, and every public template.
    """

    @classmethod
    def setUpTestData(cls):
        call_command('seed_insights')
        call_command('seed_case_studies', verbosity=0)

    @staticmethod
    def _retired_names_and_paths():
        from django.urls import reverse

        from public.urls import _RETIRED_TO_WEB_DESIGN, urlpatterns

        names = {
            pattern.name for pattern in urlpatterns
            if getattr(pattern, 'callback', None) is _RETIRED_TO_WEB_DESIGN
        }
        paths = {reverse(f'public:{name}') for name in names}
        return names, paths

    def test_no_model_field_links_to_a_retired_url(self):
        from clients.models import CaseStudy
        from public.models import Article, City

        _, retired_paths = self._retired_names_and_paths()
        offenders = []

        city_fields = (
            'hero_heading_html', 'hero_lead', 'honesty_html',
            'secondary_html', 'cross_link_html', 'cta_body_html',
        )
        for city in City.objects.all():
            for field in city_fields:
                value = getattr(city, field) or ''
                for path in retired_paths:
                    if path in value:
                        offenders.append(
                            f'City({city.slug}).{field} -> {path}')

        for article in Article.objects.filter(status='published'):
            for path in retired_paths:
                if path in (article.body or ''):
                    offenders.append(
                        f'Article({article.slug}).body -> {path}')
            if article.related_url in retired_paths:
                offenders.append(
                    f'Article({article.slug}).related_url -> '
                    f'{article.related_url}')

        # CaseStudy has no HVAC rows yet — checked anyway so this stays
        # covered as HVAC case studies get published, without waiting
        # for a bug to show up in one first.
        cs_fields = (
            'summary', 'challenge', 'solution', 'results',
            'testimonial_quote',
        )
        for cs in CaseStudy.objects.all():
            label = cs.slug or f'pk={cs.pk}'
            for field in cs_fields:
                value = getattr(cs, field) or ''
                for path in retired_paths:
                    if path in value:
                        offenders.append(
                            f'CaseStudy({label}).{field} -> {path}')

        self.assertEqual(offenders, [], (
            'Internal link(s) to a retired (301) URL, found in database '
            f'content: {offenders}'))

    def test_no_public_template_links_to_a_retired_url(self):
        import pathlib

        retired_names, retired_paths = self._retired_names_and_paths()

        # Templates behind an unrouted, deliberately-retired view are
        # dead code kept on disk on purpose (see the _RETIRED_TO_WEB_DESIGN
        # comment in public/urls.py) — not a page a visitor or crawler
        # can reach, so its links aren't held to this standard. Derived
        # from the retired url names themselves — this codebase's
        # convention is one template per view, named after it — rather
        # than a hand-maintained list, so a page retired this way in the
        # future is skipped automatically instead of needing this test
        # updated too.
        skip_templates = {f'{name}.html' for name in retired_names}

        offenders = []
        for root in ('public/templates/public', 'core/templates'):
            for path in pathlib.Path(root).rglob('*.html'):
                if path.name in skip_templates:
                    continue
                source = path.read_text(encoding='utf-8', errors='replace')
                # The actual bug shape: {% url 'public:name' %} pointing
                # at a retired url name — the literal path never appears
                # in the template source, only after Django resolves it.
                for name in retired_names:
                    if (f"'public:{name}'" in source
                            or f'"public:{name}"' in source):
                        offenders.append(f'{path} -> public:{name}')
                # Belt and suspenders: a hardcoded path instead of a
                # {% url %} tag would still be a dead link.
                for target in retired_paths:
                    if f'href="{target}"' in source:
                        offenders.append(f'{path} -> {target}')

        self.assertEqual(offenders, [], (
            f'Live template(s) link to a retired (301) URL: {offenders}'))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class CustomWebsiteCostPriceConsistencyTests(TestCase):
    """
    "How Much Does a Custom Website Cost?" quoted retired pricing for
    months (~121 impressions/month in Search Console — one of the
    better-performing pages on the site) because Article.body is raw
    stored HTML (`{{ article.body|safe }}` in insight_detail.html) — it
    never passes through the Django template engine, so it can't pull a
    live price the way a real template can with
    `{% include "core/_price.html" %}`. The numbers are hardcoded in
    public/migrations/0009_custom_website_cost_live_pricing.py and
    seed_insights.py instead, so this asserts them against the live
    ServiceTier/AddonPricing rows on every test run — a price change in
    the pricing admin that isn't mirrored in both places fails here
    instead of the article quietly going stale again.
    """

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')
        call_command('seed_insights')

    def test_article_prices_match_the_live_tiers(self):
        from billing.pricing_models import AddonPricing, ServiceTier
        from public.models import Article

        body = Article.objects.get(
            slug='how-much-does-a-custom-website-cost').body

        tiers = {
            'build (upfront)': ServiceTier.objects.get(
                slug='hvac-build-full'),
            'build (installment)': ServiceTier.objects.get(
                slug='hvac-build-installment'),
            'Full Plan': ServiceTier.objects.get(slug='hvac-full-plan'),
            'Full Plan (paid in full)': ServiceTier.objects.get(
                slug='hvac-plan-paid-in-full'),
            'hosting + security': ServiceTier.objects.get(
                slug='hvac-hosting-security'),
        }
        for label, tier in tiers.items():
            price_string = f'${tier.price:,.0f}'
            with self.subTest(tier=label):
                self.assertIn(price_string, body, (
                    f'"{price_string}" ({label}) not found in the '
                    'how-much-does-a-custom-website-cost article body — '
                    'it may be quoting a stale price. Update the body in '
                    'public/migrations/'
                    '0009_custom_website_cost_live_pricing.py and '
                    'seed_insights.py to match ServiceTier.'))

        hourly = AddonPricing.objects.get(slug='addon-hourly')
        hourly_string = f'${hourly.price_min:,.0f}'
        self.assertIn(hourly_string, body, (
            f'"{hourly_string}" (out-of-scope hourly rate) not found in '
            'the article body.'))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class LawFirmCostArticleNeutralityTests(TestCase):
    """
    "How Much Does Law Firm Web Design Cost?" was rewritten Sept 2026 to
    strip Aspired out as a named vendor and stand as neutral market
    information — every price in it is now a market observation, not
    Aspired's price list (see public/migrations/
    0010_law_firm_cost_neutral_rewrite.py).

    This is the inverse of CustomWebsiteCostPriceConsistencyTests above:
    it asserts the retired self-quote figures are gone, AND that
    Aspired's current live prices don't appear either. If a future
    ServiceTier price change happens to land on one of this article's
    market-rate figures, that coincidence is worth a human look, not
    something that should pass silently.
    """

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')
        call_command('seed_insights')

    def _body(self):
        from public.models import Article
        return Article.objects.get(
            slug='how-much-does-law-firm-web-design-cost').body

    def test_retired_self_quote_prices_are_gone(self):
        body = self._body()
        retired = ('$4,500', '$299', '$150–$200', '$150/year')
        for figure in retired:
            with self.subTest(figure=figure):
                self.assertNotIn(figure, body, (
                    f'"{figure}" is a retired Aspired price still in the '
                    'law-firm-cost article body — it is meant to read '
                    'as market information now, not a stale self-quote.'))

    def test_no_longer_names_aspired_as_the_vendor(self):
        body = self._body()
        self.assertNotIn('Ours.', body, (
            'The article names Aspired as the vendor in the three-way '
            'comparison — it was rewritten to be a neutral market '
            'observation instead.'))

    def test_current_live_prices_do_not_coincidentally_appear(self):
        from billing.pricing_models import ServiceTier

        body = self._body()
        for slug in ('hvac-build-full', 'hvac-build-installment',
                     'hvac-full-plan', 'hvac-plan-paid-in-full',
                     'hvac-hosting-security'):
            tier = ServiceTier.objects.get(slug=slug)
            price_string = f'${tier.price:,.0f}'
            with self.subTest(slug=slug):
                self.assertNotIn(
                    price_string, body,
                    f'"{price_string}" (current {tier.name} price) '
                    'appears in the law-firm-cost article, which is '
                    "meant to read as neutral market information, not "
                    "Aspired's current price list. Confirm this is a "
                    'coincidental match with a market-rate figure and '
                    'not a reintroduced self-quote.')


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
