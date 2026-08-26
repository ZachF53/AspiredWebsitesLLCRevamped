"""
Regression cover for the portal maintenance cancel/resume path.

The bug: `portal_maintenance_cancel` hands `request.account` (an Account) to
`cancel_maintenance_subscription`, which read `client.stripe_subscription_id`
— a column Account does not have. The AttributeError was swallowed by the
view's broad `except Exception` into a "Could not cancel" flash, so the
button silently never worked for any account-based client and no test noticed.

These tests pin the resolution to MaintenancePlan and assert the Stripe call
actually happens.
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from clients.account_models import Account, Website
from clients.service_models import MaintenancePlan

User = get_user_model()


def _stripe_sub(status='active', cancel_at_period_end=False):
    return SimpleNamespace(
        id='sub_test123', status=status, metadata={},
        cancel_at_period_end=cancel_at_period_end,
    )


@override_settings(STRIPE_SECRET_KEY='sk_test_dummy')
class MaintenanceCancelResolvesThroughPlan(TestCase):
    def setUp(self):
        user = User.objects.create_user(
            username='cancelme', email='cancelme@example.com',
            password='test-pass-123')
        # Account may be auto-created by the ClientProfile signal; this test
        # builds the Account directly, which is the account-based shape.
        self.account = Account.objects.filter(user=user).first() or (
            Account.objects.create(user=user, name='Cancel Co'))
        self.website = Website.objects.create(
            account=self.account, name='Cancel Co Site')
        self.plan = MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='maintenance-essentials', status='active',
            stripe_subscription_id='sub_test123')

    def test_account_has_no_subscription_column(self):
        """Guards the premise — if Account ever gains this field, revisit."""
        self.assertFalse(hasattr(self.account, 'stripe_subscription_id'))

    def test_cancel_sets_cancel_at_period_end(self):
        from billing.stripe_helpers import cancel_maintenance_subscription

        with patch('billing.stripe_helpers.stripe') as mock_stripe:
            mock_stripe.Subscription.retrieve.return_value = _stripe_sub()
            mock_stripe.Subscription.modify.return_value = _stripe_sub(
                cancel_at_period_end=True)
            result = cancel_maintenance_subscription(
                self.account, reason='too expensive')

        self.assertIsNotNone(result, 'cancel returned None — sub unresolved')
        args, kwargs = mock_stripe.Subscription.modify.call_args
        self.assertEqual(args[0], 'sub_test123')
        self.assertTrue(kwargs['cancel_at_period_end'])
        self.assertEqual(kwargs['metadata']['cancel_reason'], 'too expensive')

        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, 'cancelled')
        self.assertIsNotNone(self.plan.cancelled_at)

    def test_resume_clears_cancel_at_period_end(self):
        from billing.stripe_helpers import resume_maintenance_subscription

        self.plan.status = 'cancelled'
        self.plan.save(update_fields=['status'])

        with patch('billing.stripe_helpers.stripe') as mock_stripe:
            mock_stripe.Subscription.modify.return_value = _stripe_sub()
            result = resume_maintenance_subscription(self.account)

        self.assertIsNotNone(result)
        args, kwargs = mock_stripe.Subscription.modify.call_args
        self.assertEqual(args[0], 'sub_test123')
        self.assertFalse(kwargs['cancel_at_period_end'])

        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, 'active')

    def test_no_plan_returns_none_without_calling_stripe(self):
        from billing.stripe_helpers import cancel_maintenance_subscription

        MaintenancePlan.objects.all().delete()
        with patch('billing.stripe_helpers.stripe') as mock_stripe:
            result = cancel_maintenance_subscription(self.account)

        self.assertIsNone(result)
        mock_stripe.Subscription.modify.assert_not_called()


class SubscriptionCardShowsDiscountedPrice(TestCase):
    """A 50%-off plan must not render at list price on the billing page."""

    def test_percent_off_is_applied(self):
        from clients.views import _subscription_discounted

        sub = SimpleNamespace(discounts=[
            SimpleNamespace(coupon=SimpleNamespace(
                percent_off=50.0, amount_off=None))])
        self.assertEqual(_subscription_discounted(sub, 299.00), 149.50)

    def test_amount_off_is_applied(self):
        from clients.views import _subscription_discounted

        sub = SimpleNamespace(discounts=[
            SimpleNamespace(coupon=SimpleNamespace(
                percent_off=None, amount_off=5000))])
        self.assertEqual(_subscription_discounted(sub, 299.00), 249.00)

    def test_no_discount_leaves_list_price(self):
        from clients.views import _subscription_discounted

        sub = SimpleNamespace(discounts=[], discount=None)
        self.assertEqual(_subscription_discounted(sub, 299.00), 299.00)

    def test_unexpanded_discount_id_is_skipped_not_guessed(self):
        from clients.views import _subscription_discounted

        sub = SimpleNamespace(discounts=['di_bare_id'], discount=None)
        self.assertEqual(_subscription_discounted(sub, 299.00), 299.00)

    def test_coupon_nested_under_source_is_applied(self):
        """API 2026-04-22 moved the coupon to discount.source.coupon.

        Reading only discount.coupon silently returned list price against
        a live subscription that was genuinely 50% off.
        """
        from clients.views import _subscription_discounted

        sub = SimpleNamespace(discounts=[SimpleNamespace(
            coupon=None,
            source=SimpleNamespace(coupon=SimpleNamespace(
                percent_off=50.0, amount_off=None)))])
        self.assertEqual(_subscription_discounted(sub, 299.00), 149.50)

    def test_bare_coupon_id_under_source_is_skipped(self):
        from clients.views import _subscription_discounted

        sub = SimpleNamespace(discounts=[SimpleNamespace(
            coupon=None, source=SimpleNamespace(coupon='pct50_forever'))])
        self.assertEqual(_subscription_discounted(sub, 299.00), 299.00)
