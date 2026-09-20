"""
/portal/maintenance/ — a client on a custom/negotiated tier (name
contains "Custom") doesn't see self-serve Switch/Upgrade buttons for
the public tiers, since those would let them move off a one-off
arrangement without anyone weighing in. Also: the "Currently
subscribed" banner shows the real tier name for any tier, not just the
3 legacy slugs it used to hardcode.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from billing.pricing_models import ServiceTier
from clients.account_models import Account
from clients.service_models import MaintenancePlan

User = get_user_model()

_seq = 0


def _account():
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'custplan{_seq}', email=f'custplan{_seq}@example.com',
        password='pw-123456')
    return Account.objects.create(
        user=u, name=f'Custom Plan Co {_seq}', onboarding_status='complete')


class CustomPlanHidesSwitchButtonsTests(TestCase):

    def test_custom_plan_hides_tier_grid_and_shows_note(self):
        ServiceTier.objects.create(
            category='maintenance', name='Denis Custom',
            slug='maintenance-denis-custom', price=Decimal('350.00'),
            is_active=True, is_public=False)
        account = _account()
        MaintenancePlan.objects.create(
            account=account, tier_slug='maintenance-denis-custom',
            status='active')
        self.client.force_login(account.user)

        resp = self.client.get(reverse('clients:portal_maintenance'))
        self.assertContains(resp, 'Denis Custom')
        self.assertContains(resp, "You're on a custom plan")
        self.assertNotContains(resp, 'Switch to')
        self.assertNotContains(resp, 'maint-tier__foot')
        # CLAUDE.md hard rule regression: a multi-line {# #} leaks its
        # literal text into the page instead of being stripped — this
        # exact comment shipped that way once already.
        self.assertNotContains(resp, 'Tier comparison')
        self.assertNotContains(resp, 'anyone weighing in')

    def test_normal_plan_still_shows_tier_grid(self):
        ServiceTier.objects.create(
            category='maintenance', name='Full Plan',
            slug='hvac-full-plan', price=Decimal('250.00'),
            is_active=True, is_public=True)
        account = _account()
        MaintenancePlan.objects.create(
            account=account, tier_slug='hvac-full-plan', status='active')
        self.client.force_login(account.user)

        resp = self.client.get(reverse('clients:portal_maintenance'))
        self.assertContains(resp, 'Full Plan')
        self.assertNotContains(resp, "You're on a custom plan")
