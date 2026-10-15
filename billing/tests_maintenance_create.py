"""
Regression: the create and change-tier paths crashed on every Account.

`create_maintenance_subscription` read `client.stripe_subscription_id`
and wrote `client.package`; `change_maintenance_subscription_tier` and
`_sync_local_maintenance_after_change` did the same, plus a
`client.migrated_account` fallback. **`Account` has none of those three**
— not as a field, not as a property. They moved to `MaintenancePlan` and
`Website` when Account became canonical, and the cancel path was fixed
accordingly (`_maintenance_sub_id` / `_maintenance_plan_for`) while
these two were missed.

The effect was not subtle: the portal's "Upgrade to the Full Plan"
button 500'd for every Account-based client. It was masked for a while
because `_MAINTENANCE_TIER_SLUGS` rejected the HVAC slugs before the
code could get far enough to crash.

Stripe is fully mocked here. The assertion that matters is "no
AttributeError", so these must keep calling the real helper rather than
a reimplementation.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from billing.pricing_models import ServiceTier
from clients.account_models import Account, Website
from clients.service_models import MaintenancePlan

User = get_user_model()

_seq = 0


def _account(with_website=True):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'mcreate{_seq}', email=f'mcreate{_seq}@example.com',
        password='test-pass-123')
    account = Account.objects.create(
        user=user, name=f'Create Co {_seq}',
        stripe_customer_id=f'cus_fake_{_seq}')
    if with_website:
        Website.objects.create(account=account, name=f'Create Site {_seq}')
    return account


def _tier(slug='hvac-plan-paid-in-full', price='145.00'):
    tier, _ = ServiceTier.objects.update_or_create(
        slug=slug, defaults=dict(
            category='maintenance', name='Full Plan: Build Paid Upfront',
            price=Decimal(price), is_recurring=True,
            billing_interval='month', is_active=True,
            stripe_price_id=f'price_fake_{slug}'))
    return tier


def _stripe_mock(sub_id='sub_fake_new', status='active'):
    """A stripe module stand-in.

    Both create and retrieve return ATTRIBUTE-style objects, not dicts:
    the helper reads `existing.id` and `getattr(existing, 'status', '')`,
    and a dict there raises AttributeError inside the broad `except`,
    which silently turns a short-circuit into a second subscription. A
    real Stripe object supports attribute access, so the mock must too.
    """
    mock = MagicMock()
    mock.Subscription.create.return_value = MagicMock(id=sub_id)
    mock.Subscription.retrieve.return_value = MagicMock(
        id=sub_id, status=status)
    return mock


@override_settings(STRIPE_SECRET_KEY='sk_test_fake')
class CreateMaintenanceSubscriptionTests(TestCase):

    def setUp(self):
        self.account = _account()
        self.tier = _tier()

    def _create(self):
        from billing import stripe_helpers
        with patch.object(stripe_helpers, 'stripe', _stripe_mock()), \
                patch.object(stripe_helpers, '_init'), \
                patch.object(stripe_helpers, 'create_or_get_customer',
                             return_value=MagicMock(id='cus_fake_1')), \
                patch.object(stripe_helpers,
                             'get_customer_default_payment_method',
                             return_value='pm_fake_1'):
            return stripe_helpers.create_maintenance_subscription(
                self.account, self.tier.slug)

    def test_it_does_not_raise_attribute_error_on_an_account(self):
        # Before the fix this raised AttributeError on the first line
        # that touched client.stripe_subscription_id.
        self._create()

    def test_it_writes_the_plan_row(self):
        self._create()
        plan = MaintenancePlan.objects.get(account=self.account)
        self.assertEqual(plan.tier_slug, self.tier.slug)
        self.assertEqual(plan.status, 'active')
        self.assertEqual(plan.stripe_subscription_id, 'sub_fake_new')
        self.assertIsNotNone(plan.started_at)

    def test_it_mirrors_onto_a_single_website(self):
        self._create()
        site = self.account.websites.get()
        self.assertTrue(site.maintenance_active)
        self.assertEqual(site.package, 'hvac_plan_paid_in_full')
        self.assertEqual(
            site.stripe_maintenance_subscription_id, 'sub_fake_new')

    def test_it_leaves_a_multi_site_account_for_a_human(self):
        """With several sites there is no way to tell which the plan
        covers, so guessing the oldest would be worse than not guessing."""
        Website.objects.create(account=self.account, name='Second Site')
        self._create()
        for site in self.account.websites.all():
            self.assertFalse(site.maintenance_active)
        # The plan row is still written — that is the canonical record.
        self.assertTrue(
            MaintenancePlan.objects.filter(account=self.account).exists())

    def test_an_account_with_no_website_still_gets_a_plan_row(self):
        self.account = _account(with_website=False)
        self._create()
        self.assertTrue(
            MaintenancePlan.objects.filter(account=self.account).exists())

    def test_an_existing_active_subscription_short_circuits(self):
        from billing import stripe_helpers
        MaintenancePlan.objects.create(
            account=self.account, tier_slug=self.tier.slug, status='active',
            stripe_subscription_id='sub_already_there')
        mock = _stripe_mock()
        with patch.object(stripe_helpers, 'stripe', mock), \
                patch.object(stripe_helpers, '_init'), \
                patch.object(stripe_helpers, 'create_or_get_customer',
                             return_value=MagicMock(id='cus_fake_1')):
            stripe_helpers.create_maintenance_subscription(
                self.account, self.tier.slug)
        mock.Subscription.create.assert_not_called()

    def test_an_operator_discount_survives_the_write(self):
        """Field-by-field then save(), never update_or_create(defaults=),
        so a negotiated discount is not clobbered."""
        MaintenancePlan.objects.create(
            account=self.account, tier_slug=self.tier.slug,
            status='awaiting_payment', discount_percent=20,
            discount_duration='forever', stripe_subscription_id='')
        self._create()
        plan = MaintenancePlan.objects.get(account=self.account)
        self.assertEqual(plan.discount_percent, 20)
        self.assertEqual(plan.discount_duration, 'forever')


@override_settings(STRIPE_SECRET_KEY='sk_test_fake')
class ChangeMaintenanceTierTests(TestCase):

    def setUp(self):
        self.account = _account()
        self.current = _tier('hvac-full-plan', '250.00')
        self.current.name = 'Full Plan: Build Financed'
        self.current.save(update_fields=['name'])
        self.target = _tier()
        self.plan = MaintenancePlan.objects.create(
            account=self.account, tier_slug=self.current.slug,
            status='active', stripe_subscription_id='sub_existing')

    def _change(self, direction='upgrade', effective_ts=None):
        from billing import stripe_helpers
        result = {'direction': direction, 'effective_ts': effective_ts}
        with patch.object(stripe_helpers, 'stripe', _stripe_mock()), \
                patch.object(stripe_helpers, '_init'), \
                patch.object(stripe_helpers, '_change_subscription_tier',
                             return_value=result):
            return stripe_helpers.change_maintenance_subscription_tier(
                self.account, self.target.slug)

    def test_an_upgrade_does_not_raise_attribute_error(self):
        self._change('upgrade')

    def test_an_upgrade_applies_the_new_tier_immediately(self):
        self._change('upgrade')
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.tier_slug, self.target.slug)
        self.assertEqual(self.plan.pending_tier_slug, '')

    def test_a_downgrade_queues_rather_than_applying(self):
        import time
        self._change('downgrade', effective_ts=int(time.time()) + 86400)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.tier_slug, self.current.slug)
        self.assertEqual(self.plan.pending_tier_slug, self.target.slug)
        self.assertIsNotNone(self.plan.pending_tier_effective)

    def test_a_queued_downgrade_renders_its_real_name(self):
        """The whole point of recording it — the portal shows
        "switching to X on Y"."""
        import time
        self._change('downgrade', effective_ts=int(time.time()) + 86400)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.pending_tier_display,
                         'Full Plan: Build Paid Upfront')

    def test_no_subscription_at_all_raises_a_clear_value_error(self):
        from billing import stripe_helpers
        self.plan.stripe_subscription_id = ''
        self.plan.save(update_fields=['stripe_subscription_id'])
        with patch.object(stripe_helpers, '_init'):
            with self.assertRaises(ValueError):
                stripe_helpers.change_maintenance_subscription_tier(
                    self.account, self.target.slug)


@override_settings(STRIPE_SECRET_KEY='sk_test_fake')
class CreateAddonSubscriptionTests(TestCase):
    """The portal's Review System signup. Separate from the maintenance
    helper because `get_maintenance_tier` filters category='maintenance'
    and would never find an add-on."""

    def setUp(self):
        self.account = _account()
        self.tier, _ = ServiceTier.objects.update_or_create(
            slug='hvac-review-system', defaults=dict(
                category='addon', name='Review System',
                price=Decimal('97.00'), is_recurring=True,
                billing_interval='month', is_active=True,
                stripe_price_id='price_fake_reviews'))

    def _create(self):
        from billing import stripe_helpers
        with patch.object(stripe_helpers, 'stripe',
                          _stripe_mock('sub_fake_addon')), \
                patch.object(stripe_helpers, '_init'), \
                patch.object(stripe_helpers, 'create_or_get_customer',
                             return_value=MagicMock(id='cus_fake_1')), \
                patch.object(stripe_helpers,
                             'get_customer_default_payment_method',
                             return_value='pm_fake_1'):
            return stripe_helpers.create_addon_subscription(
                self.account, self.tier.slug)

    def test_it_creates_a_plan_row_carrying_the_addon_slug(self):
        self._create()
        plan = MaintenancePlan.objects.get(
            account=self.account, tier_slug='hvac-review-system')
        self.assertEqual(plan.stripe_subscription_id, 'sub_fake_addon')
        self.assertEqual(plan.status, 'active')

    def test_the_row_is_website_less(self):
        """A reviews-only client may have no site of ours at all."""
        self._create()
        plan = MaintenancePlan.objects.get(tier_slug='hvac-review-system')
        self.assertIsNone(plan.website_id)

    def test_it_grants_entitlement(self):
        from reviews.entitlement import review_entitlement
        self._create()
        ent = review_entitlement(self.account)
        self.assertTrue(ent.has_access)
        self.assertEqual(ent.source, 'standalone')

    def test_buying_twice_does_not_double_bill(self):
        from billing import stripe_helpers
        self._create()
        mock = _stripe_mock('sub_fake_addon')
        with patch.object(stripe_helpers, 'stripe', mock), \
                patch.object(stripe_helpers, '_init'), \
                patch.object(stripe_helpers, 'create_or_get_customer',
                             return_value=MagicMock(id='cus_fake_1')), \
                patch.object(stripe_helpers,
                             'get_customer_default_payment_method',
                             return_value='pm_fake_1'):
            stripe_helpers.create_addon_subscription(
                self.account, self.tier.slug)
        mock.Subscription.create.assert_not_called()

    def test_a_maintenance_slug_is_refused(self):
        from billing import stripe_helpers
        _tier()
        with patch.object(stripe_helpers, '_init'):
            with self.assertRaises(ValueError):
                stripe_helpers.create_addon_subscription(
                    self.account, 'hvac-plan-paid-in-full')

    def test_a_tier_with_no_stripe_price_is_refused_before_charging(self):
        from billing import stripe_helpers
        self.tier.stripe_price_id = ''
        self.tier.save(update_fields=['stripe_price_id'])
        with patch.object(stripe_helpers, '_init'):
            with self.assertRaises(ValueError):
                stripe_helpers.create_addon_subscription(
                    self.account, self.tier.slug)
