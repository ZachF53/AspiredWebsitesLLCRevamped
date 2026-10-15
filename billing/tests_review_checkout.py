"""
Provisioning a Review System sale: the wizard/plan-row split.

One value used to do two jobs. `_provision_product_type` chose both the
plan-row shape AND the onboarding wizard, which is fine until a product
needs a different one of each. The Review System does: it needs a
`MaintenancePlan` row (so entitlement works) but the `review_system`
wizard (because the maintenance wizard asks a reviews-only customer for
their hosting-panel password).

The failure mode if the split is done carelessly is expensive and quiet:
make `_provision_product_type` return `'review_system'` and the
`if product_type == 'maintenance'` branch in
`provision_self_checkout_account` stops firing, so someone who just paid
$97 gets no plan row, no entitlement, and a portal offering to sell it
to them again.

The backwards-compatibility test matters just as much. Subscriptions
created before the metadata key existed must still provision, because
the webhook backstop replays them.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from billing.account_provisioning import provision_self_checkout_account
from billing.pricing_models import ServiceTier
from clients.account_models import Account
from clients.service_models import MaintenancePlan
from onboarding.models import Onboarding
from reviews.models import ReviewProgram

User = get_user_model()

_seq = 0


def _review_tier():
    tier, _ = ServiceTier.objects.update_or_create(
        slug='hvac-review-system', defaults=dict(
            category='addon', name='Review System', price=Decimal('97.00'),
            is_recurring=True, billing_interval='month', is_active=True,
            stripe_price_id='price_fake_reviews'))
    return tier


def _hosting_tier():
    tier, _ = ServiceTier.objects.update_or_create(
        slug='hvac-hosting-security', defaults=dict(
            category='hosting', name='Hosting + Security Only',
            price=Decimal('45.00'), is_recurring=True,
            billing_interval='month', is_active=True,
            stripe_price_id='price_fake_hosting'))
    return tier


def _email():
    global _seq
    _seq += 1
    return f'rvcheckout{_seq}@example.com'


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ProductTypeSplitTests(TestCase):

    def test_the_plan_row_shape_is_unchanged_for_every_tier(self):
        from billing.checkout_views import _provision_product_type
        self.assertEqual(
            _provision_product_type(_review_tier()), 'maintenance')
        self.assertEqual(
            _provision_product_type(_hosting_tier()), 'maintenance')

    def test_only_the_review_tier_gets_the_review_wizard(self):
        from billing.checkout_views import _onboarding_product_type
        self.assertEqual(
            _onboarding_product_type(_review_tier()), 'review_system')
        self.assertEqual(
            _onboarding_product_type(_hosting_tier()), 'maintenance')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ReviewSelfCheckoutProvisioningTests(TestCase):

    def setUp(self):
        self.tier = _review_tier()
        self.email = _email()
        self.user = provision_self_checkout_account(
            email=self.email,
            customer_id='cus_fake_rv',
            tier_slug=self.tier.slug,
            product_type='maintenance',
            onboarding_product_type='review_system',
            subscription_id='sub_fake_rv',
            customer_name='Dale Johnson',
        )

    def test_a_user_and_account_are_created(self):
        self.assertIsNotNone(self.user)
        self.assertTrue(Account.objects.filter(user=self.user).exists())

    def test_the_plan_row_exists_so_entitlement_works(self):
        """The expensive failure: no plan row means the buyer is offered
        the product they just paid for."""
        from reviews.entitlement import review_entitlement
        account = Account.objects.get(user=self.user)
        plan = MaintenancePlan.objects.get(account=account)
        self.assertEqual(plan.tier_slug, 'hvac-review-system')
        self.assertEqual(plan.stripe_subscription_id, 'sub_fake_rv')

        ent = review_entitlement(account)
        self.assertTrue(ent.has_access)
        self.assertEqual(ent.source, 'standalone')
        self.assertFalse(ent.can_buy_standalone)

    def test_the_buyer_gets_the_review_wizard_not_the_maintenance_one(self):
        obs = list(Onboarding.objects.filter(user=self.user))
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0].product_type, 'review_system')
        self.assertEqual(obs[0].tier_slug, 'hvac-review-system')

    def test_the_review_program_is_created_ready_for_the_wizard(self):
        account = Account.objects.get(user=self.user)
        program = ReviewProgram.objects.get(account=account)
        self.assertEqual(program.setup_status, 'awaiting_onboarding')
        self.assertTrue(program.public_key)

    def test_no_website_is_created(self):
        """A reviews-only buyer is not a website-build client and must
        not land on build-only portal nav."""
        account = Account.objects.get(user=self.user)
        self.assertFalse(account.websites.exists())

    def test_running_it_again_is_idempotent(self):
        """The synchronous checkout path and the webhook backstop both
        call this."""
        provision_self_checkout_account(
            email=self.email,
            customer_id='cus_fake_rv',
            tier_slug=self.tier.slug,
            product_type='maintenance',
            onboarding_product_type='review_system',
            subscription_id='sub_fake_rv',
        )
        account = Account.objects.get(user=self.user)
        self.assertEqual(
            MaintenancePlan.objects.filter(account=account).count(), 1)
        self.assertEqual(Onboarding.objects.filter(user=self.user).count(), 1)
        self.assertEqual(
            ReviewProgram.objects.filter(account=account).count(), 1)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class BackwardsCompatibilityTests(TestCase):
    """A subscription created before `onboarding_product_type` existed
    has no value for it. The webhook backstop replays those, and they
    must behave exactly as they did before the split."""

    def test_omitting_the_new_argument_falls_back_to_product_type(self):
        _hosting_tier()
        email = _email()
        user = provision_self_checkout_account(
            email=email,
            customer_id='cus_fake_legacy',
            tier_slug='hvac-hosting-security',
            product_type='maintenance',
            subscription_id='sub_fake_legacy',
        )
        ob = Onboarding.objects.get(user=user)
        self.assertEqual(ob.product_type, 'maintenance')

    def test_a_hosting_sale_creates_no_review_program(self):
        _hosting_tier()
        user = provision_self_checkout_account(
            email=_email(),
            customer_id='cus_fake_h',
            tier_slug='hvac-hosting-security',
            product_type='maintenance',
            subscription_id='sub_fake_h',
        )
        account = Account.objects.get(user=user)
        self.assertFalse(ReviewProgram.objects.filter(
            account=account).exists())

    def test_an_explicit_none_also_falls_back(self):
        _hosting_tier()
        user = provision_self_checkout_account(
            email=_email(),
            customer_id='cus_fake_n',
            tier_slug='hvac-hosting-security',
            product_type='maintenance',
            onboarding_product_type=None,
            subscription_id='sub_fake_n',
        )
        self.assertEqual(
            Onboarding.objects.get(user=user).product_type, 'maintenance')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class CheckoutGatingTests(TestCase):

    def test_the_review_tier_is_self_checkoutable(self):
        from billing.checkout_views import _tier_allows_selfcheckout
        self.assertTrue(_tier_allows_selfcheckout(_review_tier()))

    def test_an_annual_domain_addon_is_not(self):
        """The allowlist is by slug precisely so 'addon' did not become a
        blanket grant to the yearly domain tiers."""
        from billing.checkout_views import _tier_allows_selfcheckout
        domain, _ = ServiceTier.objects.update_or_create(
            slug='domain-standard', defaults=dict(
                category='addon', name='Domain Registration',
                price=Decimal('75.00'), is_recurring=True,
                billing_interval='year', is_active=True))
        self.assertFalse(_tier_allows_selfcheckout(domain))

    def test_checkout_refuses_a_tier_with_no_stripe_price(self):
        """The tier ships deliberately unsynced. Until a Price ID exists
        nobody can be charged, which is the correct behaviour, not a bug
        to work around."""
        tier = _review_tier()
        tier.stripe_price_id = ''
        tier.save(update_fields=['stripe_price_id'])
        resp = self.client.post(
            f'/billing/checkout/{tier.slug}/confirm/',
            data='{"email": "x@example.com", "payment_method_id": "pm_1"}',
            content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Stripe price', resp.json()['error'])
