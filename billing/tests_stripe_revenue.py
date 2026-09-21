"""
Tests for the "Collected this year" dashboard figure — a Stripe-sourced,
account-wide total (unscoped by customer, so manually-charged clients
with no Account.stripe_customer_id link are still counted) refreshed
by a Celery beat sweep every 5 minutes into the StripeRevenueSync
singleton.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from billing.revenue_models import StripeRevenueSync
from billing.stripe_helpers import StripeNotConfigured, get_ytd_stripe_collected
from billing.tasks import sync_stripe_ytd_collected_task


def _charge(amount, refunded=0, status='succeeded'):
    return SimpleNamespace(
        amount=amount, amount_refunded=refunded, status=status)


class StripeRevenueSyncModelTests(TestCase):
    def test_singleton_pins_to_one_row(self):
        a = StripeRevenueSync.get()
        a.total_collected = Decimal('100.00')
        a.save()
        b = StripeRevenueSync.get()
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(b.total_collected, Decimal('100.00'))


@override_settings(STRIPE_SECRET_KEY='sk_test_dummy')
class GetYtdStripeCollectedTests(TestCase):
    def test_sums_succeeded_charges_net_of_refunds(self):
        charges = [
            _charge(250_00),
            _charge(100_00, refunded=25_00),   # net 75.00
            _charge(50_00, status='pending'),  # excluded — not succeeded
            _charge(0),                        # excluded — zero net
        ]
        with patch('billing.stripe_helpers.stripe.Charge.list') as mock_list:
            mock_list.return_value.auto_paging_iter.return_value = iter(charges)
            result = get_ytd_stripe_collected(as_of=timezone.now())

        self.assertEqual(result['total'], Decimal('325.00'))
        self.assertEqual(result['charge_count'], 2)

    def test_date_range_is_jan_1_to_as_of(self):
        as_of = timezone.now().replace(month=6, day=15)
        with patch('billing.stripe_helpers.stripe.Charge.list') as mock_list:
            mock_list.return_value.auto_paging_iter.return_value = iter([])
            get_ytd_stripe_collected(as_of=as_of)

        called_created = mock_list.call_args.kwargs['created']
        jan_1 = as_of.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        self.assertEqual(called_created['gte'], int(jan_1.timestamp()))
        self.assertEqual(called_created['lte'], int(as_of.timestamp()))

    @override_settings(STRIPE_SECRET_KEY='')
    def test_raises_when_not_configured(self):
        with self.assertRaises(StripeNotConfigured):
            get_ytd_stripe_collected()


class SyncStripeYtdCollectedTaskTests(TestCase):
    def test_writes_result_to_singleton(self):
        with patch('billing.stripe_helpers.get_ytd_stripe_collected') as mock_get:
            mock_get.return_value = {
                'year': 2026, 'total': Decimal('4200.00'), 'charge_count': 7,
            }
            sync_stripe_ytd_collected_task()

        row = StripeRevenueSync.get()
        self.assertEqual(row.year, 2026)
        self.assertEqual(row.total_collected, Decimal('4200.00'))
        self.assertEqual(row.charge_count, 7)
        self.assertIsNotNone(row.last_synced_at)
        self.assertEqual(row.last_error, '')

    def test_stripe_not_configured_records_error_without_raising(self):
        with patch('billing.stripe_helpers.get_ytd_stripe_collected',
                    side_effect=StripeNotConfigured('no key')):
            sync_stripe_ytd_collected_task()  # must not raise

        row = StripeRevenueSync.get()
        self.assertIn('no key', row.last_error)

    def test_generic_failure_keeps_last_good_total(self):
        row = StripeRevenueSync.get()
        row.total_collected = Decimal('999.00')
        row.year = 2026
        row.save()

        with patch('billing.stripe_helpers.get_ytd_stripe_collected',
                    side_effect=RuntimeError('stripe is down')):
            sync_stripe_ytd_collected_task()  # must not raise

        row.refresh_from_db()
        # Last-good figure is preserved — only the error field changes.
        self.assertEqual(row.total_collected, Decimal('999.00'))
        self.assertIn('stripe is down', row.last_error)
