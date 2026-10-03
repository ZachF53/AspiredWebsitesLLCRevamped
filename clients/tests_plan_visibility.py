"""
Account.visible_plan_tiers — per-client override of which maintenance
tiers show on /portal/maintenance/.

Empty (every client today) = unchanged default behavior: only the
public lineup shows. Non-empty = the portal shows exactly the tiers in
the override, including a non-public/custom one — this is how a single
client (e.g. Deins) can be shown just the plan built for her while
everyone else keeps seeing the full public lineup.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from billing.pricing_models import ServiceTier
from clients.account_models import Account

User = get_user_model()

_seq = 0


def _account():
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'visplan{_seq}', email=f'visplan{_seq}@example.com',
        password='pw-123456')
    return Account.objects.create(
        user=u, name=f'Visibility Co {_seq}', onboarding_status='complete')


class PlanVisibilityOverrideTests(TestCase):

    def setUp(self):
        self.public_a = ServiceTier.objects.create(
            category='maintenance', name='Essentials',
            slug='maintenance-essentials-pv', price=Decimal('299.00'),
            is_active=True, is_public=True)
        # Named "Premier" rather than the real "Growth" tier name — the
        # portal page's static pitch copy has its own "Growth" heading
        # unrelated to the tier grid, which would false-positive a
        # assertNotContains check on the word "Growth".
        self.public_b = ServiceTier.objects.create(
            category='maintenance', name='Premier',
            slug='maintenance-premier-pv', price=Decimal('599.00'),
            is_active=True, is_public=True)
        self.custom = ServiceTier.objects.create(
            category='maintenance', name='Deins Custom',
            slug='maintenance-deins-custom-pv', price=Decimal('350.00'),
            is_active=True, is_public=False)

    def test_default_no_override_shows_only_public_tiers(self):
        account = _account()
        self.client.force_login(account.user)

        resp = self.client.get(reverse('clients:portal_maintenance'))
        self.assertContains(resp, 'Essentials')
        self.assertContains(resp, 'Premier')
        self.assertNotContains(resp, 'Deins Custom')

    def test_override_restricts_to_only_the_checked_tier(self):
        account = _account()
        account.visible_plan_tiers.set([self.custom])
        self.client.force_login(account.user)

        resp = self.client.get(reverse('clients:portal_maintenance'))
        self.assertContains(resp, 'Deins Custom')
        self.assertNotContains(resp, 'Essentials')
        self.assertNotContains(resp, 'Premier')

    def test_override_can_add_a_public_tier_alongside_the_custom_one(self):
        account = _account()
        account.visible_plan_tiers.set([self.custom, self.public_a])
        self.client.force_login(account.user)

        resp = self.client.get(reverse('clients:portal_maintenance'))
        self.assertContains(resp, 'Deins Custom')
        self.assertContains(resp, 'Essentials')
        self.assertNotContains(resp, 'Premier')
