"""
Checkout page: the mobile stacking bug and the duplicated bullet.

On a phone the order-summary card and the Stripe payment form painted
on top of each other. The grid *did* collapse to one column at 880px;
the cause was `position: sticky` on `.checkout-summary`, a two-column
affordance. The card is taller than a phone screen, so stacked it
pinned itself to the viewport and the form scrolled over its text.

The first fix un-stuck it only below 880px and still shipped broken,
because `public.css` — the generated main.css subset that the public
checkout page actually loads — was never rebuilt and kept serving the
old rule. So these tests check BOTH bundles, and check for the absence
of `position` rather than for the presence of an override: there is no
width at which this card should be stuck, and an absent property
cannot be beaten on source order by a stale copy.
"""

import re
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings

from billing.pricing_models import ServiceTier, TierFeature

CSS_DIR = Path(settings.BASE_DIR) / 'core' / 'static' / 'css'


class CheckoutStacksOnMobileTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.bundles = {
            name: (CSS_DIR / name).read_text(encoding='utf-8')
            for name in ('main.css', 'public.css')
        }

    def test_the_grid_collapses_to_one_column(self):
        for name, css in self.bundles.items():
            with self.subTest(bundle=name):
                self.assertRegex(
                    css,
                    r'@media\s*\(max-width:\s*880px\)\s*\{[^}]*'
                    r'\.checkout-grid\s*\{\s*grid-template-columns:\s*1fr',
                    f'{name} does not stack the checkout grid')

    def test_the_summary_is_never_positioned(self):
        """The whole fix. `position: sticky` at any width lets the
        payment form scroll over the summary card on a phone."""
        for name, css in self.bundles.items():
            for body in self._summary_rules(css):
                with self.subTest(bundle=name, rule=body[:60]):
                    self.assertNotRegex(
                        body, r'position\s*:',
                        f'.checkout-summary carries a position property '
                        f'in {name}; the card must simply flow')

    def test_public_bundle_actually_contains_the_rule(self):
        """Guards the assertion above from passing vacuously — if the
        generator ever stops emitting .checkout-summary, the
        never-positioned test would succeed on an empty list."""
        self.assertTrue(
            self._summary_rules(self.bundles['public.css']),
            'public.css has no .checkout-summary rule at all; the '
            'checkout page is on the public bundle and would render '
            'the card unstyled')

    @staticmethod
    def _summary_rules(css):
        """Every rule body whose selector list includes .checkout-summary
        exactly — not .checkout-summary__title and friends."""
        return [
            match.group(1)
            for match in re.finditer(
                r'\.checkout-summary\s*(?:,[^{]*)?\{([^}]*)\}', css)
        ]


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class MonthlyNoteIsNotDuplicatedTests(TestCase):
    """The summary card appends its own "Month-to-month" bullet for
    recurring tiers. Several tiers already say it in their own feature
    text, which printed the same promise twice in a row."""

    # Category matters here: self-checkout is granted to ANY maintenance
    # tier but to hosting and addon tiers only by explicit slug
    # allowlist, so an invented slug in those categories 302s to the
    # schedule-a-call page. The generic cases therefore use
    # 'maintenance'; the real allowlisted addon is covered by
    # test_the_real_review_system_tier_says_it_exactly_once below.
    def _tier(self, slug, features, category='maintenance'):
        tier, _ = ServiceTier.objects.update_or_create(
            slug=slug, defaults=dict(
                category=category, name='Test Tier', price=Decimal('97.00'),
                is_recurring=True, billing_interval='month', is_active=True,
                stripe_price_id=f'price_fake_{slug}'))
        tier.features.all().delete()
        for index, text in enumerate(features, start=1):
            TierFeature.objects.create(tier=tier, text=text, sort_order=index)
        return tier

    def _flag(self, tier):
        resp = self.client.get(f'/billing/checkout/{tier.slug}/')
        self.assertEqual(resp.status_code, 200)
        return resp.context['show_monthly_note'], resp

    def test_suppressed_when_the_tier_already_says_it(self):
        tier = self._tier('layout-says-monthly',
                          ['Month-to-month: cancel anytime'])
        show, resp = self._flag(tier)
        self.assertFalse(show)
        body = resp.content.decode().lower()
        self.assertEqual(body.count('month-to-month'), 1, (
            'the checkout summary printed the month-to-month promise '
            'more than once'))

    def test_suppressed_for_the_other_wording_too(self):
        tier = self._tier('layout-says-monthly-2',
                          ['Month-to-month from day one: cancel anytime'])
        show, _ = self._flag(tier)
        self.assertFalse(show)

    def test_still_shown_when_the_tier_is_silent_about_it(self):
        """The bullet exists for a reason — don't lose it for tiers
        whose features never mention the terms."""
        tier = self._tier('layout-silent', ['Server and OS patching'])
        show, resp = self._flag(tier)
        self.assertTrue(show)
        self.assertContains(resp, 'Month-to-month')

    def test_the_real_review_system_tier_says_it_exactly_once(self):
        from django.core.management import call_command
        call_command('seed_pricing')
        tier = ServiceTier.objects.get(slug='hvac-review-system')
        tier.stripe_price_id = 'price_fake_reviews'
        tier.save(update_fields=['stripe_price_id'])
        resp = self.client.get(f'/billing/checkout/{tier.slug}/')
        self.assertEqual(
            resp.content.decode().lower().count('month-to-month'), 1)
