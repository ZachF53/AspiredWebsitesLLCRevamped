"""
v2 Billing tab — Plan Visibility card (account_set_visible_tiers).

Covers: empty POST clears the override (client falls back to the public
lineup), a non-empty POST replaces the override with exactly the tiers
checked (including a non-public/custom tier), and the endpoint is
visibility-only — it never calls into billing.plan_billing or touches
any MaintenancePlan/Stripe state.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.pricing_models import ServiceTier
from clients.account_models import Account, Website

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class V2VisibleTiersTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='vt_staff', email='vt_staff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)

        u = User.objects.create_user(
            username='vt_client', email='vt_client@example.com', password='x')
        cls.account = Account.objects.filter(user=u).first() or (
            Account.objects.create(user=u, name='Visible Tiers Co'))
        cls.account.websites.all().delete()
        cls.website = Website.objects.create(
            account=cls.account, name='Visible Tiers Site',
            build_platform='custom')

        cls.public_tier = ServiceTier.objects.create(
            category='maintenance', name='Growth', slug='maintenance-growth-vt',
            price=Decimal('599.00'), is_active=True, is_public=True)
        cls.hidden_tier = ServiceTier.objects.create(
            category='maintenance', name='Deins Custom',
            slug='maintenance-deins-custom', price=Decimal('350.00'),
            is_active=True, is_public=False)
        cls.social_tier = ServiceTier.objects.create(
            category='social_media', name='Standard', slug='social-standard-vt',
            price=Decimal('699.00'), is_active=True, is_public=True)

        cls.billing_url = (
            reverse('admin_dashboard:v2_website_detail', args=[cls.website.id])
            + '?tab=billing')
        cls.visible_tiers_url = reverse(
            'admin_dashboard:v2_account_set_visible_tiers', args=[cls.website.id])

    def setUp(self):
        self.client.force_login(self.staff)

    def test_checking_a_hidden_tier_restricts_the_account_to_it(self):
        r = self.client.post(
            self.visible_tiers_url, {'tier_ids': [str(self.hidden_tier.id)]})
        self.assertEqual(r.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(
            set(self.account.visible_plan_tiers.values_list('id', flat=True)),
            {self.hidden_tier.id})

    def test_checking_nothing_clears_the_override(self):
        self.account.visible_plan_tiers.set([self.hidden_tier])
        r = self.client.post(self.visible_tiers_url, {})
        self.assertEqual(r.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.visible_plan_tiers.count(), 0)

    def test_social_tier_ids_are_ignored(self):
        """The override is maintenance-only — a social tier id posted
        here (e.g. a stray field) must not get attached."""
        r = self.client.post(
            self.visible_tiers_url,
            {'tier_ids': [str(self.hidden_tier.id), str(self.social_tier.id)]})
        self.assertEqual(r.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(
            set(self.account.visible_plan_tiers.values_list('id', flat=True)),
            {self.hidden_tier.id})

    def test_get_is_not_allowed(self):
        r = self.client.get(self.visible_tiers_url)
        self.assertEqual(r.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.visible_plan_tiers.count(), 0)

    def test_unauthenticated_post_is_redirected_not_applied(self):
        self.client.logout()
        r = self.client.post(
            self.visible_tiers_url, {'tier_ids': [str(self.hidden_tier.id)]})
        self.assertEqual(r.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.visible_plan_tiers.count(), 0)

    def test_billing_tab_renders_visibility_card_with_current_state(self):
        self.account.visible_plan_tiers.set([self.hidden_tier])
        r = self.client.get(self.billing_url)
        self.assertContains(r, 'Plan visibility')
        self.assertContains(r, 'Deins Custom')
        self.assertContains(r, 'Growth')
