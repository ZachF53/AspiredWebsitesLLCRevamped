"""
Cover for the duplicate MaintenancePlan bug.

`plan_billing.start_website_plan` keyed its row on (account, website) and
committed the Stripe subscription id only at the END of the function —
after two blocking Stripe round-trips (finalize_invoice, send_invoice).
Stripe fires customer.subscription.created the instant the subscription
exists, so the webhook backstop ran during that gap, looked the plan up by
(account, stripe_subscription_id), found nothing, and created a SECOND
row with website=None for the same purchase.

Observed live on 2026-08-26: two rows 4 seconds apart, the website-less
one active with no discount, the real one stranded on awaiting_payment.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from billing.account_provisioning import (
    _drop_websiteless_twin,
    _plan_row_for_subscription,
    provision_self_checkout_account,
)
from clients.account_models import Account, Website
from clients.service_models import MaintenancePlan

User = get_user_model()

SUB = 'sub_race123'


class PlanRowResolution(TestCase):
    def setUp(self):
        user = User.objects.create_user(
            username='racer', email='racer@example.com',
            password='test-pass-123')
        self.account = Account.objects.filter(user=user).first() or (
            Account.objects.create(user=user, name='Racer Co'))
        self.account.websites.all().delete()
        self.website = Website.objects.create(
            account=self.account, name='Racer Site')

    def test_existing_subscription_row_wins(self):
        plan = MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='maintenance-essentials', stripe_subscription_id=SUB)
        found = _plan_row_for_subscription(
            MaintenancePlan, self.account, SUB, str(self.website.id))
        self.assertEqual(found.pk, plan.pk)

    def test_metadata_website_attaches_to_the_uncommitted_row(self):
        """The race itself: plan exists for the site, id not yet saved."""
        plan = MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='maintenance-essentials', stripe_subscription_id='')
        found = _plan_row_for_subscription(
            MaintenancePlan, self.account, SUB, str(self.website.id))
        self.assertEqual(found.pk, plan.pk)
        self.assertEqual(found.website_id, self.website.id)

    def test_sole_website_used_when_metadata_absent(self):
        plan = MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='maintenance-essentials', stripe_subscription_id='')
        found = _plan_row_for_subscription(
            MaintenancePlan, self.account, SUB, '')
        self.assertEqual(found.pk, plan.pk)

    def test_new_row_carries_the_website_not_none(self):
        found = _plan_row_for_subscription(
            MaintenancePlan, self.account, SUB, str(self.website.id))
        # TimestampedModel assigns the UUID pk at construction, so "not
        # yet created" means absent from the database, not pk is None.
        self.assertFalse(MaintenancePlan.objects.filter(pk=found.pk).exists())
        self.assertEqual(found.website_id, self.website.id)

    def test_ambiguous_multisite_account_stays_websiteless(self):
        Website.objects.create(account=self.account, name='Second Site')
        found = _plan_row_for_subscription(
            MaintenancePlan, self.account, SUB, '')
        self.assertIsNone(found.website_id)

    def test_drop_websiteless_twin_removes_the_duplicate(self):
        keep = MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='maintenance-essentials', stripe_subscription_id=SUB)
        MaintenancePlan.objects.create(
            account=self.account, website=None,
            tier_slug='maintenance-essentials', stripe_subscription_id=SUB)
        self.assertEqual(MaintenancePlan.objects.count(), 2)
        _drop_websiteless_twin(MaintenancePlan, self.account, SUB, keep)
        self.assertEqual(MaintenancePlan.objects.count(), 1)
        self.assertEqual(MaintenancePlan.objects.first().pk, keep.pk)

    def test_twin_kept_when_the_survivor_is_itself_websiteless(self):
        keep = MaintenancePlan.objects.create(
            account=self.account, website=None,
            tier_slug='maintenance-essentials', stripe_subscription_id=SUB)
        _drop_websiteless_twin(MaintenancePlan, self.account, SUB, keep)
        self.assertEqual(MaintenancePlan.objects.count(), 1)


class ProvisioningDoesNotDuplicate(TestCase):
    """End-to-end: the webhook backstop must not add a second row."""

    def setUp(self):
        user = User.objects.create_user(
            username='buyer', email='buyer@example.com',
            password='test-pass-123')
        self.account = Account.objects.filter(user=user).first() or (
            Account.objects.create(user=user, name='Buyer Co'))
        self.account.websites.all().delete()
        self.website = Website.objects.create(
            account=self.account, name='Buyer Site')

    def _provision(self, **kw):
        return provision_self_checkout_account(
            email='buyer@example.com', customer_id='cus_x',
            tier_slug='maintenance-essentials', product_type='maintenance',
            subscription_id=SUB, **kw)

    def test_webhook_arriving_mid_race_reuses_the_website_row(self):
        plan = MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='maintenance-essentials', stripe_subscription_id='',
            status='awaiting_payment', discount_percent=50,
            discount_duration='forever')

        self._provision(website_id=str(self.website.id))

        self.assertEqual(
            MaintenancePlan.objects.filter(account=self.account).count(), 1)
        plan.refresh_from_db()
        self.assertEqual(plan.stripe_subscription_id, SUB)
        self.assertEqual(plan.website_id, self.website.id)
        self.assertEqual(plan.status, 'active')

    def test_operator_discount_survives_the_backstop(self):
        plan = MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='maintenance-essentials', stripe_subscription_id=SUB,
            discount_percent=50, discount_duration='forever')

        self._provision(website_id=str(self.website.id))

        plan.refresh_from_db()
        self.assertEqual(plan.discount_percent, 50)
        self.assertEqual(plan.discount_duration, 'forever')

    def test_running_twice_is_idempotent(self):
        self._provision(website_id=str(self.website.id))
        self._provision(website_id=str(self.website.id))
        self.assertEqual(
            MaintenancePlan.objects.filter(account=self.account).count(), 1)


class ActivationHandlesEveryRow(TestCase):
    def setUp(self):
        user = User.objects.create_user(
            username='multi', email='multi@example.com',
            password='test-pass-123')
        self.account = Account.objects.filter(user=user).first() or (
            Account.objects.create(user=user, name='Multi Co'))
        self.account.websites.all().delete()
        self.website = Website.objects.create(
            account=self.account, name='Multi Site')

    def test_all_awaiting_rows_activate_and_site_flag_is_set(self):
        from billing.webhooks import _activate_website_plan_sub

        linked = MaintenancePlan.objects.create(
            account=self.account, website=self.website,
            tier_slug='maintenance-essentials', status='awaiting_payment',
            stripe_subscription_id=SUB)
        orphan = MaintenancePlan.objects.create(
            account=self.account, website=None,
            tier_slug='maintenance-essentials', status='awaiting_payment',
            stripe_subscription_id=SUB)

        self.assertTrue(_activate_website_plan_sub(SUB))

        linked.refresh_from_db()
        orphan.refresh_from_db()
        self.assertEqual(linked.status, 'active')
        self.assertEqual(orphan.status, 'active')
        self.website.refresh_from_db()
        self.assertTrue(
            self.website.maintenance_active,
            'site flag must be set even when a website-less row sorts first')
