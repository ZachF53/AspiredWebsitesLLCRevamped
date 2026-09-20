"""
v2 website detail — Billing tab, Subscriptions card.

Covers: the Cancel subscription button only renders when there's an
actual subscription to cancel (hosting, maintenance, or build
installment), and the new build-installment status line.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.account_models import Account, Website

User = get_user_model()


class SubscriptionsCardTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='sub_card_staff', email='sub_card_staff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)
        u = User.objects.create_user(
            username='sub_card_client', email='sub_card_client@example.com',
            password='x')
        cls.account = Account.objects.create(user=u, name='Sub Card Co')
        cls.website = Website.objects.create(
            account=cls.account, name='Sub Card Site', build_platform='custom')
        cls.url = (
            reverse('admin_dashboard:v2_website_detail', args=[cls.website.id])
            + '?tab=billing')

    def setUp(self):
        self.client.force_login(self.staff)

    def test_no_subscriptions_hides_cancel_button(self):
        r = self.client.get(self.url)
        self.assertNotContains(r, 'Cancel subscription')
        self.assertContains(r, 'No subscription to cancel')

    def test_hosting_subscription_shows_cancel_button(self):
        self.website.stripe_hosting_subscription_id = 'sub_hosting_1'
        self.website.save(update_fields=['stripe_hosting_subscription_id'])
        r = self.client.get(self.url)
        self.assertContains(r, 'Cancel subscription')

    def test_build_installment_subscription_shows_cancel_button_and_status(self):
        self.website.stripe_build_installment_subscription_id = 'sub_installment_1'
        self.website.save(
            update_fields=['stripe_build_installment_subscription_id'])
        r = self.client.get(self.url)
        self.assertContains(r, 'Website build installment')
        self.assertContains(r, 'Cancel subscription')

    def test_installment_none_by_default(self):
        r = self.client.get(self.url)
        self.assertContains(r, 'Website build installment')
