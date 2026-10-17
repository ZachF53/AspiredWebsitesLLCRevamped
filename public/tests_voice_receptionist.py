"""
Public surfaces for the AI Voice Receptionist.

Two classes of assertion, and the second is the one that catches real
bugs:

1. The price is on the page and comes from the live tier.
2. **Every page still renders with the tier missing.** The project has
   been bitten by half-rendered price sentences before — "on its own for
   /month" — so the degradation path gets as much coverage as the happy
   one. A deactivated tier must produce a page with no dangling clause,
   no empty price element, and valid FAQ JSON-LD.

Plus the one commercial sentence that must never appear: any public page
claiming the receptionist is included in a plan.
"""

import json

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.pricing_models import ServiceTier

SLUG = 'hvac-voice-receptionist'

PAGES = [
    '/pricing/',
    '/services/ai-receptionist/',
    '/',
]


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class PricingIsVisibleTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()

    def test_service_page_renders_with_the_price(self):
        response = self.client.get('/services/ai-receptionist/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '97')
        self.assertContains(response, 'AI Voice Receptionist')

    def test_pricing_page_shows_the_card(self):
        response = self.client.get('/pricing/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'AI Voice Receptionist')
        self.assertContains(response, reverse(
            'billing:checkout_page', args=[SLUG]))

    def test_both_pages_link_to_checkout(self):
        for url in ('/pricing/', '/services/ai-receptionist/'):
            response = self.client.get(url)
            self.assertContains(response, reverse(
                'billing:checkout_page', args=[SLUG]), msg_prefix=url)

    def test_allowance_and_overage_are_stated_publicly(self):
        """Material terms. A buyer must not meet "300 minutes" for the
        first time on an invoice."""
        response = self.client.get('/services/ai-receptionist/')
        self.assertContains(response, '300')
        self.assertContains(response, '25 cents')

    def test_checkout_page_states_the_allowance(self):
        response = self.client.get(reverse(
            'billing:checkout_page', args=[SLUG]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '300 talk minutes')

    def test_faq_schema_is_valid_json(self):
        response = self.client.get('/services/ai-receptionist/')
        schema = response.context['faq_schema']
        parsed = json.loads(schema)
        self.assertEqual(parsed['@type'], 'FAQPage')
        self.assertTrue(parsed['mainEntity'])

    def test_sitemap_includes_the_page(self):
        response = self.client.get('/sitemap.xml')
        self.assertContains(response, '/services/ai-receptionist/')

    def test_llms_txt_states_it_is_never_bundled(self):
        """An assistant that lumps it in with the Full Plan misquotes our
        price by $97 to anyone who asks."""
        response = self.client.get('/llms.txt')
        body = response.content.decode('utf-8')
        self.assertIn('AI Voice Receptionist', body)
        self.assertIn('NOT included in the Full Plan', body)

    def test_services_nav_links_to_it(self):
        response = self.client.get('/pricing/')
        self.assertContains(response, '/services/ai-receptionist/')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class NeverClaimsToBeBundledTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()

    def test_pricing_page_says_it_is_an_addon(self):
        response = self.client.get('/pricing/')
        body = response.content.decode('utf-8').lower()
        self.assertIn('never bundled', body)

    def test_service_page_answers_the_bundling_question(self):
        response = self.client.get('/services/ai-receptionist/')
        body = response.content.decode('utf-8')
        self.assertIn('Is it included in the Full Plan?', body)

    def test_no_page_claims_the_plan_includes_it(self):
        """The phrasing a well-meaning copy edit would introduce."""
        banned = (
            'receptionist included',
            'includes the ai voice receptionist',
            'receptionist is included in the full plan',
        )
        for url in PAGES:
            body = self.client.get(url).content.decode('utf-8').lower()
            for phrase in banned:
                self.assertNotIn(phrase, body, (
                    f'{url} advertises the receptionist as bundled'))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ScarcityBarTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()

    def test_bar_shows_spots_remaining_not_accounts_enrolled(self):
        """At a real count of two, "2 of 10 clients have this" reads as
        an unpopular product. Spots-left framing is the whole point."""
        response = self.client.get('/services/ai-receptionist/')
        self.assertContains(response, '10 of 10 spots left')
        self.assertNotContains(response, 'clients have this')

    def test_bar_names_the_next_price_and_the_lock(self):
        response = self.client.get('/services/ai-receptionist/')
        self.assertContains(response, '$147')
        body = response.content.decode('utf-8').lower()
        self.assertIn('never changes', body)

    def test_bar_is_hidden_when_there_is_no_cohort(self):
        from voice.models import VoiceCohort
        VoiceCohort.objects.all().delete()
        cache.clear()
        response = self.client.get('/services/ai-receptionist/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'spots left')

    def test_bar_is_hidden_when_the_cohort_is_full(self):
        """Between filling and escalating, "0 spots left at $97" would be
        wrong in both directions."""
        from django.contrib.auth import get_user_model
        from clients.account_models import Account
        from voice.models import VoiceCohort, VoiceProgram
        User = get_user_model()
        cohort = VoiceCohort.objects.get(sequence=1)
        for index in range(cohort.capacity):
            user = User.objects.create_user(
                username=f'full{index}', email=f'full{index}@example.com',
                password='x')
            account = Account.objects.create(user=user, name=f'Full {index}')
            VoiceProgram.objects.create(account=account, cohort=cohort)
        cache.clear()
        response = self.client.get('/services/ai-receptionist/')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'spots left')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class MissingTierDegradesCleanlyTests(TestCase):
    """The tier is deactivated. Every page must still be shippable."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()
        ServiceTier.objects.filter(slug=SLUG).update(
            is_active=False, is_public=False)

    def test_every_page_still_renders(self):
        for url in PAGES:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)

    def test_service_page_has_no_dangling_price_clause(self):
        body = self.client.get(
            '/services/ai-receptionist/').content.decode('utf-8')
        self.assertNotIn('for /month', body)
        self.assertNotIn('Start for </a>', body)
        self.assertNotIn('$/', body)
        self.assertNotIn('class="card__price">$</div>', body)

    def test_faq_schema_stays_valid_without_the_tier(self):
        response = self.client.get('/services/ai-receptionist/')
        parsed = json.loads(response.context['faq_schema'])
        self.assertTrue(parsed['mainEntity'])

    def test_service_jsonld_stays_valid_without_the_tier(self):
        """The bare fallback Offer exists exactly for this case."""
        body = self.client.get(
            '/services/ai-receptionist/').content.decode('utf-8')
        start = body.index('"@type": "Service"')
        chunk = body[body.rindex('<script', 0, start):]
        raw = chunk[chunk.index('>') + 1:chunk.index('</script>')]
        parsed = json.loads(raw)
        self.assertTrue(parsed['offers'])

    def test_pricing_page_omits_the_card_entirely(self):
        """The CARD goes, not every mention. The Services nav link stays
        on purpose: /services/ai-receptionist/ still explains the product
        and degrades to a call-booking CTA, exactly like the review page
        does. What must not survive is a buy button for a tier we have
        switched off."""
        response = self.client.get('/pricing/')
        self.assertNotContains(response, reverse(
            'billing:checkout_page', args=[SLUG]))
        self.assertNotContains(response, 'Never Miss Another After-Hours')

    def test_checkout_redirects_rather_than_selling_an_inactive_tier(self):
        response = self.client.get(reverse(
            'billing:checkout_page', args=[SLUG]))
        self.assertEqual(response.status_code, 302)
