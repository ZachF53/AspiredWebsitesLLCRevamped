"""
The review system is a standalone product (Oct 2026).

Automated review generation used to be reachable only by buying the Full
Plan, which meant only by commissioning a website build. That capped the
product at website clients and the site said so out loud — the service
page's hero read "Not sold on its own" and its FAQ answered "Is review
automation sold on its own?" with "No."

These tests pin the two things that repositioning has to keep true:

  1. The offer is visible and buyable. The price shows on /pricing/, on
     /services/review-automation/ and on the home page, the service page
     links to a real self-checkout URL, and checkout actually accepts
     the tier instead of bouncing it to the schedule-a-call page.

  2. Nothing anywhere still tells a visitor they need the plan or a
     build to get it. That's the regression that matters: a single
     surviving "included in the Full Plan, not sold separately"
     sentence re-closes the door this work opened, and it would be easy
     to reintroduce while editing nearby copy.

Every price assertion reads the live ServiceTier row rather than the
string "$97" (CLAUDE.md: never hardcode a price), so a price change in
the pricing admin moves these tests with it.
"""

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.pricing_models import ServiceTier


SLUG = 'hvac-review-system'

# Pages that sell or describe the review system and so must never claim
# it is plan-only.
SELLING_PAGES = [
    '/',
    '/pricing/',
    '/services/review-automation/',
    '/services/hosting-maintenance/',
]

# Phrasings that re-close the door. Matched case-insensitively against
# the rendered HTML of every page above.
#
# These are only a defect when they are talking about the REVIEW SYSTEM.
# Several are legitimately true of other things and must not be flagged:
# the home page says SEO fundamentals are "built in, not sold separately"
# (true — SEO is not a product here), and the web-design page says the
# same. So each hit is checked for "review" nearby rather than being
# failed on sight. See _mentions_reviews_near.
PLAN_ONLY_PHRASES = [
    'not sold on its own',
    'not sold separately',
    'not available on its own',
    'only as part of',
    'requires the full plan',
    'no review system',
]

# How far either side of a flagged phrase to look for "review" before
# treating the phrase as a claim about the review system. Wide enough to
# cross a sentence boundary, narrow enough not to reach an unrelated
# paragraph.
CONTEXT_CHARS = 180


