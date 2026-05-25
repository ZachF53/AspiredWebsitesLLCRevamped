"""Unit tests for the domains app — no live Namecheap calls."""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from billing.pricing_models import ServiceTier, TierFeature
from clients.models import ClientProfile
from domains.models import (
    DomainRegistration,
    PREMIUM_TLDS,
    tier_slug_for_tld,
)
from domains.services import registrant_from_client

User = get_user_model()


@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='test',
    NAMECHEAP_API_KEY='test',
    NAMECHEAP_USERNAME='test',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class DomainModelTests(TestCase):
    def test_tier_slug_for_tld_returns_law_for_premium(self):
        self.assertEqual(tier_slug_for_tld('law'), 'domain-law')

    def test_tier_slug_for_tld_returns_standard_for_others(self):
        for tld in ('com', 'net', 'org', 'legal', 'attorney'):
            self.assertEqual(
                tier_slug_for_tld(tld), 'domain-standard',
                f'{tld} should be standard')

    def test_premium_tlds_only_contains_law(self):
        self.assertEqual(PREMIUM_TLDS, frozenset({'law'}))

    def test_decrypt_epp_code_when_unset_returns_empty(self):
        reg = DomainRegistration(domain_name='x.com', tld='com')
        self.assertEqual(reg.decrypt_epp_code(), '')


class RegistrantBuilderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='u1', email='client@example.com',
            password='x')
        self.client_profile = ClientProfile.objects.create(
            user=self.user,
            firm_name='Smith Law Firm',
            contact_name='Jane Smith',
            address='123 Main St',
            city='Austin', state='TX', zip_code='78701',
            phone='(512) 555-1212',
        )

    def test_registrant_from_complete_profile_builds_dict(self):
        reg = registrant_from_client(self.client_profile)
        self.assertEqual(reg['first_name'], 'Jane')
        self.assertEqual(reg['last_name'], 'Smith')
        self.assertEqual(reg['organization_name'], 'Smith Law Firm')
        self.assertEqual(reg['address1'], '123 Main St')
        self.assertEqual(reg['city'], 'Austin')
        self.assertEqual(reg['state_province'], 'TX')
        self.assertEqual(reg['postal_code'], '78701')
        self.assertEqual(reg['country'], 'US')
        # Phone should be normalized to +1.NNNNNNNNNN
        self.assertTrue(reg['phone'].startswith('+1.'))
        self.assertEqual(reg['email_address'], 'client@example.com')

    def test_registrant_missing_address_raises(self):
        self.client_profile.address = ''
        self.client_profile.save()
        with self.assertRaises(ValueError) as ctx:
            registrant_from_client(self.client_profile)
        self.assertIn('street address', str(ctx.exception))

    def test_registrant_missing_phone_raises(self):
        self.client_profile.phone = ''
        self.client_profile.save()
        with self.assertRaises(ValueError) as ctx:
            registrant_from_client(self.client_profile)
        self.assertIn('phone', str(ctx.exception))


class DomainSubscriptionHelperTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='u2', email='c2@example.com', password='x')
        self.client_profile = ClientProfile.objects.create(
            user=self.user, firm_name='Acme Co')
        self.tier_std = ServiceTier.objects.create(
            slug='domain-standard',
            category='addon',
            name='Domain', price=Decimal('75'),
            is_recurring=True, billing_interval='year',
            stripe_price_id='price_test_std',
        )
        self.tier_law = ServiceTier.objects.create(
            slug='domain-law',
            category='addon',
            name='Domain Law', price=Decimal('175'),
            is_recurring=True, billing_interval='year',
            stripe_price_id='price_test_law',
        )

    def test_get_domain_tier_returns_standard_for_com(self):
        from billing.stripe_helpers import get_domain_tier
        self.assertEqual(get_domain_tier('com').slug, 'domain-standard')

    def test_get_domain_tier_returns_law_for_law(self):
        from billing.stripe_helpers import get_domain_tier
        self.assertEqual(get_domain_tier('law').slug, 'domain-law')

    def test_get_domain_tier_raises_when_no_price_id(self):
        from billing.stripe_helpers import get_domain_tier
        self.tier_std.stripe_price_id = ''
        self.tier_std.save()
        with self.assertRaises(ValueError):
            get_domain_tier('com')
