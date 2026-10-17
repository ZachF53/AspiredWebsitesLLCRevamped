"""
The AI Voice Receptionist is never bundled into any plan, at any price.

This is a commercial rule, not a technical one, which is exactly why it
needs a test: nothing about the code would break if someone added the
slug to ``FULL_PLAN_SLUGS``. The site would keep working. It would just
start giving away a product that bills real per-minute vendor cost on
every call, inside a $145 plan priced on the assumption that it doesn't.

The Review System is the deliberate contrast and is asserted here too —
it IS bundled, and that asymmetry is the thing a future reader is most
likely to "tidy up" into consistency.

If a later decision genuinely is to bundle the receptionist, these tests
should be deleted in the same commit that reprices the Full Plan. Failing
them is the prompt to have that conversation.
"""

from django.core.management import call_command
from django.test import TestCase, override_settings

from billing.checkout_views import (
    SELF_CHECKOUT_ADDON_SLUGS, _onboarding_product_type,
    _provision_product_type,
)
from billing.pricing_models import ServiceTier
from clients import contract_options as opts
from clients.views import _maintenance_tiers, _portal_plan_slugs

SLUG = 'hvac-voice-receptionist'


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class VoiceReceptionistIsNeverBundledTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_slug_constant_matches(self):
        self.assertEqual(opts.VOICE_RECEPTIONIST_SLUG, SLUG)

    def test_absent_from_full_plan_slugs(self):
        self.assertNotIn(SLUG, opts.FULL_PLAN_SLUGS, (
            'the receptionist was added to FULL_PLAN_SLUGS, which makes '
            'every Full Plan client entitled to it for free'))

    def test_absent_from_slug_to_package(self):
        self.assertNotIn(SLUG, opts.SLUG_TO_PACKAGE, (
            'the receptionist now maps to a Website.package code, so a '
            'contract would record it as part of the plan'))

    def test_absent_from_every_bundled_packages_tuple(self):
        """Catches a new ``*_BUNDLED_PACKAGES`` tuple as well as the
        existing review one, so this does not need updating when another
        bundled product is added."""
        bundled = {
            name: value for name, value in vars(opts).items()
            if name.endswith('_BUNDLED_PACKAGES')
        }
        self.assertTrue(bundled, 'expected at least the review tuple')
        for name, value in bundled.items():
            self.assertNotIn(SLUG, value, f'{SLUG} leaked into {name}')
            self.assertNotIn(SLUG.replace('-', '_'), value,
                             f'{SLUG} leaked into {name} as a package code')

    def test_tier_is_an_addon_not_a_maintenance_plan(self):
        tier = ServiceTier.objects.get(slug=SLUG)
        self.assertEqual(tier.category, 'addon', (
            "category='maintenance' would put the receptionist in the "
            'portal plan chooser, which is a SWITCH surface — a Full Plan '
            'client could switch down to it and silently lose hosting'))
        self.assertTrue(tier.is_recurring)
        self.assertEqual(tier.billing_interval, 'month')

    def test_absent_from_the_portal_plan_chooser(self):
        self.assertNotIn(
            SLUG, [t.slug for t in _maintenance_tiers()],
            'the receptionist appears in the portal plan chooser')
        self.assertNotIn(SLUG, _portal_plan_slugs())

    def test_no_plan_tier_lists_it_as_an_included_feature(self):
        """The real-world version of this bug is copy, not code: a feature
        bullet on the Full Plan saying 'AI receptionist included'."""
        plan_tiers = ServiceTier.objects.filter(
            category__in=('maintenance', 'hosting'))
        self.assertTrue(plan_tiers.exists())
        for tier in plan_tiers:
            for feature in tier.features.all():
                text = feature.text.lower()
                self.assertFalse(
                    'receptionist' in text
                    or ('voice' in text and 'ai' in text),
                    f'{tier.slug} advertises the receptionist as included: '
                    f'"{feature.text}"')
            self.assertNotIn('receptionist', tier.description.lower(),
                             f'{tier.slug} description includes it')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class VoiceReceptionistIsStillBuyableTests(TestCase):
    """Never-bundled must not have been achieved by making it unsellable."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_self_checkout_is_allowed(self):
        self.assertIn(SLUG, SELF_CHECKOUT_ADDON_SLUGS)

    def test_product_type_split_is_respected(self):
        """The plan-row shape stays 'maintenance' while the wizard is the
        voice one. Collapsing these two was the bug the review system hit:
        the buyer gets no plan row, so no entitlement, so the portal
        offers to sell it to them again."""
        tier = ServiceTier.objects.get(slug=SLUG)
        self.assertEqual(_provision_product_type(tier), 'maintenance')
        self.assertEqual(_onboarding_product_type(tier), 'voice_receptionist')

    def test_review_system_remains_bundled(self):
        """The contrast. If this ever fails, the two products' rules have
        been accidentally unified."""
        self.assertIn('hvac_full_plan', opts.REVIEWS_BUNDLED_PACKAGES)
        self.assertNotIn(opts.REVIEW_SYSTEM_SLUG, opts.SLUG_TO_PACKAGE)
