"""
portal_subscriptions must not show an unpaid, awaiting_payment plan as an
active recurring service.

Found via staging QA on 2026-09-19: a maintenance plan added with no card
on file (collection_method='send_invoice') is 'active' in Stripe's own
subscription.status the instant it's created — Stripe doesn't have an
"awaiting first invoice payment" subscription status. The maintenance-
plans query on this page never filtered by our own MaintenancePlan.status
(unlike the social-plans query right below it, which already did), so an
unpaid, no-card Add Plan showed up on the client's own billing page as a
normal "Active" $X/month subscription before anyone had paid anything.
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.account_models import Account, Website
from clients.service_models import MaintenancePlan

User = get_user_model()


def _fake_sub(sub_id, collection_method='send_invoice'):
    return SimpleNamespace(
        id=sub_id, status='active', collection_method=collection_method,
        items=SimpleNamespace(data=[]),
        discounts=[], discount=None,
        cancel_at_period_end=False, current_period_end=None, trial_end=None,
        default_payment_method='',
    )


class AwaitingPaymentPlanTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='awaitingclient', email='awaiting@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(
            user=cls.user, name='Awaiting Co', onboarding_status='complete',
            stripe_customer_id='cus_fake_awaiting')
        cls.website = Website.objects.create(
            account=cls.account, name='Awaiting Co', stage='live',
            onboarding_status='complete')

    def setUp(self):
        self.client.force_login(self.user)

    def test_awaiting_payment_plan_is_not_shown_as_a_subscription(self):
        MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='maintenance-essentials', status='awaiting_payment',
            stripe_subscription_id='sub_fake_unpaid')

        # Stripe would never even be queried for this sub — the fix is
        # that it's filtered out of label_by_sub before that loop runs —
        # but stub it anyway so a regression can't slip through by
        # accidentally reaching a live-looking Stripe call.
        with patch('stripe.Subscription.retrieve',
                    return_value=_fake_sub('sub_fake_unpaid')) as mock_retrieve:
            response = self.client.get(
                reverse('clients:portal_subscriptions'), follow=True)

        self.assertEqual(response.status_code, 200)
        mock_retrieve.assert_not_called()
        content = response.content.decode()
        self.assertNotIn('sub_fake_unpaid', content)
        self.assertIn('No active subscriptions.', content)

    def test_active_plan_still_shows(self):
        """The fix must not swallow genuinely active plans."""
        from types import SimpleNamespace as SN
        fake_sub = SN(
            id='sub_fake_active', status='active',
            collection_method='charge_automatically',
            items=SN(data=[SN(price=SN(
                unit_amount=29900, currency='usd', product='prod_x',
                recurring=SN(interval='month')))]),
            discounts=[], discount=None, cancel_at_period_end=False,
            current_period_end=None, trial_end=None,
            default_payment_method='',
        )
        MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='maintenance-essentials', status='active',
            stripe_subscription_id='sub_fake_active')

        with patch('stripe.Subscription.retrieve', return_value=fake_sub), \
             patch('billing.stripe_helpers.get_customer_default_payment_method',
                   return_value=''), \
             patch('billing.stripe_helpers.list_customer_payment_methods',
                   return_value=[]):
            response = self.client.get(
                reverse('clients:portal_subscriptions'), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('$299.00', response.content.decode())
