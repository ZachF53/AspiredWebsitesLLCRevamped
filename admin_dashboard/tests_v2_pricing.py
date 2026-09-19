"""
v2 Pricing — create/manage ServiceTiers from v2 rather than Django admin
or v1. v1's ServiceTierForm excludes category, slug, is_recurring, and
billing_interval, which is why a tier could be edited there but never
created. Covers:

  - a tier created through v2 with the full field set is saved exactly as
    submitted, and is picked up by sync_stripe_products' own query
  - it's absent from the portal's self-serve chooser (is_public=False)
    and present in v1's Add Plan dropdown (is_active=True, unaffected by
    is_public)
  - once a tier carries a stripe_price_id, its price cannot be changed —
    neither through the rendered form nor a crafted POST
  - the sync-state badge (Synced / NOT SYNCED) reflects stripe_price_id
    correctly on both the list and detail pages
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.pricing_models import ServiceTier
from clients.account_models import Account, Website

User = get_user_model()


def _admin():
    return User.objects.create_user(
        username='pricingv2admin', email='pricingv2admin@example.com',
        password='test-pass-123', is_staff=True, is_superuser=True)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class CreateTierTests(TestCase):

    def setUp(self):
        self.admin = _admin()
        self.client.force_login(self.admin)

    def test_created_tier_has_exactly_the_submitted_values(self):
        resp = self.client.post(reverse('admin_dashboard:v2_pricing_create'), {
            'category': 'maintenance',
            'name': 'Legacy 350',
            'slug': 'maintenance-legacy-350',
            'price': '350.00',
            'is_recurring': 'on',
            'billing_interval': 'month',
            'tagline': '',
            'description': '',
            'is_active': 'on',
            # is_public omitted — unchecked
            'is_featured': '',
            'sort_order': '99',
        })
        self.assertEqual(resp.status_code, 302)

        tier = ServiceTier.objects.get(slug='maintenance-legacy-350')
        self.assertEqual(tier.category, 'maintenance')
        self.assertEqual(tier.name, 'Legacy 350')
        self.assertEqual(tier.price, Decimal('350.00'))
        self.assertTrue(tier.is_recurring)
        self.assertEqual(tier.billing_interval, 'month')
        self.assertTrue(tier.is_active)
        self.assertFalse(tier.is_public)
        self.assertFalse(tier.is_featured)
        self.assertEqual(tier.sort_order, 99)
        self.assertEqual(tier.stripe_price_id, '')

    def test_created_tier_is_picked_up_by_sync_query(self):
        self.client.post(reverse('admin_dashboard:v2_pricing_create'), {
            'category': 'maintenance', 'name': 'Legacy 350',
            'slug': 'maintenance-legacy-350', 'price': '350.00',
            'is_recurring': 'on', 'billing_interval': 'month',
            'is_active': 'on', 'sort_order': '0',
        })
        # Exact query from billing/management/commands/sync_stripe_products.py
        picked_up = ServiceTier.objects.filter(
            is_active=True, stripe_price_id='')
        self.assertIn(
            ServiceTier.objects.get(slug='maintenance-legacy-350'),
            list(picked_up))

    def test_duplicate_slug_rejected(self):
        ServiceTier.objects.create(
            category='maintenance', name='Existing', slug='dupe-slug',
            price=Decimal('1.00'))
        resp = self.client.post(reverse('admin_dashboard:v2_pricing_create'), {
            'category': 'maintenance', 'name': 'New', 'slug': 'dupe-slug',
            'price': '10.00', 'sort_order': '0',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            ServiceTier.objects.filter(slug='dupe-slug').count(), 1)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class VisibilityAndBillabilityTests(TestCase):
    """is_public=False + is_active=True: hidden from self-serve surfaces,
    fully selectable/billable by an operator."""

    @classmethod
    def setUpTestData(cls):
        cls.tier = ServiceTier.objects.create(
            category='maintenance', name='Legacy 350 Hidden',
            slug='maintenance-legacy-350-hidden', price=Decimal('350.00'),
            is_recurring=True, billing_interval='month',
            is_active=True, is_public=False,
            stripe_price_id='price_fake_legacy350',
            stripe_product_id='prod_fake_legacy350')

        u = User.objects.create_user(
            username='chooserclientv2', email='chooserv2@example.com',
            password='x')
        cls.account = Account.objects.create(
            user=u, name='Chooser Co V2', onboarding_status='complete')
        cls.website = Website.objects.create(
            account=cls.account, name='Chooser Co V2', stage='live',
            onboarding_status='complete', build_platform='custom')

    def setUp(self):
        self.admin = _admin()
        self.client.force_login(self.admin)

    def test_absent_from_public_pricing_page(self):
        resp = self.client.get(reverse('public:pricing'))
        self.assertNotContains(resp, self.tier.name)

    def test_absent_from_portal_chooser(self):
        self.client.logout()
        self.client.force_login(self.website.account.user)
        resp = self.client.get(reverse('clients:portal_maintenance'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, self.tier.name)

    def test_present_in_v1_add_plan_dropdown(self):
        resp = self.client.get(
            reverse('admin_dashboard:website_detail',
                    args=[self.website.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.tier.name)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class PriceLockTests(TestCase):
    """Once a tier has a stripe_price_id, price cannot change — by form
    or by crafted POST."""

    def setUp(self):
        self.admin = _admin()
        self.client.force_login(self.admin)
        self.tier = ServiceTier.objects.create(
            category='maintenance', name='Synced Tier',
            slug='maintenance-synced-tier', price=Decimal('299.00'),
            is_recurring=True, billing_interval='month',
            stripe_price_id='price_fake_synced',
            stripe_product_id='prod_fake_synced')

    def _post(self, **overrides):
        data = {
            'category': 'maintenance', 'name': 'Synced Tier',
            'slug': 'maintenance-synced-tier', 'is_recurring': 'on',
            'billing_interval': 'month', 'is_active': 'on',
            'sort_order': '0',
        }
        data.update(overrides)
        return self.client.post(
            reverse('admin_dashboard:v2_pricing_detail', args=[self.tier.id]),
            data)

    def test_form_render_marks_price_readonly(self):
        resp = self.client.get(
            reverse('admin_dashboard:v2_pricing_detail', args=[self.tier.id]))
        content = resp.content.decode()
        self.assertIn('readonly', content)
        self.assertIn('already has a Stripe Price ID', content)

    def test_submitting_through_the_rendered_form_does_not_change_price(self):
        # The rendered form's price field carries the current value via a
        # hidden input (readonly+disabled fields aren't submitted).
        resp = self._post(price=str(self.tier.price))
        self.assertEqual(resp.status_code, 302)
        self.tier.refresh_from_db()
        self.assertEqual(self.tier.price, Decimal('299.00'))

    def test_crafted_post_with_a_different_price_is_ignored(self):
        resp = self._post(price='1.00')
        self.assertEqual(resp.status_code, 302)
        self.tier.refresh_from_db()
        self.assertEqual(self.tier.price, Decimal('299.00'))

    def test_other_fields_still_editable_while_price_is_locked(self):
        self._post(price='1.00', name='Renamed Synced Tier')
        self.tier.refresh_from_db()
        self.assertEqual(self.tier.name, 'Renamed Synced Tier')
        self.assertEqual(self.tier.price, Decimal('299.00'))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class SyncBadgeTests(TestCase):

    def setUp(self):
        self.admin = _admin()
        self.client.force_login(self.admin)

    def test_synced_tier_shows_synced_badge(self):
        tier = ServiceTier.objects.create(
            category='maintenance', name='Has Price Id',
            slug='has-price-id', price=Decimal('1.00'),
            stripe_price_id='price_fake_1')
        list_resp = self.client.get(reverse('admin_dashboard:v2_pricing_list'))
        detail_resp = self.client.get(
            reverse('admin_dashboard:v2_pricing_detail', args=[tier.id]))
        for resp in (list_resp, detail_resp):
            self.assertContains(resp, 'v2-sync-badge--ok')
            self.assertContains(resp, 'Synced')
            self.assertNotContains(resp, 'NOT SYNCED')

    def test_unsynced_tier_shows_not_synced_badge(self):
        tier = ServiceTier.objects.create(
            category='maintenance', name='No Price Id',
            slug='no-price-id', price=Decimal('1.00'),
            stripe_price_id='')
        list_resp = self.client.get(reverse('admin_dashboard:v2_pricing_list'))
        detail_resp = self.client.get(
            reverse('admin_dashboard:v2_pricing_detail', args=[tier.id]))
        for resp in (list_resp, detail_resp):
            self.assertContains(resp, 'v2-sync-badge--missing')
            self.assertContains(resp, 'NOT SYNCED')

    def test_the_five_real_hvac_tiers_show_synced(self):
        for slug, price_id in [
            ('hvac-build-full', 'price_1UHOjSRm8IUsmaKX0xXPrMOf'),
            ('hvac-build-installment', 'price_1UHOjSRm8IUsmaKXWAImDhmd'),
            ('hvac-full-plan', 'price_1UHOjRRm8IUsmaKXdTGmne3x'),
            ('hvac-plan-paid-in-full', 'price_1UHOjSRm8IUsmaKXV1U7lBTY'),
            ('hvac-hosting-security', 'price_1UHOjRRm8IUsmaKXBJm8NLsg'),
        ]:
            ServiceTier.objects.get_or_create(
                slug=slug, defaults=dict(
                    category='maintenance', name=slug, price=Decimal('1.00'),
                    stripe_price_id=price_id))
        resp = self.client.get(reverse('admin_dashboard:v2_pricing_list'))
        self.assertEqual(
            resp.content.decode().count('v2-sync-badge--missing'), 0)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class NavigationAndNoDeleteTests(TestCase):

    def setUp(self):
        self.admin = _admin()
        self.client.force_login(self.admin)

    def test_pricing_link_in_v2_nav(self):
        self.client.get(reverse('admin_dashboard:use_v2'))
        resp = self.client.get(reverse('admin_dashboard:v2_home'))
        self.assertContains(resp, reverse('admin_dashboard:v2_pricing_list'))

    def test_no_delete_route_exists(self):
        tier = ServiceTier.objects.create(
            category='maintenance', name='Undeletable',
            slug='undeletable', price=Decimal('1.00'))
        try:
            reverse('admin_dashboard:v2_pricing_delete', args=[tier.id])
        except Exception:
            pass
        else:
            self.fail('a v2 pricing delete route exists — out of scope')
