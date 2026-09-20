"""Website.get_package_display() — regression for the v2 admin Overview
tab showing raw "maintenance_denis_custom" instead of "Denis Custom".

billing/account_provisioning.py's self-checkout maintenance path stamps
Website.package with the buyer's tier_slug (dashes -> underscores) for
ANY tier, including ones outside the hardcoded PACKAGE_CHOICES. Same
fallback pattern as MaintenancePlan/SocialMediaPlan.get_tier_slug_display.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from billing.pricing_models import ServiceTier
from clients.account_models import Account, Website

User = get_user_model()

_seq = 0


def _account():
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'pkgdisp{_seq}', email=f'pkgdisp{_seq}@example.com',
        password='x')
    return Account.objects.create(user=u, name=f'Pkg Display Co {_seq}')


class WebsitePackageDisplayTests(TestCase):

    def test_hardcoded_package_still_displays_via_static_choices(self):
        website = Website.objects.create(
            account=_account(), name='Static Pkg Site',
            build_platform='custom', package='premium_build')
        self.assertEqual(website.get_package_display(), 'Premium Website Build')

    def test_underscore_mangled_custom_tier_uses_service_tier_name(self):
        ServiceTier.objects.create(
            category='maintenance', name='Denis Custom',
            slug='maintenance-denis-custom', price=Decimal('350.00'),
            is_active=True, is_public=False)
        website = Website.objects.create(
            account=_account(), name='Custom Pkg Site',
            build_platform='custom', package='maintenance_denis_custom')
        self.assertEqual(website.get_package_display(), 'Denis Custom')
        self.assertNotIn('_', website.get_package_display())

    def test_blank_package_displays_blank(self):
        website = Website.objects.create(
            account=_account(), name='No Pkg Site', build_platform='custom')
        self.assertEqual(website.get_package_display(), '')

    def test_unknown_package_with_no_matching_tier_falls_back_to_raw(self):
        website = Website.objects.create(
            account=_account(), name='Unknown Pkg Site',
            build_platform='custom', package='totally_made_up')
        self.assertEqual(
            website.get_package_display(), 'totally_made_up')
