"""MaintenancePlan.current_monthly_price — the actual amount a plan is
charging right now (list price with any still-active discount applied),
used by clients.revenue.get_current_mrr so MRR reflects real recurring
revenue rather than rate-card price."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from billing.pricing_models import ServiceTier
from clients.account_models import Account
from clients.service_models import MaintenancePlan

User = get_user_model()

_seq = 0


def _account():
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'cmp{_seq}', email=f'cmp{_seq}@example.com', password='x')
    return Account.objects.create(user=u, name=f'CMP Co {_seq}')


class CurrentMonthlyPriceTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.tier = ServiceTier.objects.create(
            category='maintenance', name='Denis Custom',
            slug='maintenance-denis-custom', price=Decimal('350.00'),
            is_active=True, is_public=False)

    def test_no_discount_returns_list_price(self):
        plan = MaintenancePlan.objects.create(
            account=_account(), tier_slug='maintenance-denis-custom',
            status='active')
        self.assertEqual(plan.current_monthly_price, 350.0)

    def test_forever_discount_always_applies(self):
        plan = MaintenancePlan.objects.create(
            account=_account(), tier_slug='maintenance-denis-custom',
            status='active', discount_percent=15, discount_duration='forever',
            started_at=timezone.now() - timedelta(days=400))
        self.assertAlmostEqual(plan.current_monthly_price, 297.5, places=2)

    def test_once_discount_applies_within_first_30_days(self):
        plan = MaintenancePlan.objects.create(
            account=_account(), tier_slug='maintenance-denis-custom',
            status='active', discount_percent=15, discount_duration='once',
            started_at=timezone.now() - timedelta(days=5))
        self.assertAlmostEqual(plan.current_monthly_price, 297.5, places=2)

    def test_once_discount_expires_after_30_days(self):
        plan = MaintenancePlan.objects.create(
            account=_account(), tier_slug='maintenance-denis-custom',
            status='active', discount_percent=15, discount_duration='once',
            started_at=timezone.now() - timedelta(days=45))
        self.assertEqual(plan.current_monthly_price, 350.0)

    def test_unknown_tier_prices_zero(self):
        plan = MaintenancePlan.objects.create(
            account=_account(), tier_slug='nonexistent-tier',
            status='active')
        self.assertEqual(plan.current_monthly_price, 0.0)
