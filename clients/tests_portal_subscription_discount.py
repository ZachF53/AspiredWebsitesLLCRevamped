"""
portal_subscriptions.html — discounted vs non-discounted rendering.

_subscription_card (clients/views.py) has always computed list_amount,
amount, and is_discounted, but the template only ever rendered `amount` —
a client on a discounted plan saw one final number with no indication a
discount was even applied. This covers the template-only fix: the
non-discounted path must render exactly as before (single price, no
strikethrough, no discount line), and the discounted path must show the
struck-through list price alongside the discounted total.
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.account_models import Account, Website
from clients.service_models import MaintenancePlan

User = get_user_model()


def _fake_price(unit_amount, product='prod_essentials'):
    return SimpleNamespace(
        unit_amount=unit_amount, currency='usd', product=product,
        recurring=SimpleNamespace(interval='month'),
    )


def _fake_sub(sub_id, unit_amount, coupon_percent_off=None):
    item = SimpleNamespace(price=_fake_price(unit_amount))
    discounts = []
    if coupon_percent_off is not None:
        coupon = SimpleNamespace(percent_off=coupon_percent_off, amount_off=None)
        discounts = [SimpleNamespace(source=SimpleNamespace(coupon=coupon))]
    return SimpleNamespace(
        id=sub_id, status='active',
        items=SimpleNamespace(data=[item]),
        discounts=discounts, discount=None,
        cancel_at_period_end=False, current_period_end=None, trial_end=None,
        default_payment_method='',
    )


class PortalSubscriptionDiscountRenderTests(TestCase):
    """Real view + real template, Stripe calls mocked — no network."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='discountclient', email='discount@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(
            user=cls.user, name='Burgland Technology',
            onboarding_status='complete',
            stripe_customer_id='cus_fake_burgland')
        cls.website = Website.objects.create(
            account=cls.account, name='Burgland Technology',
            stage='live', onboarding_status='complete',
            maintenance_active=True)
        cls.plan = MaintenancePlan.objects.create(
            account=cls.account, website=cls.website,
            tier_slug='maintenance-essentials', status='active',
            stripe_subscription_id='sub_fake_burgland',
            discount_percent=50, discount_duration='forever')

    def setUp(self):
        self.client.force_login(self.user)

    def _get(self, fake_sub):
        with patch('stripe.Subscription.retrieve', return_value=fake_sub), \
             patch('billing.stripe_helpers.get_customer_default_payment_method',
                   return_value=''), \
             patch('billing.stripe_helpers.list_customer_payment_methods',
                   return_value=[]):
            return self.client.get(
                reverse('clients:portal_subscriptions'), follow=True)

    def test_discounted_subscription_shows_list_price_total_and_discount_line(self):
        """Burgland's real numbers: $299 list, 50% off, $149.50 total."""
        fake_sub = _fake_sub('sub_fake_burgland', 29900, coupon_percent_off=50)
        response = self._get(fake_sub)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('$299.00', content)
        self.assertIn('$149.50', content)
        self.assertIn('Discount applied', content)
        self.assertIn('text-decoration:line-through', content)

    def test_non_discounted_subscription_shows_single_amount_no_discount_markup(self):
        """No coupon → identical to the pre-change rendering."""
        fake_sub = _fake_sub('sub_fake_burgland', 29900, coupon_percent_off=None)
        response = self._get(fake_sub)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('$299.00', content)
        self.assertNotIn('Discount applied', content)
        self.assertNotIn('text-decoration:line-through', content)
        # Only one dollar figure for this subscription's price block —
        # not a struck-through list price plus a separate total.
        self.assertEqual(content.count('$299.00'), 1)
