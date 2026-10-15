"""
Checkout page: the mobile stacking bug and the duplicated bullet.

On a phone the order-summary card and the Stripe payment form rendered
on top of each other. The grid *did* collapse to one column at 880px —
the cause was `position: sticky` on `.checkout-summary`, which is a
two-column affordance. Stacked, it pins the card to the top of the
viewport and the form scrolls straight over it.

The CSS assertions below are deliberately structural rather than
pixel-based: there is no browser in this suite, so what can be checked
is that the override exists, is scoped to the stacked breakpoint, and
is declared *after* the base rule. That last one is the whole fix — a
media query carries no extra specificity, so the same block placed
above `position: sticky` would lose to it and change nothing.
"""

import re
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings

from billing.pricing_models import ServiceTier, TierFeature

CSS = Path(settings.BASE_DIR) / 'core' / 'static' / 'css' / 'main.css'


class CheckoutStacksOnMobileTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.css = CSS.read_text(encoding='utf-8')

    def test_the_grid_collapses_to_one_column(self):
        self.assertRegex(
            self.css,
            r'@media\s*\(max-width:\s*880px\)\s*\{[^}]*'
            r'\.checkout-grid\s*\{\s*grid-template-columns:\s*1fr')

    def test_the_summary_is_unstuck_at_the_stacked_breakpoint(self):
        """The actual fix. Without this the card stays pinned and the
        payment form scrolls over it."""
        block = self._mobile_block()
        self.assertIsNotNone(block, 'no max-width: 880px block found')
        self.assertIn('.checkout-summary', block)
        self.assertRegex(block, r'position:\s*static')

    def test_the_override_comes_after_the_base_sticky_rule(self):
        """Source order IS the fix. A media query adds no specificity,
        so an override placed above the base rule silently does
        nothing — which is exactly the mistake worth pinning."""
        base = self.css.index('position: sticky')
        override = self.css.index('position: static')
        self.assertLess(base, override, (
            'the stacked-layout override is declared before the base '
            'sticky rule, so it loses on source order and the card will '
            'pin on mobile again'))

    def _mobile_block(self):
        match = re.search(
            r'@media\s*\(max-width:\s*880px\)\s*\{(.*?)\n\}',
            self.css, re.DOTALL)
        return match.group(1) if match else None


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
