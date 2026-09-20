"""
clients.revenue._price_for_package / get_current_mrr — regression for
MRR silently under-reporting.

Website.package stores a slug-shaped string (e.g.
'maintenance_denis_custom', dashes converted to underscores by
billing.account_provisioning), not a ServiceTier NAME. The old
`name__iexact` lookup only ever matched a tier whose human name
happened to equal that raw string verbatim — never true for HVAC-era
or operator-custom tiers — so every one of those priced at $0 in MRR.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from billing.pricing_models import ServiceTier
from clients.account_models import Account, Website
from clients.revenue import _price_for_package, get_current_mrr
from clients.service_models import MaintenancePlan

User = get_user_model()

_seq = 0


def _account_and_website(package):
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'mrv{_seq}', email=f'mrv{_seq}@example.com', password='x')
    account = Account.objects.create(
        user=u, name=f'MRR Co {_seq}', status='active', is_tester=False)
    website = Website.objects.create(
        account=account, name=f'MRR Site {_seq}', build_platform='custom',
        status='active', package=package, maintenance_active=True)
    return account, website


class PriceForPackageTests(TestCase):

    def test_legacy_underscore_package_still_prices_via_fallback(self):
        # No matching ServiceTier row at all — falls through to the
        # hardcoded _FALLBACK_PRICES dict, same as before this fix.
        self.assertEqual(_price_for_package('maintenance_essentials'), 299)

    def test_custom_tier_prices_via_slug_lookup(self):
        ServiceTier.objects.create(
            category='maintenance', name='Denis Custom',
            slug='maintenance-denis-custom', price=Decimal('350.00'),
            is_active=True, is_public=False)
        self.assertEqual(
            _price_for_package('maintenance_denis_custom'), 350.0)

    def test_hvac_tier_prices_via_slug_lookup(self):
        ServiceTier.objects.create(
            category='maintenance', name='Full Plan',
            slug='hvac-full-plan', price=Decimal('250.00'),
            is_active=True, is_public=True)
        self.assertEqual(_price_for_package('hvac_full_plan'), 250.0)

    def test_unknown_package_prices_zero(self):
        self.assertEqual(_price_for_package('totally_made_up'), 0)

    def test_blank_package_prices_zero(self):
        self.assertEqual(_price_for_package(''), 0)


class GetCurrentMrrIncludesCustomTiersTests(TestCase):
    """No MaintenancePlan row exists for these sites — exercises the
    Website.package fallback path only."""

    def test_custom_tier_website_counts_toward_mrr(self):
        ServiceTier.objects.create(
            category='maintenance', name='Denis Custom',
            slug='maintenance-denis-custom', price=Decimal('350.00'),
            is_active=True, is_public=False)
        _account_and_website('maintenance_denis_custom')

        result = get_current_mrr()
        self.assertEqual(result['mrr_total'], 350.0)
        self.assertEqual(result['active_maintenance_clients'], 1)


class GetCurrentMrrUsesPlanDiscountTests(TestCase):
    """A real MaintenancePlan row exists — MRR must reflect the
    discounted amount actually being charged, not the tier's list
    price. This is the normal, common case going forward."""

    def test_forever_discount_reflected_in_mrr(self):
        ServiceTier.objects.create(
            category='maintenance', name='Denis Custom',
            slug='maintenance-denis-custom', price=Decimal('350.00'),
            is_active=True, is_public=False)
        account, website = _account_and_website('maintenance_denis_custom')
        MaintenancePlan.objects.create(
            account=account, website=website,
            tier_slug='maintenance-denis-custom', status='active',
            discount_percent=15, discount_duration='forever')

        result = get_current_mrr()
        self.assertAlmostEqual(result['mrr_total'], 297.5, places=2)
        self.assertEqual(result['breakdown'][0]['plan'], 'Denis Custom')
