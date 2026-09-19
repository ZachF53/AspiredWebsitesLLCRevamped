"""
v2 Billing tab — Add Plan form.

Covers the thin-caller contract with billing.plan_billing.start_website_plan
(the view must not reimplement tier lookup, customer/coupon creation, the
card-on-file branch, or the local row write), the card-state banner, the
price preview data feeding the template, the per-service-type "already has
an active plan" guard, the confirmation-required gate, and server-side
discount validation.

Every test mocks Stripe at the billing.plan_billing / billing.stripe_helpers
seams — none may reach the real Stripe API.
"""

from contextlib import ExitStack
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.pricing_models import ServiceTier
from clients.account_models import Account, Website
from clients.service_models import MaintenancePlan

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class V2AddPlanTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='ap_staff', email='ap_staff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)

        u = User.objects.create_user(
            username='ap_client', email='ap_client@example.com', password='x')
        cls.account = Account.objects.filter(user=u).first() or (
            Account.objects.create(user=u, name='Add Plan Co'))
        cls.account.websites.all().delete()
        cls.website = Website.objects.create(
            account=cls.account, name='Add Plan Site', build_platform='custom')

        cls.maint_tier = ServiceTier.objects.create(
            category='maintenance', name='Growth', slug='maintenance-growth-ap',
            price=Decimal('599.00'), stripe_price_id='price_maint_growth',
            is_active=True, is_public=True)
        cls.hidden_tier = ServiceTier.objects.create(
            category='maintenance', name='Legacy Grandfather',
            slug='maintenance-legacy-ap', price=Decimal('199.00'),
            stripe_price_id='price_legacy', is_active=True, is_public=False)
        cls.social_tier = ServiceTier.objects.create(
            category='social_media', name='Standard', slug='social-standard-ap',
            price=Decimal('699.00'), stripe_price_id='price_social_std',
            is_active=True, is_public=True)

        cls.billing_url = (
            reverse('admin_dashboard:v2_website_detail', args=[cls.website.id])
            + '?tab=billing')
        cls.add_plan_url = reverse(
            'admin_dashboard:v2_website_add_plan', args=[cls.website.id])

    def setUp(self):
        self.client.force_login(self.staff)

    def _card_patches(self, has_card, brand='visa', last4='4242'):
        stack = ExitStack()
        stack.enter_context(patch('billing.plan_billing._stripe'))
        stack.enter_context(
            patch('billing.plan_billing._customer_id_for', return_value='cus_test'))
        stack.enter_context(
            patch('billing.plan_billing._has_card_on_file', return_value=has_card))
        if has_card:
            stack.enter_context(patch(
                'billing.stripe_helpers.get_customer_default_payment_method',
                return_value='pm_1'))
            pm = MagicMock()
            pm.id = 'pm_1'
            pm.card = MagicMock()
            pm.card.brand = brand
            pm.card.last4 = last4
            stack.enter_context(patch(
                'billing.stripe_helpers.list_customer_payment_methods',
                return_value=[pm]))
        return stack

    # ── Form rendering ──

    def test_billing_tab_renders_hidden_tier(self):
        with self._card_patches(False):
            r = self.client.get(self.billing_url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Legacy Grandfather')
        self.assertContains(r, 'value="maintenance-legacy-ap"')

    def test_card_on_file_shows_charge_warning(self):
        with self._card_patches(True, brand='visa', last4='4242'):
            r = self.client.get(self.billing_url)
        self.assertContains(r, 'Card on file')
        self.assertContains(r, 'VISA')
        self.assertContains(r, '4242')
        self.assertContains(r, 'charge it immediately')

    def test_no_card_shows_invoice_warning(self):
        with self._card_patches(False):
            r = self.client.get(self.billing_url)
        self.assertContains(r, 'No card on file')
        self.assertContains(r, 'Stripe-hosted invoice')

    # ── Submit → thin-caller contract ──

    def test_add_plan_passes_discount_and_duration(self):
        with patch('billing.plan_billing.start_website_plan') as mock_start:
            mp = MagicMock()
            mp.status = 'active'
            mp.stripe_subscription_id = 'sub_ap1'
            mp.discount_percent = 15
            mp.discount_duration = 'forever'
            mp.get_discount_duration_display.return_value = 'Forever'
            mock_start.return_value = mp
            r = self.client.post(self.add_plan_url, data={
                'service_type': 'maintenance',
                'tier_slug': self.maint_tier.slug,
                'discount_percent': '15',
                'discount_duration': 'forever',
                'confirmed': 'yes',
            })
        self.assertEqual(r.status_code, 302)
        mock_start.assert_called_once()
        args, kwargs = mock_start.call_args
        self.assertEqual(args[0], self.website)
        self.assertEqual(args[1], 'maintenance')
        self.assertEqual(args[2], self.maint_tier.slug)
        self.assertEqual(kwargs.get('discount_percent'), 15)
        self.assertEqual(kwargs.get('discount_duration'), 'forever')

    def test_add_plan_without_confirmation_is_refused(self):
        """One click from a dropdown must not be enough — the hidden
        `confirmed` field (set by the Review step's JS) is required."""
        with patch('billing.plan_billing.start_website_plan') as mock_start, \
             self._card_patches(False):
            r = self.client.post(self.add_plan_url, data={
                'service_type': 'maintenance',
                'tier_slug': self.maint_tier.slug,
            }, follow=True)
        mock_start.assert_not_called()
        self.assertContains(r, 'Confirm the plan details')

    # ── Guard: second plan of the same service type ──

    def test_add_plan_blocked_when_active_plan_exists(self):
        MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug=self.maint_tier.slug, status='active',
            stripe_subscription_id='sub_existing_ap')

        with patch('billing.plan_billing.start_website_plan') as mock_start, \
             self._card_patches(False):
            r = self.client.post(self.add_plan_url, data={
                'service_type': 'maintenance',
                'tier_slug': self.maint_tier.slug,
                'confirmed': 'yes',
            }, follow=True)
        mock_start.assert_not_called()
        self.assertContains(r, 'already has an active')
        self.assertContains(r, 'sub_existing_ap')

    def test_add_plan_not_blocked_for_different_service_type(self):
        """An active MAINTENANCE plan must not block adding a SOCIAL
        plan — the guard is per service type, matching
        start_website_plan's own per-(account, website, category) row."""
        MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug=self.maint_tier.slug, status='active',
            stripe_subscription_id='sub_existing_ap2')

        with patch('billing.plan_billing.start_website_plan') as mock_start:
            mp = MagicMock()
            mp.status = 'active'
            mp.stripe_subscription_id = 'sub_social_new'
            mp.discount_percent = None
            mp.discount_duration = ''
            mock_start.return_value = mp
            r = self.client.post(self.add_plan_url, data={
                'service_type': 'social',
                'tier_slug': self.social_tier.slug,
                'confirmed': 'yes',
            })
        mock_start.assert_called_once()
        self.assertEqual(r.status_code, 302)

    # ── Server-side discount validation ──

    def test_add_plan_rejects_zero_discount(self):
        with patch('billing.plan_billing.start_website_plan') as mock_start, \
             self._card_patches(False):
            r = self.client.post(self.add_plan_url, data={
                'service_type': 'maintenance',
                'tier_slug': self.maint_tier.slug,
                'discount_percent': '0',
                'confirmed': 'yes',
            }, follow=True)
        mock_start.assert_not_called()
        self.assertContains(r, 'whole number from 1 to 100')

    def test_add_plan_rejects_over_100_discount(self):
        with patch('billing.plan_billing.start_website_plan') as mock_start, \
             self._card_patches(False):
            r = self.client.post(self.add_plan_url, data={
                'service_type': 'maintenance',
                'tier_slug': self.maint_tier.slug,
                'discount_percent': '150',
                'confirmed': 'yes',
            }, follow=True)
        mock_start.assert_not_called()
        self.assertContains(r, 'whole number from 1 to 100')

    def test_add_plan_rejects_non_integer_discount(self):
        with patch('billing.plan_billing.start_website_plan') as mock_start, \
             self._card_patches(False):
            r = self.client.post(self.add_plan_url, data={
                'service_type': 'maintenance',
                'tier_slug': self.maint_tier.slug,
                'discount_percent': 'abc',
                'confirmed': 'yes',
            }, follow=True)
        mock_start.assert_not_called()
        self.assertContains(r, 'whole number from 1 to 100')

    def test_add_plan_blank_discount_is_allowed(self):
        with patch('billing.plan_billing.start_website_plan') as mock_start:
            mp = MagicMock()
            mp.status = 'active'
            mp.stripe_subscription_id = 'sub_ap_blank'
            mp.discount_percent = None
            mp.discount_duration = ''
            mock_start.return_value = mp
            r = self.client.post(self.add_plan_url, data={
                'service_type': 'maintenance',
                'tier_slug': self.maint_tier.slug,
                'discount_percent': '',
                'confirmed': 'yes',
            })
        self.assertEqual(r.status_code, 302)
        mock_start.assert_called_once()
        _args, kwargs = mock_start.call_args
        self.assertIsNone(kwargs.get('discount_percent'))

    # ── Result display ──

    def test_add_plan_awaiting_payment_message_says_local_id_not_set(self):
        with patch('billing.plan_billing.start_website_plan') as mock_start, \
             self._card_patches(False):
            mp = MagicMock()
            mp.status = 'awaiting_payment'
            mp.stripe_subscription_id = 'sub_await_ap'
            mp.discount_percent = None
            mp.discount_duration = ''
            mock_start.return_value = mp
            r = self.client.post(self.add_plan_url, data={
                'service_type': 'maintenance',
                'tier_slug': self.maint_tier.slug,
                'confirmed': 'yes',
            }, follow=True)
        self.assertContains(r, 'sub_await_ap')
        self.assertContains(r, 'awaiting payment')
        self.assertContains(r, 'was NOT set')

    def test_add_plan_missing_tier_shows_error(self):
        with patch('billing.plan_billing.start_website_plan') as mock_start, \
             self._card_patches(False):
            r = self.client.post(self.add_plan_url, data={
                'service_type': 'maintenance',
                'tier_slug': '',
                'confirmed': 'yes',
            }, follow=True)
        mock_start.assert_not_called()
        self.assertContains(r, 'Choose a plan type and a tier')

    # ── Safety net: live-subscription block ──

    def test_add_plan_blocked_when_website_has_live_subscription(self):
        # A dedicated Website (not the shared cls.website) so this
        # doesn't mutate a setUpTestData object other tests reuse.
        live_website = Website.objects.create(
            account=self.account, name='Live Sub Site',
            build_platform='custom',
            stripe_hosting_subscription_id='sub_hosting_live_ap')
        url = reverse('admin_dashboard:v2_website_add_plan',
                       args=[live_website.id])
        with patch('billing.plan_billing.start_website_plan') as mock_start, \
             self._card_patches(False):
            r = self.client.post(url, data={
                'service_type': 'maintenance',
                'tier_slug': self.maint_tier.slug,
                'confirmed': 'yes',
            }, follow=True)
        mock_start.assert_not_called()
        self.assertContains(r, 'live Stripe subscription')
