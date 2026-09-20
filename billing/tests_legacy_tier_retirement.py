"""
seed_pricing — the three original maintenance tiers (Essentials/Growth/
Dominant) are retired (is_active=False) in favour of the HVAC-era
hvac-full-plan / hvac-plan-paid-in-full, and must stay retired across
re-seeds (seed_pricing is documented as safe to re-run any time).
"""

from django.core.management import call_command
from django.test import TestCase

from billing.pricing_models import ServiceTier

_RETIRED = ['maintenance-essentials', 'maintenance-growth', 'maintenance-dominant']
_CURRENT = ['hvac-full-plan', 'hvac-plan-paid-in-full']


class LegacyMaintenanceTierRetirementTests(TestCase):

    def test_retired_tiers_are_inactive_after_seed(self):
        call_command('seed_pricing')
        for slug in _RETIRED:
            with self.subTest(slug=slug):
                tier = ServiceTier.objects.get(slug=slug)
                self.assertFalse(tier.is_active)

    def test_current_tiers_stay_active_after_seed(self):
        call_command('seed_pricing')
        for slug in _CURRENT:
            with self.subTest(slug=slug):
                tier = ServiceTier.objects.get(slug=slug)
                self.assertTrue(tier.is_active)

    def test_retirement_survives_a_re_seed(self):
        """The whole point — seed_pricing is safe to run any time, so a
        casual re-run must not silently resurrect a retired tier."""
        call_command('seed_pricing')
        # Simulate someone manually re-activating it, then re-seeding —
        # the second seed should put it back to retired, matching the
        # source-of-truth data in seed_pricing.py.
        ServiceTier.objects.filter(slug='maintenance-essentials').update(
            is_active=True)
        call_command('seed_pricing')
        tier = ServiceTier.objects.get(slug='maintenance-essentials')
        self.assertFalse(tier.is_active)

    def test_retired_tiers_excluded_from_add_plan_query(self):
        """The actual observable effect: Add Plan's tier query filters
        is_active=True, so a retired tier must not appear there."""
        call_command('seed_pricing')
        qs = ServiceTier.objects.filter(category='maintenance', is_active=True)
        slugs = set(qs.values_list('slug', flat=True))
        for slug in _RETIRED:
            self.assertNotIn(slug, slugs)
        for slug in _CURRENT:
            self.assertIn(slug, slugs)