def _review_claims(html, phrase):
    """Occurrences of `phrase` that are talking about reviews.

    Returns the surrounding snippets, so a failure message shows the
    actual sentence rather than just asserting something is wrong
    somewhere in 40KB of HTML.
    """
    hits = []
    start = 0
    while True:
        index = html.find(phrase, start)
        if index == -1:
            return hits
        window = html[max(0, index - CONTEXT_CHARS):
                      index + len(phrase) + CONTEXT_CHARS]
        if 'review' in window:
            hits.append(' '.join(window.split()))
        start = index + len(phrase)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ReviewSystemTierTests(TestCase):
    """The ServiceTier row itself — shape, not just existence."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_tier_exists_and_is_a_public_monthly_subscription(self):
        tier = ServiceTier.objects.get(slug=SLUG)
        self.assertTrue(tier.is_active)
        self.assertTrue(tier.is_public)
        self.assertTrue(tier.is_recurring)
        self.assertEqual(tier.billing_interval, 'month')

    def test_tier_is_an_addon_not_a_maintenance_plan(self):
        """category='maintenance' would put it in the portal's plan
        chooser, which is a SWITCH surface — a Full Plan client could
        pick reviews-only and silently lose hosting. It must stay out of
        that lineup."""
        tier = ServiceTier.objects.get(slug=SLUG)
        self.assertEqual(tier.category, 'addon')

    def test_tier_absent_from_the_portal_maintenance_chooser_lineup(self):
        """The guarantee behind the category choice, asserted against the
        helper both self-serve portal surfaces actually call."""
        from clients.views import _maintenance_tiers
        slugs = [t.slug for t in _maintenance_tiers()]
        self.assertNotIn(SLUG, slugs)

    def test_tier_has_feature_bullets_to_render(self):
        tier = ServiceTier.objects.get(slug=SLUG)
        self.assertGreater(tier.features.count(), 0)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ReviewSystemIsBuyableTests(TestCase):
    """Self-checkout has to accept the tier. Without this the price is
    advertised and the Sign Up button lands on a redirect."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_checkout_page_renders_rather_than_redirecting_to_a_call(self):
        tier = ServiceTier.objects.get(slug=SLUG)
        resp = self.client.get(
            reverse('billing:checkout_page', args=[tier.slug]))
        self.assertEqual(resp.status_code, 200, (
            'the review system tier bounced out of checkout — '
            '_tier_allows_selfcheckout in billing/checkout_views.py '
            'probably no longer allowlists it'))
        self.assertContains(resp, tier.name)

    def test_checkout_offers_the_month_to_month_promise(self):
        resp = self.client.get(
            reverse('billing:checkout_page', args=[SLUG]))
        self.assertContains(resp, 'Month-to-month')

    def test_provisioning_shape_is_maintenance(self):
        """Rides the MaintenancePlan path, which already supports a null
        website plus an external_site_url — the record a buyer with no
        site of ours needs."""
        from billing.checkout_views import _provision_product_type
        tier = ServiceTier.objects.get(slug=SLUG)
        self.assertEqual(_provision_product_type(tier), 'maintenance')

    def test_annual_domain_addons_are_still_not_self_checkoutable(self):
        """The allowlist is by slug precisely so 'addon' didn't become a
        blanket grant to the annual domain tiers."""
        from billing.checkout_views import _tier_allows_selfcheckout
        domain = ServiceTier.objects.filter(slug='domain-standard').first()
        if domain is None:
            self.skipTest('domain-standard not seeded')
        self.assertFalse(_tier_allows_selfcheckout(domain))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ReviewSystemPricingIsVisibleTests(TestCase):
    """The $97 has to actually appear where buyers look."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def _price(self):
        return ServiceTier.objects.get(slug=SLUG).get_price_display()

    def test_price_shows_on_the_pricing_page(self):
        resp = self.client.get(reverse('public:pricing'))
        self.assertContains(resp, self._price())

    def test_price_shows_on_the_review_automation_service_page(self):
        resp = self.client.get(
            reverse('public:service_review_automation'))
        self.assertContains(resp, self._price())

    def test_price_shows_on_the_home_page_service_card(self):
        resp = self.client.get(reverse('public:home'))
        self.assertContains(resp, self._price())

    def test_service_page_links_to_checkout(self):
        resp = self.client.get(
            reverse('public:service_review_automation'))
        self.assertContains(
            resp, reverse('billing:checkout_page', args=[SLUG]))

    def test_pricing_page_links_to_checkout(self):
        resp = self.client.get(reverse('public:pricing'))
        self.assertContains(
            resp, reverse('billing:checkout_page', args=[SLUG]))

    def test_cost_table_carries_a_reviews_only_path(self):
        """The total-cost table listed only build-based paths, so a
        reviews-only buyer could not find their own number in it."""
        resp = self.client.get(reverse('public:pricing'))
        self.assertContains(resp, 'Review system only')

    def test_llms_txt_states_it_does_not_require_a_build(self):
        """AI assistants were reading the old llms.txt and correctly
        reporting that review automation required a website build."""
        resp = self.client.get('/llms.txt')
        body = resp.content.decode()
        self.assertIn('Review System (standalone)', body)
        self.assertIn('Does NOT require a website build', body)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class NoPlanOnlyClaimsRemainTests(TestCase):
    """The regression guard. One surviving sentence undoes the work."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_no_selling_page_says_it_is_plan_only(self):
        for path in SELLING_PAGES:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            html = resp.content.decode().lower()
            for phrase in PLAN_ONLY_PHRASES:
                with self.subTest(path=path, phrase=phrase):
                    hits = _review_claims(html, phrase)
                    self.assertEqual(hits, [], (
                        f'"{phrase}" still appears on {path} in a '
                        'sentence about reviews. The review system is '
                        'sold standalone now — this re-closes the door '
                        f'it opened.\nFound: {hits}'))

    def test_review_faq_answers_sold_on_its_own_with_yes(self):
        resp = self.client.get(
            reverse('public:service_review_automation'))
        html = resp.content.decode()
        self.assertIn('Is review automation sold on its own?', html)
        # The answer immediately follows the question in the FAQ card.
        tail = html.split('Is review automation sold on its own?', 1)[1]
        self.assertIn('Yes', tail[:400], (
            'the "sold on its own" FAQ no longer answers yes'))

    def test_review_page_states_no_build_is_required(self):
        resp = self.client.get(
            reverse('public:service_review_automation'))
        self.assertContains(resp, 'No website build required')

    def test_terms_describe_the_standalone_subscription(self):
        resp = self.client.get(reverse('core:terms_of_service'))
        self.assertContains(resp, 'standalone')

    def test_pricing_faq_covers_buying_it_without_a_website(self):
        resp = self.client.get(reverse('public:pricing'))
        self.assertContains(
            resp, 'Can I buy the review system without a website?')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class MissingTierDegradesCleanlyTests(TestCase):
    """Every price on these pages is read from the tier row, so a hidden
    or missing row must not render "$" with nothing after it, an empty
    checkout URL, or a half-built FAQ answer. This is the state the site
    is in before `sync_stripe_products` runs, and on any environment
    where the row was hidden from the admin."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        ServiceTier.objects.filter(slug=SLUG).update(is_public=False)

    def test_pages_still_render_without_the_tier(self):
        for path in SELLING_PAGES:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)

    def test_no_empty_price_or_dangling_sentence_on_pricing(self):
        html = self.client.get(reverse('public:pricing')).content.decode()
        self.assertNotIn('for /month', html)
        self.assertNotIn('Add it for .', html)
        self.assertNotIn('Review system only', html)

    def test_no_empty_price_on_the_service_page(self):
        html = self.client.get(
            reverse('public:service_review_automation')).content.decode()
        self.assertNotIn('Start for </a>', html)
        self.assertNotIn('on its own for /month', html)

    def test_service_page_faq_schema_is_still_valid_json(self):
        """The Service offers array is assembled by hand in the
        template; with one offer dropped it must not leave a trailing
        comma or an empty array element."""
        import json
        import re
        html = self.client.get(
            reverse('public:service_review_automation')).content.decode()
        blocks = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            html, re.DOTALL)
        self.assertTrue(blocks)
        for block in blocks:
            json.loads(block)  # raises on malformed JSON-LD
