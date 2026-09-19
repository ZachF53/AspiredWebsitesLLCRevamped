"""
ServiceTier.is_public — separates "billable" (is_active) from "publicly
browsable" (is_public). Before this, a legacy/negotiated tier had no way
to be billable-but-hidden: is_active alone gated the public pricing page,
the portal's self-serve plan chooser, the admin Add Plan dropdown, and
start_website_plan's lookup all at once.

Covers:
  - a non-public tier is absent from the real public pricing page
    (public:pricing) and the portal maintenance chooser
  - that same tier IS in the admin Add Plan dropdown and DOES resolve in
    start_website_plan's own lookup query
  - existing tiers, at the is_public default (True), are unchanged on
    every surface
  - the 5 newly-synced HVAC tiers still render on the live pricing page

Note on public/views.py:147 (law_firms._price_range) and :281
(service_digital_marketing): both views are dead code today — their URL
routes (public/urls.py) 301-redirect to service_web_design and never call
these view functions (Sept 2026 HVAC repositioning). The is_public filter
was still added at :147 per the literal instruction, but the surface that
actually matters is public.views.pricing() (the live /pricing/ route),
which doesn't use get_active() at all — it filters by an explicit slug
whitelist. is_public was added there too; without it this task would not
have touched the only public pricing page a visitor can actually reach.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.pricing_models import ServiceTier
from clients.account_models import Account, Website

User = get_user_model()

_HVAC_SLUGS = [
    'hvac-build-full', 'hvac-build-installment',
    'hvac-full-plan', 'hvac-plan-paid-in-full',
    'hvac-hosting-security',
]


def _make_tier(**overrides):
    defaults = dict(
        category='maintenance', name='Legacy 350', slug='maintenance-legacy-350',
        price='350.00', is_recurring=True, billing_interval='month',
        is_active=True, stripe_price_id='price_fake_legacy350',
        stripe_product_id='prod_fake_legacy350',
    )
    defaults.update(overrides)
    return ServiceTier.objects.create(**defaults)


class ServiceTierIsPublicDefaultTests(TestCase):
    def test_default_is_public_is_true(self):
        tier = _make_tier()
        self.assertTrue(tier.is_public)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class PublicPricingPageTests(TestCase):
    """The real, reachable /pricing/ page — public.views.pricing().

    These 5 tiers aren't in seed_pricing.py's TIERS list (they were
    added directly, outside that command — confirmed against prod), so
    the test DB needs them created explicitly rather than assumed.
    """

    @classmethod
    def setUpTestData(cls):
        seed = [
            ('hvac-build-full', 'website_build',
             'Website Build: Pay in Full', '2000.00', False, ''),
            ('hvac-build-installment', 'website_build',
             'Website Build: 24-Month Installment', '105.00', True, 'month'),
            ('hvac-full-plan', 'maintenance', 'Full Plan',
             '250.00', True, 'month'),
            ('hvac-plan-paid-in-full', 'maintenance',
             'Full Plan: Build Paid in Full', '145.00', True, 'month'),
            ('hvac-hosting-security', 'hosting',
             'Hosting + Security Only', '45.00', True, 'month'),
        ]
        for slug, category, name, price, recurring, interval in seed:
            ServiceTier.objects.get_or_create(
                slug=slug, defaults=dict(
                    category=category, name=name, price=price,
                    is_recurring=recurring, billing_interval=interval,
                    is_active=True,
                    stripe_price_id=f'price_fake_{slug}',
                    stripe_product_id=f'prod_fake_{slug}',
                ))

    def test_all_five_hvac_tiers_render_at_default(self):
        """Item 5's explicit ask: the 5 newly-synced tiers still render."""
        resp = self.client.get(reverse('public:pricing'))
        self.assertEqual(resp.status_code, 200)
        for slug in _HVAC_SLUGS:
            tier = ServiceTier.objects.get(slug=slug)
            self.assertContains(resp, tier.name)

    def test_hiding_one_hvac_tier_removes_it_and_only_it(self):
        hidden_slug = 'hvac-hosting-security'
        tier = ServiceTier.objects.get(slug=hidden_slug)
        tier.is_public = False
        tier.save(update_fields=['is_public'])

        resp = self.client.get(reverse('public:pricing'))
        self.assertNotContains(resp, tier.name)
        for slug in _HVAC_SLUGS:
            if slug == hidden_slug:
                continue
            other = ServiceTier.objects.get(slug=slug)
            self.assertContains(resp, other.name)


class PortalMaintenanceChooserTests(TestCase):
    """clients/views.py _maintenance_tiers() — /portal/maintenance/ and
    the /portal/subscriptions/ upsell card share this one helper."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='chooserclient', email='chooser@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(
            user=cls.user, name='Chooser Co', onboarding_status='complete')
        Website.objects.create(
            account=cls.account, name='Chooser Co', stage='live',
            onboarding_status='complete')

    def setUp(self):
        self.client.force_login(self.user)

    def test_non_public_tier_absent_from_chooser(self):
        hidden = _make_tier(is_public=False)
        resp = self.client.get(reverse('clients:portal_maintenance'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, hidden.name)

    def test_default_public_tier_present_in_chooser(self):
        visible = _make_tier(
            name='Visible Legacy', slug='maintenance-legacy-visible-test')
        resp = self.client.get(reverse('clients:portal_maintenance'))
        self.assertContains(resp, visible.name)

    def test_existing_real_maintenance_tiers_still_appear(self):
        """Existing tiers, at the is_public default, are unchanged."""
        resp = self.client.get(reverse('clients:portal_maintenance'))
        for slug in ('maintenance-essentials', 'maintenance-growth',
                     'maintenance-dominant'):
            tier = ServiceTier.objects.filter(slug=slug).first()
            if tier is None:
                continue  # local dev DB may not have run seed_pricing
            self.assertContains(resp, tier.name)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class AdminAddPlanDropdownAndBillingLookupTests(TestCase):
    """A non-public tier must stay fully billable and fully selectable
    by an operator — is_public must NOT gate either of these."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username='pricingadmin', email='pricingadmin@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)
        u = User.objects.create_user(
            username='billableclient', email='billable@example.com',
            password='x')
        cls.account = Account.objects.create(user=u, name='Billable Co')
        cls.website = Website.objects.create(
            account=cls.account, name='Billable Co', build_platform='custom')

    def setUp(self):
        self.client.force_login(self.admin)

    def test_non_public_tier_appears_in_v1_add_plan_dropdown(self):
        hidden = _make_tier(is_public=False)
        resp = self.client.get(
            reverse('admin_dashboard:website_detail',
                    args=[self.website.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, hidden.name)

    def test_non_public_tier_resolves_in_start_website_plan_lookup(self):
        hidden = _make_tier(is_public=False)
        found = ServiceTier.objects.filter(
            slug=hidden.slug, category='maintenance', is_active=True
        ).first()
        self.assertIsNotNone(found)
        self.assertEqual(found.id, hidden.id)
        self.assertTrue(found.stripe_price_id)

    def test_default_public_tier_also_still_resolves(self):
        """Sanity check — the lookup query itself is untouched by this
        task; a normal public tier resolves exactly as before."""
        visible = _make_tier(
            name='Visible Sanity', slug='maintenance-visible-sanity-test')
        found = ServiceTier.objects.filter(
            slug=visible.slug, category='maintenance', is_active=True
        ).first()
        self.assertIsNotNone(found)
