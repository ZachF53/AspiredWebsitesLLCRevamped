"""
Regression: the portal rendered plan buttons its own view then rejected.

`portal_maintenance.html` renders a Subscribe/Switch button for every
tier `_maintenance_tiers` returns — which includes `hvac-full-plan` and
`hvac-plan-paid-in-full` — but `portal_maintenance_start` gated on a
hardcoded three-slug tuple of the *legacy* `maintenance-*` plans.
Clicking any HVAC button came back with "Unknown maintenance plan."

The allowlist still exists (it is what stops arbitrary slug injection in
the URL) but is now derived from the same helper the buttons come from,
so a new maintenance tier can never again render a button that bounces.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.pricing_models import ServiceTier
from clients.account_models import Account, Website

User = get_user_model()

_seq = 0


def _logged_in_client(test):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'pslug{_seq}', email=f'pslug{_seq}@example.com',
        password='test-pass-123')
    account = Account.objects.create(
        user=user, name=f'Slug Co {_seq}', onboarding_status='complete',
        stripe_customer_id='')
    Website.objects.create(
        account=account, name=f'Slug Site {_seq}',
        onboarding_status='complete')
    test.client.force_login(user)
    return account


def _tier(slug, name, price, category='maintenance', **extra):
    defaults = dict(
        category=category, name=name, price=Decimal(price),
        is_recurring=True, billing_interval='month', is_active=True,
        stripe_price_id=f'price_fake_{slug}')
    defaults.update(extra)
    tier, _ = ServiceTier.objects.update_or_create(
        slug=slug, defaults=defaults)
    return tier


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class PortalPlanSlugAllowlistTests(TestCase):

    def setUp(self):
        self.account = _logged_in_client(self)

    def _start(self, slug):
        return self.client.get(
            reverse('clients:portal_maintenance_start', args=[slug]),
            follow=True)

    def test_the_hvac_full_plan_is_accepted(self):
        _tier('hvac-plan-paid-in-full', 'Full Plan: Build Paid Upfront',
              '145.00')
        resp = self._start('hvac-plan-paid-in-full')
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'Unknown maintenance plan')

    def test_a_legacy_maintenance_tier_is_still_accepted(self):
        _tier('maintenance-growth', 'Growth', '599.00')
        resp = self._start('maintenance-growth')
        self.assertNotContains(resp, 'Unknown maintenance plan')

    def test_an_arbitrary_slug_is_still_rejected(self):
        """The allowlist exists to stop slug injection, and still does."""
        resp = self._start('definitely-not-a-plan')
        self.assertContains(resp, 'Unknown maintenance plan')

    def test_the_review_system_addon_is_not_accepted_here(self):
        """It is category='addon' with its own portal view and its own
        Stripe helper — `get_maintenance_tier` would reject it anyway,
        and routing it here would be the wrong purchase flow."""
        _tier('hvac-review-system', 'Review System', '97.00',
              category='addon')
        resp = self._start('hvac-review-system')
        self.assertContains(resp, 'Unknown maintenance plan')

    def test_a_non_public_tier_is_rejected_unless_granted_to_this_account(self):
        hidden = _tier('maintenance-negotiated-xyz', 'Negotiated', '350.00',
                       is_public=False)
        self.assertContains(
            self._start(hidden.slug), 'Unknown maintenance plan')

        # visible_plan_tiers is how a one-off negotiated plan is shown to
        # exactly one client; the allowlist has to honour that too, or
        # the button appears and the destination refuses it.
        self.account.visible_plan_tiers.add(hidden)
        self.assertNotContains(
            self._start(hidden.slug), 'Unknown maintenance plan')

    def test_the_success_page_names_an_hvac_tier(self):
        tier = _tier('hvac-plan-paid-in-full',
                     'Full Plan: Build Paid Upfront', '145.00')
        resp = self.client.get(
            reverse('clients:portal_maintenance_success'),
            {'tier': tier.slug}, follow=True)
        self.assertContains(resp, tier.name)
