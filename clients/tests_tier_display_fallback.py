"""
MaintenancePlan/SocialMediaPlan.get_tier_slug_display() — regression for
"maintenance-denis-custom" leaking onto the plan-activated page as a raw
slug. TIER_CHOICES only knows the three original hardcoded tiers;
anything created later (HVAC-era tiers, operator custom tiers) needs
ServiceTier.name as the real source of truth.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from billing.pricing_models import ServiceTier
from clients.account_models import Account
from clients.service_models import MaintenancePlan, SocialMediaPlan

User = get_user_model()

_seq = 0


def _account():
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'tierdisp{_seq}', email=f'tierdisp{_seq}@example.com',
        password='x')
    return Account.objects.create(user=u, name=f'Tier Display Co {_seq}')


class MaintenancePlanTierDisplayTests(TestCase):

    def test_hardcoded_tier_still_displays_via_static_choices(self):
        plan = MaintenancePlan.objects.create(
            account=_account(), tier_slug='maintenance-essentials')
        self.assertEqual(plan.get_tier_slug_display(), 'Essentials')

    def test_custom_tier_not_in_static_choices_uses_service_tier_name(self):
        ServiceTier.objects.create(
            category='maintenance', name='Denis Custom',
            slug='maintenance-denis-custom', price=Decimal('350.00'),
            is_active=True, is_public=False)
        plan = MaintenancePlan.objects.create(
            account=_account(), tier_slug='maintenance-denis-custom')
        self.assertEqual(plan.get_tier_slug_display(), 'Denis Custom')
        self.assertNotIn('-', plan.get_tier_slug_display())

    def test_unknown_slug_with_no_matching_service_tier_falls_back_to_raw(self):
        plan = MaintenancePlan.objects.create(
            account=_account(), tier_slug='totally-made-up-slug')
        self.assertEqual(
            plan.get_tier_slug_display(), 'totally-made-up-slug')

    def test_a_non_maintenance_tier_on_this_row_still_gets_a_real_name(self):
        """A MaintenancePlan legitimately carries tiers whose
        ServiceTier.category is NOT 'maintenance':
        billing.checkout_views._provision_product_type maps both
        'hosting' and 'addon' sales onto this model. Filtering the lookup
        on category alone sent those to the TIER_CHOICES fallback, which
        doesn't list them either, so the raw slug rendered in the portal.
        """
        ServiceTier.objects.create(
            category='addon', name='Review System',
            slug='hvac-review-system-display-test', price=Decimal('97.00'),
            is_recurring=True, billing_interval='month', is_active=True)
        plan = MaintenancePlan.objects.create(
            account=_account(), tier_slug='hvac-review-system-display-test')
        self.assertEqual(plan.get_tier_slug_display(), 'Review System')


class PendingTierDisplayTests(TestCase):
    """`pending_tier_display` drives the portal's "switching to X on Y"
    notice. It read TIER_CHOICES only, so a queued HVAC or custom tier
    rendered as a raw slug — the same bug as above, one property over."""

    def test_a_queued_hvac_tier_renders_its_real_name(self):
        ServiceTier.objects.create(
            category='maintenance', name='Full Plan: Build Paid Upfront',
            slug='hvac-plan-paid-in-full-pending-test',
            price=Decimal('145.00'), is_recurring=True,
            billing_interval='month', is_active=True)
        plan = MaintenancePlan.objects.create(
            account=_account(), tier_slug='maintenance-growth',
            pending_tier_slug='hvac-plan-paid-in-full-pending-test')
        self.assertEqual(plan.pending_tier_display,
                         'Full Plan: Build Paid Upfront')

    def test_no_pending_tier_is_an_empty_string(self):
        plan = MaintenancePlan.objects.create(
            account=_account(), tier_slug='maintenance-growth')
        self.assertEqual(plan.pending_tier_display, '')

    def test_a_legacy_queued_tier_still_uses_static_choices(self):
        plan = MaintenancePlan.objects.create(
            account=_account(), tier_slug='maintenance-dominant',
            pending_tier_slug='maintenance-essentials')
        self.assertEqual(plan.pending_tier_display, 'Essentials')


class SocialMediaPlanTierDisplayTests(TestCase):

    def test_custom_tier_uses_service_tier_name(self):
        ServiceTier.objects.create(
            category='social_media', name='Custom Social',
            slug='social-custom-xyz', price=Decimal('450.00'),
            is_active=True, is_public=False)
        plan = SocialMediaPlan.objects.create(
            account=_account(), tier_slug='social-custom-xyz')
        self.assertEqual(plan.get_tier_slug_display(), 'Custom Social')
