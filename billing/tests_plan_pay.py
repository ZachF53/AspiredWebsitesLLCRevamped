"""
complete_awaiting_plan_payment + the /plan-pay/ views.

Covers the fix for "the checkout is supposed to be on my website" —
start_website_plan's no-card branch no longer creates a Stripe
subscription (and no longer relies on Stripe's own hosted invoice
email); this module's function is what actually creates the
subscription once the client supplies a card on our own page.

Every test mocks Stripe — none may reach the real API.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from billing.pricing_models import ServiceTier
from clients.account_models import Account, Website
from clients.service_models import MaintenancePlan, SocialMediaPlan

User = get_user_model()


def _seed_tiers():
    from decimal import Decimal
    ServiceTier.objects.get_or_create(
        slug='maintenance-essentials', defaults=dict(
            category='maintenance', name='Essentials', price=Decimal('299'),
            is_recurring=True, billing_interval='month',
            stripe_price_id='price_maint_essentials'))
    ServiceTier.objects.get_or_create(
        slug='social-standard', defaults=dict(
            category='social_media', name='Standard', price=Decimal('699'),
            is_recurring=True, billing_interval='month',
            stripe_price_id='price_soc_std'))


class FakeStripeObj(dict):
    """Dict that also answers attribute access — matches how real Stripe
    objects support both `obj.id` and `'id' in obj` / `obj['id']`."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def _fake_subscription(sub_id, client_secret=None):
    invoice = FakeStripeObj()
    if client_secret:
        invoice['confirmation_secret'] = FakeStripeObj(
            client_secret=client_secret)
    return FakeStripeObj(id=sub_id, latest_invoice=invoice)


class CompleteAwaitingPlanPaymentTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        _seed_tiers()
        u = User.objects.create_user(
            username='payplan_user', email='payplan@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(
            user=u, name='Pay Plan Co', stripe_customer_id='cus_payplan')
        cls.website = Website.objects.create(
            account=cls.account, name='Pay Plan Site')

    def setUp(self):
        self.plan = MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='maintenance-essentials', status='awaiting_payment')

    def _patch_stripe(self, sub=None, pi_status='succeeded'):
        p1 = patch('billing.plan_billing._stripe')
        m_stripe = p1.start()
        self.addCleanup(p1.stop)
        s = m_stripe.return_value
        s.PaymentMethod.attach.return_value = None
        s.Subscription.create.return_value = sub or _fake_subscription('sub_new1')
        intent = FakeStripeObj(status=pi_status)
        s.PaymentIntent.confirm.return_value = intent
        return s

    def test_happy_path_activates_plan_and_website(self):
        from billing.plan_billing import complete_awaiting_plan_payment
        s = self._patch_stripe(sub=_fake_subscription(
            'sub_new1', client_secret='pi_1_secret_x'))
        result = complete_awaiting_plan_payment(self.plan, 'pm_card_visa')

        self.assertEqual(result, {'ok': True})
        self.plan.refresh_from_db()
        self.website.refresh_from_db()
        self.assertEqual(self.plan.status, 'active')
        self.assertEqual(self.plan.stripe_subscription_id, 'sub_new1')
        self.assertTrue(self.website.maintenance_active)
        self.assertEqual(
            self.website.stripe_maintenance_subscription_id, 'sub_new1')
        s.PaymentMethod.attach.assert_called_once_with(
            'pm_card_visa', customer='cus_payplan')

    def test_requires_action_leaves_plan_awaiting(self):
        from billing.plan_billing import complete_awaiting_plan_payment
        self._patch_stripe(
            sub=_fake_subscription('sub_new2', client_secret='pi_2_secret_x'),
            pi_status='requires_action')
        result = complete_awaiting_plan_payment(self.plan, 'pm_card_threeds')

        self.assertTrue(result.get('requires_action'))
        self.assertEqual(result.get('client_secret'), 'pi_2_secret_x')
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, 'awaiting_payment')
        self.assertEqual(self.plan.stripe_subscription_id, '')

    def test_pm_attach_failure_returns_error_and_does_not_create_subscription(self):
        from billing.plan_billing import complete_awaiting_plan_payment
        with patch('billing.plan_billing._stripe') as m_stripe:
            s = m_stripe.return_value
            s.PaymentMethod.attach.side_effect = Exception('card declined')
            result = complete_awaiting_plan_payment(self.plan, 'pm_bad')

        self.assertIn('error', result)
        s.Subscription.create.assert_not_called()
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, 'awaiting_payment')

    def test_already_active_plan_is_idempotent(self):
        from billing.plan_billing import complete_awaiting_plan_payment
        self.plan.status = 'active'
        self.plan.stripe_subscription_id = 'sub_already'
        self.plan.save()

        with patch('billing.plan_billing._stripe') as m_stripe:
            result = complete_awaiting_plan_payment(self.plan, 'pm_whatever')
            m_stripe.assert_not_called()

        self.assertEqual(result, {'ok': True})

    def test_discount_applies_coupon(self):
        from billing.plan_billing import complete_awaiting_plan_payment
        self.plan.discount_percent = 15
        self.plan.discount_duration = 'forever'
        self.plan.save()

        s = self._patch_stripe(sub=_fake_subscription('sub_disc'))
        with patch('billing.plan_billing.ensure_percent_coupon',
                   return_value='pct15_forever') as mock_coupon:
            complete_awaiting_plan_payment(self.plan, 'pm_card_visa')

        mock_coupon.assert_called_once()
        _args, kwargs = s.Subscription.create.call_args
        self.assertEqual(kwargs.get('discounts'), [{'coupon': 'pct15_forever'}])

    def test_social_plan_does_not_touch_website_fields(self):
        from billing.plan_billing import complete_awaiting_plan_payment
        social = SocialMediaPlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='social-standard', status='awaiting_payment')
        self._patch_stripe(sub=_fake_subscription('sub_social1'))
        result = complete_awaiting_plan_payment(social, 'pm_card_visa')

        self.assertEqual(result, {'ok': True})
        social.refresh_from_db()
        self.website.refresh_from_db()
        self.assertEqual(social.status, 'active')
        self.assertFalse(self.website.maintenance_active)


class PayPlanViewTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        _seed_tiers()
        u = User.objects.create_user(
            username='payplanview_user', email='payplanview@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(
            user=u, name='Pay Plan View Co', stripe_customer_id='cus_ppv')
        cls.website = Website.objects.create(
            account=cls.account, name='Pay Plan View Site')

    def setUp(self):
        self.plan = MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='maintenance-essentials', status='awaiting_payment')

    def test_pay_plan_page_renders(self):
        r = self.client.get(reverse('pay_plan', args=[self.plan.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Essentials')

    def test_pay_plan_redirects_when_already_active(self):
        self.plan.status = 'active'
        self.plan.stripe_subscription_id = 'sub_x'
        self.plan.save()
        r = self.client.get(reverse('pay_plan', args=[self.plan.id]))
        self.assertRedirects(r, reverse('pay_plan_success', args=[self.plan.id]))

    def test_pay_plan_404_for_unknown_id(self):
        import uuid
        r = self.client.get(reverse('pay_plan', args=[uuid.uuid4()]))
        self.assertEqual(r.status_code, 404)

    def test_pay_plan_confirm_missing_payment_method(self):
        r = self.client.post(
            reverse('pay_plan_confirm', args=[self.plan.id]),
            data='{}', content_type='application/json')
        self.assertEqual(r.status_code, 400)

    def test_pay_plan_confirm_delegates_to_complete_awaiting_plan_payment(self):
        with patch('billing.plan_billing.complete_awaiting_plan_payment',
                   return_value={'ok': True}) as mock_complete:
            r = self.client.post(
                reverse('pay_plan_confirm', args=[self.plan.id]),
                data='{"payment_method_id": "pm_1"}',
                content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {'ok': True})
        mock_complete.assert_called_once()
        args, _kwargs = mock_complete.call_args
        self.assertEqual(args[0].pk, self.plan.pk)
        self.assertEqual(args[1], 'pm_1')

    def test_pay_plan_confirm_short_circuits_when_already_active(self):
        self.plan.status = 'active'
        self.plan.stripe_subscription_id = 'sub_x'
        self.plan.save()
        with patch('billing.plan_billing.complete_awaiting_plan_payment') as mock_complete:
            r = self.client.post(
                reverse('pay_plan_confirm', args=[self.plan.id]),
                data='{"payment_method_id": "pm_1"}',
                content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {'ok': True})
        mock_complete.assert_not_called()

    def test_pay_plan_success_renders(self):
        r = self.client.get(reverse('pay_plan_success', args=[self.plan.id]))
        self.assertEqual(r.status_code, 200)
