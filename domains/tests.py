"""Unit tests for the domains app — no live Namecheap calls."""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.pricing_models import ServiceTier, TierFeature
from clients.models import ClientProfile
from domains.models import (
    DomainRegistration,
    PREMIUM_TLDS,
    tier_slug_for_tld,
)
from domains.services import registrant_from_client

User = get_user_model()


def _healthy_balance():
    """A safe stub for `NamecheapClient.get_balances()` — used in tests
    that patch the client but don't care about the balance widget."""
    return {
        'available_balance':             Decimal('100'),
        'account_balance':               Decimal('100'),
        'earned_amount':                 Decimal('0'),
        'withdrawable_amount':           Decimal('0'),
        'funds_required_for_auto_renew': Decimal('0'),
        'currency':                      'USD',
    }


@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='test',
    NAMECHEAP_API_KEY='test',
    NAMECHEAP_USERNAME='test',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class DomainModelTests(TestCase):
    def test_tier_slug_for_tld_returns_law_for_premium(self):
        # All three attorney-niche TLDs sit on the premium tier.
        for tld in ('law', 'legal', 'attorney'):
            self.assertEqual(
                tier_slug_for_tld(tld), 'domain-law',
                f'{tld} should be premium (domain-law tier)')

    def test_tier_slug_for_tld_returns_standard_for_others(self):
        for tld in ('com', 'net', 'org'):
            self.assertEqual(
                tier_slug_for_tld(tld), 'domain-standard',
                f'{tld} should be standard')

    def test_premium_tlds_set(self):
        self.assertEqual(
            PREMIUM_TLDS,
            frozenset({'law', 'legal', 'attorney'}))

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


class AvailabilityPricingTests(TestCase):
    """Regression guard — every TLD must route through tier_slug_for_tld."""

    def setUp(self):
        ServiceTier.objects.create(
            slug='domain-standard', category='addon',
            name='Standard', price=Decimal('75'),
            is_recurring=True, billing_interval='year',
            stripe_price_id='price_std')
        ServiceTier.objects.create(
            slug='domain-law', category='addon',
            name='Premium', price=Decimal('175'),
            is_recurring=True, billing_interval='year',
            stripe_price_id='price_premium')

    def test_attorney_tlds_priced_premium(self):
        from domains.services import check_availability_all_tlds

        with patch(
            'domains.services.get_client'
        ) as mock_get:
            mock_get.return_value.check_availability.return_value = [
                {'domain': f'firmname.{t}', 'available': True,
                 'is_premium': False, 'premium_price': Decimal('0')}
                for t in ('com', 'net', 'org', 'law', 'legal', 'attorney')
            ]
            results = check_availability_all_tlds('firmname')

        prices = {r['tld']: float(r['retail_price']) for r in results}
        self.assertEqual(prices['com'], 75)
        self.assertEqual(prices['net'], 75)
        self.assertEqual(prices['org'], 75)
        self.assertEqual(prices['law'], 175)
        self.assertEqual(prices['legal'], 175)
        self.assertEqual(prices['attorney'], 175)


# ── Sandbox toggle / NamecheapConfig ───────────────────────────────────────

class NamecheapConfigTests(TestCase):
    def test_get_solo_creates_singleton_on_first_access(self):
        from domains.models import NamecheapConfig
        self.assertFalse(NamecheapConfig.objects.exists())
        row = NamecheapConfig.get_solo()
        self.assertIsNotNone(row.pk)
        self.assertEqual(NamecheapConfig.objects.count(), 1)

    def test_get_solo_seeds_from_settings_default(self):
        from domains.models import NamecheapConfig
        # Default = settings.NAMECHEAP_SANDBOX
        with self.settings(NAMECHEAP_SANDBOX=False):
            NamecheapConfig.objects.all().delete()
            row = NamecheapConfig.get_solo()
            self.assertFalse(row.sandbox_mode)

    def test_get_solo_does_not_clobber_existing_row(self):
        from domains.models import NamecheapConfig
        first = NamecheapConfig.get_solo()
        first.sandbox_mode = False
        first.save(update_fields=['sandbox_mode'])
        # Even if settings says True, an existing row keeps its value.
        with self.settings(NAMECHEAP_SANDBOX=True):
            second = NamecheapConfig.get_solo()
        self.assertEqual(first.pk, second.pk)
        self.assertFalse(second.sandbox_mode)

    def test_is_sandbox_returns_singleton_value(self):
        from domains.models import NamecheapConfig
        row = NamecheapConfig.get_solo()
        row.sandbox_mode = True
        row.save(update_fields=['sandbox_mode'])
        self.assertTrue(NamecheapConfig.is_sandbox())
        row.sandbox_mode = False
        row.save(update_fields=['sandbox_mode'])
        self.assertFalse(NamecheapConfig.is_sandbox())


@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='sb_user', NAMECHEAP_API_KEY='sb_key',
    NAMECHEAP_USERNAME='sb_user',
    NAMECHEAP_LIVE_API_USER='live_user',
    NAMECHEAP_LIVE_API_KEY='live_key',
    NAMECHEAP_LIVE_USERNAME='live_user',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class NamecheapClientEnvSwitchTests(TestCase):
    """Whichever env the DB config selects is the one the client uses."""

    def test_sandbox_picks_sandbox_creds(self):
        from domains.models import NamecheapConfig
        from domains.namecheap_client import NamecheapClient
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        client = NamecheapClient()
        self.assertTrue(client.sandbox)
        self.assertEqual(client.api_user, 'sb_user')
        self.assertIn('sandbox', client.endpoint)

    def test_live_picks_live_creds(self):
        from domains.models import NamecheapConfig
        from domains.namecheap_client import NamecheapClient
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=False)
        client = NamecheapClient()
        self.assertFalse(client.sandbox)
        self.assertEqual(client.api_user, 'live_user')
        self.assertNotIn('sandbox', client.endpoint)

    def test_explicit_sandbox_kwarg_wins_over_db(self):
        from domains.models import NamecheapConfig
        from domains.namecheap_client import NamecheapClient
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        client = NamecheapClient(sandbox=False)
        self.assertFalse(client.sandbox)
        self.assertEqual(client.api_user, 'live_user')

    def test_get_client_returns_fresh_instance_each_call(self):
        """No global caching — admin toggle must take effect immediately."""
        from domains.namecheap_client import get_client
        a = get_client()
        b = get_client()
        self.assertIsNot(a, b)


# ── Validation in register_domain_for_client ───────────────────────────────

@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x',
    NAMECHEAP_USERNAME='x',
    NAMECHEAP_LIVE_API_USER='', NAMECHEAP_LIVE_API_KEY='',
    NAMECHEAP_LIVE_USERNAME='',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class RegisterDomainValidationTests(TestCase):
    def setUp(self):
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)

        self.user = User.objects.create_user(
            username='u', email='u@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user,
            firm_name='Test LLC', contact_name='Jane Smith',
            address='1 Way', city='Austin', state='TX',
            zip_code='78701', phone='5125551212',
        )

    def test_invalid_chars_rejected(self):
        from domains.services import register_domain_for_client
        with self.assertRaises(ValueError):
            register_domain_for_client(self.profile, 'bad name', 'com')

    def test_leading_hyphen_rejected(self):
        from domains.services import register_domain_for_client
        with self.assertRaises(ValueError):
            register_domain_for_client(self.profile, '-leading', 'com')

    def test_too_long_rejected(self):
        from domains.services import register_domain_for_client
        with self.assertRaises(ValueError):
            register_domain_for_client(self.profile, 'a' * 64, 'com')

    def test_unavailable_domain_rejected(self):
        from domains.services import register_domain_for_client
        with patch('domains.services.get_client') as mock_nc:
            mock_nc.return_value.check_availability.return_value = [
                {'domain': 'taken.com', 'available': False,
                 'is_premium': False, 'premium_price': Decimal('0')}
            ]
            with self.assertRaises(ValueError) as ctx:
                register_domain_for_client(self.profile, 'taken', 'com')
            self.assertIn('no longer available', str(ctx.exception))

    def test_premium_domain_rejected_for_self_serve(self):
        from domains.services import register_domain_for_client
        with patch('domains.services.get_client') as mock_nc:
            mock_nc.return_value.check_availability.return_value = [
                {'domain': 'shortname.com', 'available': True,
                 'is_premium': True, 'premium_price': Decimal('5000')}
            ]
            with self.assertRaises(ValueError) as ctx:
                register_domain_for_client(self.profile, 'shortname', 'com')
            self.assertIn('premium', str(ctx.exception))


# ── set_auto_a_record ──────────────────────────────────────────────────────

@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x', NAMECHEAP_USERNAME='x',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class AutoARecordTests(TestCase):
    def setUp(self):
        from domains.models import DomainRegistration, NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        self.user = User.objects.create_user(
            username='u3', email='u3@example.com', password='x')
        self.client_profile = ClientProfile.objects.create(
            user=self.user, firm_name='Acme', contact_name='A B',
            address='1', city='Austin', state='TX', zip_code='78701',
            phone='5125551212')
        self.reg = DomainRegistration.objects.create(
            client=self.client_profile, domain_name='auto.com',
            tld='com', status='active',
        )

    def test_set_auto_a_record_writes_apex_a_and_www_cname(self):
        from domains.services import set_auto_a_record
        with patch('domains.services.get_client') as mock_nc:
            set_auto_a_record(self.reg, '10.0.0.42')
            mock_nc.return_value.set_dns_records.assert_called_once()
            args, kwargs = mock_nc.return_value.set_dns_records.call_args
            self.assertEqual(args[0], 'auto.com')
            pushed = args[1]
        # First two records are auto-A + www CNAME.
        self.assertEqual(pushed[0]['host'], '@')
        self.assertEqual(pushed[0]['type'], 'A')
        self.assertEqual(pushed[0]['value'], '10.0.0.42')
        self.assertEqual(pushed[1]['host'], 'www')
        self.assertEqual(pushed[1]['type'], 'CNAME')
        # Mirrored locally.
        local = list(self.reg.dns_records.all())
        self.assertEqual(len(local), 2)
        self.assertTrue(self.reg.auto_a_record_set_at)

    def test_set_auto_a_record_preserves_existing_non_apex_records(self):
        from domains.models import DNSRecord
        from domains.services import set_auto_a_record
        DNSRecord.objects.create(
            domain=self.reg, record_type='TXT', host='@',
            value='v=spf1 ~all', ttl=1800)
        DNSRecord.objects.create(
            domain=self.reg, record_type='MX', host='@',
            value='mail.example.com', ttl=1800, mx_priority=10)
        with patch('domains.services.get_client') as mock_nc:
            set_auto_a_record(self.reg, '10.0.0.99')
            args, _ = mock_nc.return_value.set_dns_records.call_args
            pushed = args[1]
        types_pushed = {(r['host'], r['type']) for r in pushed}
        # Apex A + www CNAME + the TXT + MX all present.
        self.assertIn(('@', 'A'), types_pushed)
        self.assertIn(('www', 'CNAME'), types_pushed)
        self.assertIn(('@', 'TXT'), types_pushed)
        self.assertIn(('@', 'MX'), types_pushed)


# ── replace_dns_records ────────────────────────────────────────────────────

class ReplaceDNSRecordsTests(TestCase):
    def setUp(self):
        from domains.models import DomainRegistration, NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        self.user = User.objects.create_user(
            username='u4', email='u4@example.com', password='x')
        self.client_profile = ClientProfile.objects.create(
            user=self.user, firm_name='Acme',
        )
        self.reg = DomainRegistration.objects.create(
            client=self.client_profile, domain_name='dns.com',
            tld='com', status='active',
        )

    def test_replace_pushes_to_namecheap_and_mirrors_locally(self):
        from domains.services import replace_dns_records
        new = [
            {'host': '@', 'type': 'A', 'value': '5.5.5.5',
             'ttl': 1800, 'mx_pref': 10},
            {'host': 'mail', 'type': 'MX',
             'value': 'mailserver.example.com',
             'ttl': 1800, 'mx_pref': 20},
        ]
        with patch('domains.services.get_client') as mock_nc:
            replace_dns_records(self.reg, new)
            mock_nc.return_value.set_dns_records.assert_called_once_with(
                'dns.com', new)
        local = list(self.reg.dns_records.all().order_by('host'))
        self.assertEqual(len(local), 2)
        types = {r.record_type for r in local}
        self.assertEqual(types, {'A', 'MX'})

    def test_replace_drops_pre_existing_records(self):
        from domains.models import DNSRecord
        from domains.services import replace_dns_records
        DNSRecord.objects.create(
            domain=self.reg, record_type='A', host='@',
            value='1.1.1.1', ttl=1800)
        with patch('domains.services.get_client') as mock_nc:
            replace_dns_records(self.reg, [
                {'host': '@', 'type': 'A', 'value': '2.2.2.2',
                 'ttl': 1800, 'mx_pref': 10},
            ])
        local = list(self.reg.dns_records.all())
        self.assertEqual(len(local), 1)
        self.assertEqual(local[0].value, '2.2.2.2')


# ── EPP encryption round-trip ──────────────────────────────────────────────

class EPPCryptoTests(TestCase):
    def test_set_epp_code_then_decrypt_round_trips(self):
        from domains.models import DomainRegistration
        reg = DomainRegistration(domain_name='epp.com', tld='com')
        reg.set_epp_code('A8s_secret-2026!')
        # Stored encrypted (hex), not plain.
        self.assertNotEqual(reg.epp_code_encrypted, 'A8s_secret-2026!')
        self.assertTrue(reg.epp_code_encrypted)   # non-empty
        self.assertEqual(reg.decrypt_epp_code(), 'A8s_secret-2026!')
        # epp_code_issued_at populated.
        self.assertIsNotNone(reg.epp_code_issued_at)

    def test_set_epp_code_with_empty_clears_field(self):
        from domains.models import DomainRegistration
        reg = DomainRegistration(domain_name='epp2.com', tld='com')
        reg.set_epp_code('initial')
        self.assertTrue(reg.epp_code_encrypted)
        reg.set_epp_code('')
        self.assertEqual(reg.decrypt_epp_code(), '')


# ── Namecheap XML parsing edge cases ──────────────────────────────────────

class NamecheapClientXMLTests(TestCase):
    """Direct test of XML parsing without mocking — feed crafted XML."""

    def _build_client(self):
        from domains.namecheap_client import NamecheapClient
        return NamecheapClient(sandbox=True)

    def test_check_availability_handles_premium_flag(self):
        # Build a fake XML response.
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<ApiResponse Status="OK" xmlns="http://api.namecheap.com/xml.response">
  <CommandResponse>
    <DomainCheckResult Domain="ok.com" Available="true"
        IsPremiumName="false" PremiumRegistrationPrice="0" />
    <DomainCheckResult Domain="taken.com" Available="false"
        IsPremiumName="false" PremiumRegistrationPrice="0" />
    <DomainCheckResult Domain="pricy.com" Available="true"
        IsPremiumName="true" PremiumRegistrationPrice="500.00" />
  </CommandResponse>
</ApiResponse>"""

        with override_settings(
                NAMECHEAP_SANDBOX=True,
                NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x',
                NAMECHEAP_USERNAME='x', NAMECHEAP_CLIENT_IP='127.0.0.1'):
            client = self._build_client()
            from unittest.mock import MagicMock
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = xml.decode('utf-8')
            with patch(
                'domains.namecheap_client.requests.post',
                return_value=mock_resp,
            ):
                results = client.check_availability(
                    ['ok.com', 'taken.com', 'pricy.com'])

        by_domain = {r['domain']: r for r in results}
        self.assertTrue(by_domain['ok.com']['available'])
        self.assertFalse(by_domain['taken.com']['available'])
        self.assertTrue(by_domain['pricy.com']['available'])
        self.assertTrue(by_domain['pricy.com']['is_premium'])
        self.assertEqual(
            by_domain['pricy.com']['premium_price'], Decimal('500.00'))

    def test_api_error_raises_namecheap_error(self):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<ApiResponse Status="ERROR" xmlns="http://api.namecheap.com/xml.response">
  <Errors>
    <Error Number="2030280">TLD not found in TLD list</Error>
  </Errors>
</ApiResponse>"""
        with override_settings(
                NAMECHEAP_SANDBOX=True,
                NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x',
                NAMECHEAP_USERNAME='x', NAMECHEAP_CLIENT_IP='127.0.0.1'):
            from domains.namecheap_client import (
                NamecheapClient, NamecheapError,
            )
            from unittest.mock import MagicMock
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = xml.decode('utf-8')
            with patch(
                'domains.namecheap_client.requests.post',
                return_value=mock_resp,
            ):
                client = NamecheapClient(sandbox=True)
                with self.assertRaises(NamecheapError) as ctx:
                    client.check_availability(['xx.fakeTLD'])
                self.assertEqual(ctx.exception.number, '2030280')


# ── Webhook handlers for domain events ────────────────────────────────────

class DomainWebhookTests(TestCase):
    def setUp(self):
        from domains.models import DomainRegistration, NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)

        self.user = User.objects.create_user(
            username='wb', email='wb@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='Web Co',
            stripe_customer_id='cus_abc',
        )
        self.reg = DomainRegistration.objects.create(
            client=self.profile, domain_name='wb.com', tld='com',
            status='active', stripe_subscription_id='sub_domain_123',
        )

    def test_subscription_deleted_marks_grace_as_expired(self):
        from billing.webhooks import _handle_subscription_deleted
        self.reg.status = 'grace'
        self.reg.save()
        event = {'data': {'object': {
            'id': 'sub_domain_123',
            'customer': 'cus_abc',
        }}}
        _handle_subscription_deleted(event)
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.status, 'expired')
        self.assertEqual(self.reg.stripe_subscription_id, '')

    def test_subscription_deleted_active_domain_stays_active(self):
        # If the sub was deleted while domain was still active
        # (admin-triggered cancel without grace flow) the row still
        # gets the sub id cleared but status preserved — daily
        # reconcile will catch the NC-side change.
        from billing.webhooks import _handle_subscription_deleted
        event = {'data': {'object': {
            'id': 'sub_domain_123',
            'customer': 'cus_abc',
        }}}
        _handle_subscription_deleted(event)
        self.reg.refresh_from_db()
        # status not flipped because not in grace
        self.assertEqual(self.reg.status, 'active')
        self.assertEqual(self.reg.stripe_subscription_id, '')

    def test_invoice_paid_for_domain_calls_renew(self):
        from billing.webhooks import _maybe_handle_domain_renewal
        invoice = {'subscription': 'sub_domain_123'}

        with patch('domains.namecheap_client.get_client') as mock_get_nc:
            mock_get_nc.return_value.renew_domain.return_value = {
                'renewed': True,
                'charged_amount': Decimal('9.00'),
            }
            handled = _maybe_handle_domain_renewal(
                self.profile, 'sub_domain_123', invoice)
        self.assertTrue(handled)
        self.reg.refresh_from_db()
        # expires_at should have been pushed forward.
        self.assertIsNotNone(self.reg.expires_at)
        self.assertEqual(self.reg.last_api_error, '')

    def test_invoice_paid_unknown_sub_returns_false(self):
        from billing.webhooks import _maybe_handle_domain_renewal
        invoice = {'subscription': 'sub_unknown'}
        handled = _maybe_handle_domain_renewal(
            self.profile, 'sub_unknown', invoice)
        self.assertFalse(handled)


# ── Reconcile management command ──────────────────────────────────────────

@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x', NAMECHEAP_USERNAME='x',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class ReconcileCommandTests(TestCase):
    def setUp(self):
        from django.utils import timezone as _tz
        from datetime import timedelta
        from domains.models import DomainRegistration, NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        self.user = User.objects.create_user(
            username='rc', email='rc@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='RC')
        # Domain renews in 7 days — should fire heads-up.
        self.reg = DomainRegistration.objects.create(
            client=self.profile, domain_name='rc.com', tld='com',
            status='active',
            expires_at=_tz.now() + timedelta(days=7),
        )

    def test_reconcile_dry_run_does_not_send_email(self):
        from io import StringIO
        from django.core.management import call_command
        with patch('domains.management.commands.reconcile_domains.sync_one'):
            buf = StringIO()
            call_command('reconcile_domains', '--dry-run', stdout=buf)
            out = buf.getvalue()
        self.assertIn('DRY', out)

    def test_reconcile_sends_heads_up_for_domains_in_window(self):
        from django.core.management import call_command
        from io import StringIO
        with patch('domains.management.commands.reconcile_domains.sync_one'), \
             patch('domains.management.commands.reconcile_domains.send_renewal_soon_email') as mock_send:
            buf = StringIO()
            call_command('reconcile_domains', stdout=buf)
        mock_send.assert_called_once()
        # First positional arg should be the registration.
        args, _ = mock_send.call_args
        self.assertEqual(args[0].pk, self.reg.pk)

    def test_reconcile_skips_domains_outside_window(self):
        from django.utils import timezone as _tz
        from datetime import timedelta
        from django.core.management import call_command
        from io import StringIO
        # Move expiry well outside the 7-day window (±2 days).
        self.reg.expires_at = _tz.now() + timedelta(days=30)
        self.reg.save()
        with patch('domains.management.commands.reconcile_domains.sync_one'), \
             patch('domains.management.commands.reconcile_domains.send_renewal_soon_email') as mock_send:
            buf = StringIO()
            call_command('reconcile_domains', stdout=buf)
        mock_send.assert_not_called()


# ── DNS edit view — empty + no-apex guards ───────────────────────────────

@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x', NAMECHEAP_USERNAME='x',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class DNSEditViewTests(TestCase):
    def setUp(self):
        from django.test import Client as DjangoTestClient
        from domains.models import DomainRegistration, NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)

        self.user = User.objects.create_user(
            username='dns', email='dns@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='DNS Co',
        )
        self.reg = DomainRegistration.objects.create(
            client=self.profile, domain_name='dnsguard.com',
            tld='com', status='active',
        )
        self.tc = DjangoTestClient()
        self.tc.force_login(self.user)

    def _post(self, types, hosts, values, ttls=None, prefs=None):
        return self.tc.post(
            f'/portal/domains/{self.reg.id}/dns/',
            {
                'types[]': types,
                'hosts[]': hosts,
                'values[]': values,
                'ttls[]': ttls or ['1800'] * len(values),
                'mx_prefs[]': prefs or ['10'] * len(values),
            },
            follow=False,
        )

    def test_empty_record_set_rejected(self):
        with patch('domains.views.replace_dns_records') as mock_replace:
            self._post(types=['A'], hosts=['@'], values=[''])
            mock_replace.assert_not_called()

    def test_no_apex_record_rejected(self):
        with patch('domains.views.replace_dns_records') as mock_replace:
            self._post(
                types=['A'], hosts=['blog'], values=['1.2.3.4'])
            mock_replace.assert_not_called()

    def test_valid_apex_a_record_pushed(self):
        with patch('domains.views.replace_dns_records') as mock_replace:
            self._post(
                types=['A'], hosts=['@'], values=['1.2.3.4'])
            mock_replace.assert_called_once()


# ── Admin-side registration ────────────────────────────────────────────────

@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x', NAMECHEAP_USERNAME='x',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class AdminRegisterDomainTests(TestCase):
    def setUp(self):
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        self.user = User.objects.create_user(
            username='adm', email='adm@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='Admin Co',
            contact_name='Adam B', address='1 St', city='Austin',
            state='TX', zip_code='78701', phone='5125551212')

    def test_admin_register_skips_stripe_subscription(self):
        from domains.services import admin_register_domain_for_client
        with patch('domains.services.get_client') as mock_nc:
            mock_nc.return_value.check_availability.return_value = [
                {'domain': 'gift.com', 'available': True,
                 'is_premium': False, 'premium_price': Decimal('0')}
            ]
            mock_nc.return_value.register_domain.return_value = {
                'domain': 'gift.com', 'registered': True,
                'domain_id': 'nc_123', 'whois_guard_enabled': True,
                'charged_amount': Decimal('9.00'),
                'order_id': 'ord_1', 'transaction_id': 'tx_1',
            }
            reg = admin_register_domain_for_client(
                self.profile, 'gift', 'com',
                send_email=False, internal_notes='referral gift')
        # No Stripe sub created.
        self.assertEqual(reg.stripe_subscription_id, '')
        self.assertEqual(reg.status, 'active')
        self.assertEqual(reg.internal_notes, 'referral gift')
        self.assertEqual(reg.domain_name, 'gift.com')

    def test_admin_register_allows_premium_names(self):
        """Standard registration rejects premium; admin can override."""
        from domains.services import admin_register_domain_for_client
        with patch('domains.services.get_client') as mock_nc:
            mock_nc.return_value.check_availability.return_value = [
                {'domain': 'short.com', 'available': True,
                 'is_premium': True,
                 'premium_price': Decimal('5000')}
            ]
            mock_nc.return_value.register_domain.return_value = {
                'domain': 'short.com', 'registered': True,
                'domain_id': 'nc_999', 'whois_guard_enabled': True,
                'charged_amount': Decimal('5000'),
                'order_id': 'ord_2', 'transaction_id': 'tx_2',
            }
            reg = admin_register_domain_for_client(
                self.profile, 'short', 'com', send_email=False)
        self.assertEqual(reg.status, 'active')

    def test_admin_register_rejects_invalid_chars(self):
        from domains.services import admin_register_domain_for_client
        with self.assertRaises(ValueError):
            admin_register_domain_for_client(
                self.profile, 'bad name', 'com', send_email=False)

    def test_admin_register_rejects_unavailable_domain(self):
        from domains.services import admin_register_domain_for_client
        with patch('domains.services.get_client') as mock_nc:
            mock_nc.return_value.check_availability.return_value = [
                {'domain': 'taken.com', 'available': False,
                 'is_premium': False, 'premium_price': Decimal('0')}
            ]
            with self.assertRaises(ValueError) as ctx:
                admin_register_domain_for_client(
                    self.profile, 'taken', 'com', send_email=False)
            self.assertIn('not available', str(ctx.exception))


# ── Sandbox mode bypasses Stripe ──────────────────────────────────────────

@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x', NAMECHEAP_USERNAME='x',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class SandboxBypassesStripeTests(TestCase):
    """
    When sandbox mode is on, register_domain_for_client MUST NOT
    touch Stripe — no subscription created, no payment-method check,
    no charge. Otherwise sandbox testing requires a real card on
    file, defeating the whole point.
    """

    def setUp(self):
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        self.user = User.objects.create_user(
            username='sb', email='sb@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='SB Co',
            contact_name='Sandy B', address='1 Way',
            city='Austin', state='TX', zip_code='78701',
            phone='5125551212')

    def test_sandbox_register_does_not_call_stripe(self):
        from domains.services import register_domain_for_client
        with patch('domains.services.get_client') as mock_nc, \
             patch('billing.stripe_helpers.create_domain_subscription') as mock_stripe:
            mock_nc.return_value.check_availability.return_value = [
                {'domain': 'sbtest.com', 'available': True,
                 'is_premium': False, 'premium_price': Decimal('0')}
            ]
            mock_nc.return_value.register_domain.return_value = {
                'domain': 'sbtest.com', 'registered': True,
                'domain_id': 'nc_sb1', 'whois_guard_enabled': True,
                'charged_amount': Decimal('9.00'),
                'order_id': 'ord_sb1', 'transaction_id': 'tx_sb1',
            }
            reg = register_domain_for_client(self.profile, 'sbtest', 'com')

        mock_stripe.assert_not_called()
        self.assertEqual(reg.stripe_subscription_id, '')
        self.assertEqual(reg.status, 'active')
        self.assertIn('Sandbox', reg.internal_notes)

    def test_sandbox_register_does_not_require_default_card(self):
        """Profile with NO stripe_customer_id must still be able to
        register in sandbox mode."""
        from domains.services import register_domain_for_client
        self.assertEqual(self.profile.stripe_customer_id, '')
        with patch('domains.services.get_client') as mock_nc:
            mock_nc.return_value.check_availability.return_value = [
                {'domain': 'nocard.com', 'available': True,
                 'is_premium': False, 'premium_price': Decimal('0')}
            ]
            mock_nc.return_value.register_domain.return_value = {
                'domain': 'nocard.com', 'registered': True,
                'domain_id': 'nc_nc1', 'whois_guard_enabled': True,
                'charged_amount': Decimal('9.00'),
                'order_id': 'ord_nc1', 'transaction_id': 'tx_nc1',
            }
            reg = register_domain_for_client(self.profile, 'nocard', 'com')
        self.assertEqual(reg.status, 'active')


# ── Multi-domain per client ───────────────────────────────────────────────

class MultiDomainPerClientTests(TestCase):
    def test_client_can_have_many_domains(self):
        from domains.models import DomainRegistration
        user = User.objects.create_user(
            username='multi', email='multi@example.com', password='x')
        profile = ClientProfile.objects.create(
            user=user, firm_name='Multi Co')
        for name in ('one.com', 'two.com', 'three.law', 'four.legal'):
            DomainRegistration.objects.create(
                client=profile, domain_name=name,
                tld=name.split('.')[-1], status='active')
        self.assertEqual(profile.domain_registrations.count(), 4)

    def test_domain_name_globally_unique(self):
        """Same domain can't be registered twice (across clients)."""
        from domains.models import DomainRegistration
        from django.db import IntegrityError
        user1 = User.objects.create_user(
            username='u_a', email='a@example.com', password='x')
        user2 = User.objects.create_user(
            username='u_b', email='b@example.com', password='x')
        p1 = ClientProfile.objects.create(
            user=user1, firm_name='A Inc')
        p2 = ClientProfile.objects.create(
            user=user2, firm_name='B Inc')
        DomainRegistration.objects.create(
            client=p1, domain_name='unique.com', tld='com',
            status='active')
        with self.assertRaises(IntegrityError):
            DomainRegistration.objects.create(
                client=p2, domain_name='unique.com', tld='com',
                status='active')


# ── Sandbox banner context wiring ─────────────────────────────────────────

class SandboxBannerContextTests(TestCase):
    """
    Banner must show on domain pages but NOT on other portal pages.
    Avoids spooking clients who are on a non-domain page (dashboard,
    invoices, etc.) by surfacing an irrelevant "testing mode" warning.
    """

    def setUp(self):
        from django.test import Client as DjangoTestClient
        from domains.models import NamecheapConfig
        self.user = User.objects.create_user(
            username='banner', email='banner@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='Banner Co')
        self.tc = DjangoTestClient()
        self.tc.force_login(self.user)
        NamecheapConfig.objects.all().delete()

    def test_banner_present_on_domain_search_when_sandbox_on(self):
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.create(sandbox_mode=True)
        resp = self.tc.get('/portal/domains/search/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Testing mode')
        self.assertContains(resp, 'Nothing is permanent')

    def test_banner_present_on_domain_list_when_sandbox_on(self):
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.create(sandbox_mode=True)
        resp = self.tc.get('/portal/domains/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Testing mode')

    def test_banner_absent_on_dashboard_even_when_sandbox_on(self):
        """Non-domain portal pages should NOT carry the banner."""
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.create(sandbox_mode=True)
        resp = self.tc.get('/portal/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'Testing mode')

    def test_banner_absent_on_domain_pages_when_sandbox_off(self):
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.create(sandbox_mode=False)
        resp = self.tc.get('/portal/domains/search/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'Testing mode')


# ── Settings form has all the WHOIS-required fields ───────────────────────

class SettingsFormFieldsTests(TestCase):
    """Settings form must include every field domain registration needs."""

    def test_form_includes_all_whois_required_fields(self):
        from clients.forms import SettingsForm
        form = SettingsForm()
        for field in ('contact_name', 'phone', 'address',
                      'city', 'state', 'zip_code'):
            self.assertIn(field, form.fields,
                          f'SettingsForm missing required WHOIS field {field}')

    def test_message_blocks_not_duplicated_in_domain_templates(self):
        """Regression — each domain template must NOT render its own
        messages block; base.html handles all flashes."""
        import os
        import re
        domain_templates_dir = os.path.join(
            os.path.dirname(__file__), 'templates', 'domains')
        offending = []
        msg_block = re.compile(r'\{%\s*if messages\s*%\}')
        for fname in os.listdir(domain_templates_dir):
            if not fname.endswith('.html'):
                continue
            if fname.startswith('_'):
                continue   # partials are fine, they're includes
            path = os.path.join(domain_templates_dir, fname)
            with open(path, encoding='utf-8') as f:
                if msg_block.search(f.read()):
                    offending.append(fname)
        self.assertEqual(
            offending, [],
            f'Templates duplicating the messages block: {offending}. '
            f'Remove these — base.html renders flash messages already.')

    def test_aspired_constants_match_claude_md(self):
        """Lock the WHOIS registrant identity so a refactor doesn't
        silently change what's recorded at the registry."""
        from domains.services import ASPIRED_REGISTRANT
        self.assertEqual(ASPIRED_REGISTRANT['first_name'], 'Zachery')
        self.assertEqual(ASPIRED_REGISTRANT['last_name'], 'Long')
        self.assertEqual(
            ASPIRED_REGISTRANT['organization_name'],
            'Aspired Websites LLC')
        self.assertEqual(
            ASPIRED_REGISTRANT['address1'],
            '8735 Dunwoody Place, Ste R')
        self.assertEqual(ASPIRED_REGISTRANT['city'], 'Atlanta')
        self.assertEqual(ASPIRED_REGISTRANT['state_province'], 'GA')
        self.assertEqual(ASPIRED_REGISTRANT['postal_code'], '30350')
        self.assertEqual(ASPIRED_REGISTRANT['country'], 'US')
        self.assertEqual(ASPIRED_REGISTRANT['phone'], '+1.2108962536')
        self.assertEqual(
            ASPIRED_REGISTRANT['email_address'],
            'zachery@aspiredwebsites.com')

    def test_aspired_registrant_returns_fresh_dict(self):
        """Caller mutation shouldn't poison the constant."""
        from domains.services import aspired_registrant, ASPIRED_REGISTRANT
        d = aspired_registrant()
        d['first_name'] = 'EVIL'
        self.assertEqual(ASPIRED_REGISTRANT['first_name'], 'Zachery')

    def test_settings_form_persists_address(self):
        from clients.forms import SettingsForm
        user = User.objects.create_user(
            username='setting', email='s@example.com', password='x')
        profile = ClientProfile.objects.create(
            user=user, firm_name='Settings Co')
        form = SettingsForm(
            data={
                'contact_name': 'Test User',
                'phone': '(512) 555-1234',
                'address': '500 W St',
                'city': 'Austin',
                'state': 'TX',
                'zip_code': '78701',
                'preferred_contact_method': 'email',
                'notify_on_stage_change': True,
            },
            instance=profile,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.contact_name, 'Test User')
        self.assertEqual(saved.address, '500 W St')
        self.assertEqual(saved.city, 'Austin')
        self.assertEqual(saved.state, 'TX')
        self.assertEqual(saved.zip_code, '78701')


# ── Aspired-as-registrant is what we send to Namecheap ─────────────────────

@override_settings(
    NAMECHEAP_SANDBOX=False,
    NAMECHEAP_LIVE_API_USER='x', NAMECHEAP_LIVE_API_KEY='x',
    NAMECHEAP_LIVE_USERNAME='x',
    NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x', NAMECHEAP_USERNAME='x',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class AspiredRegistrantOnRegisterTests(TestCase):
    """Both register paths must use Aspired (not the client) as registrant."""

    def setUp(self):
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=False)
        # Add a Stripe domain tier so create_domain_subscription
        # can resolve a Price ID (the client path uses Stripe live).
        ServiceTier.objects.create(
            slug='domain-standard', category='addon',
            name='Std', price=Decimal('75'),
            is_recurring=True, billing_interval='year',
            stripe_price_id='price_test_std')
        self.user = User.objects.create_user(
            username='aspired_test', email='a@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='Client Inc',
            contact_name='Client Person',
            address='100 Client St', city='Houston',
            state='TX', zip_code='77002', phone='5125550100',
            stripe_customer_id='cus_test')

    def _setup_nc_mocks(self, mock_get):
        nc = mock_get.return_value
        nc.check_availability.return_value = [
            {'domain': 'foo.com', 'available': True,
             'is_premium': False, 'premium_price': Decimal('0')}
        ]
        nc.register_domain.return_value = {
            'domain': 'foo.com', 'registered': True,
            'domain_id': 'nc_1', 'whois_guard_enabled': True,
            'charged_amount': Decimal('9'),
            'order_id': 'ord', 'transaction_id': 'tx',
        }
        return nc

    def test_client_register_path_sends_aspired_as_registrant(self):
        from domains.services import (
            ASPIRED_REGISTRANT, register_domain_for_client,
        )
        with patch('domains.services.get_client') as mock_nc, \
             patch('billing.stripe_helpers.create_domain_subscription') as mock_sub:
            mock_sub.return_value.id = 'sub_test'
            self._setup_nc_mocks(mock_nc)
            register_domain_for_client(self.profile, 'foo', 'com')

            # The registrant kwarg passed to NC.register_domain MUST
            # match the Aspired constants, NOT the client's details.
            call_kwargs = mock_nc.return_value.register_domain.call_args.kwargs
            sent_registrant = call_kwargs['registrant']
        for key in ('first_name', 'last_name', 'organization_name',
                    'address1', 'city', 'state_province',
                    'postal_code', 'country', 'phone',
                    'email_address'):
            self.assertEqual(
                sent_registrant[key], ASPIRED_REGISTRANT[key],
                f'{key} should be Aspired, got {sent_registrant[key]}')
        # Specifically NOT the client's data
        self.assertNotEqual(
            sent_registrant['email_address'], 'a@example.com')
        self.assertNotEqual(sent_registrant['city'], 'Houston')

    def test_admin_register_path_sends_aspired_as_registrant(self):
        from domains.services import (
            ASPIRED_REGISTRANT, admin_register_domain_for_client,
        )
        with patch('domains.services.get_client') as mock_nc:
            self._setup_nc_mocks(mock_nc)
            admin_register_domain_for_client(
                self.profile, 'foo', 'com', send_email=False)

            sent_registrant = (
                mock_nc.return_value.register_domain.call_args
                .kwargs['registrant'])
        self.assertEqual(
            sent_registrant['email_address'],
            ASPIRED_REGISTRANT['email_address'])
        self.assertEqual(
            sent_registrant['organization_name'],
            'Aspired Websites LLC')


# ── resume_domain ─────────────────────────────────────────────────────────

class ResumeDomainTests(TestCase):
    def setUp(self):
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        self.user = User.objects.create_user(
            username='rs', email='rs@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='Resume Co',
            stripe_customer_id='cus_resume')
        from domains.models import DomainRegistration
        self.reg = DomainRegistration.objects.create(
            client=self.profile, domain_name='undo.com', tld='com',
            status='grace', stripe_subscription_id='sub_rs')
        self.reg.set_epp_code('abc123')
        self.reg.registrar_lock = False
        self.reg.save()

    def test_resume_lifts_lock_swaps_registrant_back_clears_epp(self):
        from domains.services import resume_domain
        with patch('domains.services.get_client') as mock_nc, \
             patch('billing.stripe_helpers.resume_domain_subscription') as mock_resume:
            resume_domain(self.reg)
            # Re-locked
            mock_nc.return_value.set_registrar_lock.assert_called_once_with(
                'undo.com', lock=True)
            # Registrant swapped back to Aspired
            mock_nc.return_value.set_contacts.assert_called_once()
            args, kwargs = mock_nc.return_value.set_contacts.call_args
            sent = args[1]
            self.assertEqual(
                sent['email_address'], 'zachery@aspiredwebsites.com')
            # Stripe resume called
            mock_resume.assert_called_once_with(self.reg)
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.status, 'active')
        self.assertTrue(self.reg.registrar_lock)
        self.assertEqual(self.reg.decrypt_epp_code(), '')

    def test_resume_refuses_on_non_grace_status(self):
        from domains.services import resume_domain
        self.reg.status = 'active'
        self.reg.save()
        with self.assertRaises(ValueError) as ctx:
            resume_domain(self.reg)
        self.assertIn('grace', str(ctx.exception))


# ── transfer-out updates registrant to client ─────────────────────────────

class TransferOutRegistrantSwapTests(TestCase):
    def setUp(self):
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        self.user = User.objects.create_user(
            username='to', email='to@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='TO Co',
            contact_name='Out Person', address='5 W',
            city='Dallas', state='TX', zip_code='75201',
            phone='5125559999',
            stripe_customer_id='cus_to')
        from domains.models import DomainRegistration
        self.reg = DomainRegistration.objects.create(
            client=self.profile, domain_name='leaving.com', tld='com',
            status='active', stripe_subscription_id='sub_to')

    def test_transfer_out_calls_set_contacts_with_client_data(self):
        from domains.services import begin_transfer_out
        with patch('domains.services.get_client') as mock_nc, \
             patch('billing.stripe_helpers.cancel_domain_subscription'), \
             patch('domains.emails.send_branded'):
            mock_nc.return_value.get_epp_code.return_value = 'EPP123'
            begin_transfer_out(self.reg, reason='leaving')
            mock_nc.return_value.set_contacts.assert_called_once()
            sent_registrant = mock_nc.return_value.set_contacts.call_args.args[1]
        # Should be the CLIENT's data, not Aspired's.
        self.assertEqual(
            sent_registrant['email_address'], 'to@example.com')
        self.assertEqual(sent_registrant['city'], 'Dallas')
        self.assertEqual(sent_registrant['postal_code'], '75201')

    def test_transfer_out_proceeds_when_client_profile_incomplete(self):
        """If client profile is incomplete, skip set_contacts but
        continue the unlock + EPP + email flow."""
        from domains.services import begin_transfer_out
        # Empty out the address fields
        self.profile.address = ''
        self.profile.save()

        with patch('domains.services.get_client') as mock_nc, \
             patch('billing.stripe_helpers.cancel_domain_subscription'), \
             patch('domains.emails.send_branded'):
            mock_nc.return_value.get_epp_code.return_value = 'EPP456'
            epp = begin_transfer_out(self.reg, reason='no profile')
            # set_contacts NOT called
            mock_nc.return_value.set_contacts.assert_not_called()
            # but unlock and getEPP still called
            mock_nc.return_value.set_registrar_lock.assert_called_once_with(
                'leaving.com', lock=False)
        self.assertEqual(epp, 'EPP456')
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.status, 'grace')


# ── park_domain / unpark_domain ───────────────────────────────────────────

class ParkUnparkTests(TestCase):
    def setUp(self):
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        self.user = User.objects.create_user(
            username='pk', email='pk@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='Park Co')
        from domains.models import DomainRegistration, DNSRecord
        self.reg = DomainRegistration.objects.create(
            client=self.profile, domain_name='park.com', tld='com',
            status='active')

    def test_park_pushes_url301_for_apex_and_www(self):
        from domains.services import park_domain
        with patch('domains.services.get_client') as mock_nc:
            park_domain(self.reg)
            args, _ = mock_nc.return_value.set_dns_records.call_args
            self.assertEqual(args[0], 'park.com')
            pushed = args[1]
        # Two URL301 redirects, apex + www
        hosts = [(r['host'], r['type']) for r in pushed]
        self.assertIn(('@', 'URL301'), hosts)
        self.assertIn(('www', 'URL301'), hosts)
        # Value contains the parking URL with the domain in the query
        for r in pushed:
            self.assertIn(
                '/parked/?for=park.com', r['value'])
        self.reg.refresh_from_db()
        self.assertIsNotNone(self.reg.parked_at)

    def test_park_mirrors_records_locally(self):
        from domains.models import DNSRecord
        from domains.services import park_domain
        DNSRecord.objects.create(
            domain=self.reg, record_type='A', host='@',
            value='1.2.3.4', ttl=1800)
        with patch('domains.services.get_client'):
            park_domain(self.reg)
        local = list(self.reg.dns_records.all())
        self.assertEqual(len(local), 2)
        for r in local:
            self.assertEqual(r.record_type, 'URL301')
            self.assertTrue(r.auto_managed)

    def test_unpark_calls_set_auto_a_record(self):
        from domains.services import unpark_domain
        self.reg.parked_at = timezone.now()
        self.reg.save()
        with patch('domains.services.set_auto_a_record') as mock_set_a:
            unpark_domain(self.reg, '5.6.7.8')
            mock_set_a.assert_called_once_with(self.reg, '5.6.7.8')
        self.reg.refresh_from_db()
        self.assertIsNone(self.reg.parked_at)


# ── Hosting subscription.deleted webhook triggers park ────────────────────

class HostingCancelParksDomainsTests(TestCase):
    def setUp(self):
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        self.user = User.objects.create_user(
            username='hc', email='hc@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='HC Co',
            stripe_customer_id='cus_hc',
            stripe_hosting_subscription_id='sub_hosting_xyz')
        from domains.models import DomainRegistration
        self.reg = DomainRegistration.objects.create(
            client=self.profile, domain_name='hostparked.com',
            tld='com', status='active')

    def test_hosting_subscription_deleted_parks_active_domains(self):
        from billing.webhooks import _handle_subscription_deleted
        event = {'data': {'object': {
            'id': 'sub_hosting_xyz', 'customer': 'cus_hc',
        }}}
        with patch('domains.services.get_client') as mock_nc:
            _handle_subscription_deleted(event)
        # park called once for this domain
        mock_nc.return_value.set_dns_records.assert_called_once()
        self.reg.refresh_from_db()
        self.assertIsNotNone(self.reg.parked_at)

    def test_hosting_cancel_skips_non_active_domains(self):
        from billing.webhooks import _handle_subscription_deleted
        self.reg.status = 'grace'
        self.reg.save()
        event = {'data': {'object': {
            'id': 'sub_hosting_xyz', 'customer': 'cus_hc',
        }}}
        with patch('domains.services.get_client') as mock_nc:
            _handle_subscription_deleted(event)
        mock_nc.return_value.set_dns_records.assert_not_called()


# ── NamecheapClient: set_contacts + get_balances XML ──────────────────────

@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x', NAMECHEAP_USERNAME='x',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class NamecheapClientNewMethodsTests(TestCase):
    def test_set_contacts_posts_under_four_roles(self):
        from domains.namecheap_client import NamecheapClient
        from unittest.mock import MagicMock
        xml = (b'<?xml version="1.0"?>'
               b'<ApiResponse Status="OK" xmlns="http://api.namecheap.com/xml.response">'
               b'<CommandResponse><DomainSetContactResult Domain="x.com" IsSuccess="true"/>'
               b'</CommandResponse></ApiResponse>')
        mock_resp = MagicMock(status_code=200, text=xml.decode('utf-8'))
        registrant = {
            'first_name': 'A', 'last_name': 'B',
            'organization_name': 'Org',
            'address1': '1 St', 'address2': '',
            'city': 'Austin', 'state_province': 'TX',
            'postal_code': '78701', 'country': 'US',
            'phone': '+1.5125551212',
            'email_address': 'a@b.com',
        }
        with patch('domains.namecheap_client.requests.post',
                   return_value=mock_resp) as mock_post:
            client = NamecheapClient(sandbox=True)
            self.assertTrue(client.set_contacts('x.com', registrant))
        # Verify the sent params include all four role prefixes.
        sent_data = mock_post.call_args.kwargs['data']
        for role in ('Registrant', 'Tech', 'Admin', 'AuxBilling'):
            self.assertEqual(sent_data[f'{role}FirstName'], 'A')
            self.assertEqual(sent_data[f'{role}EmailAddress'], 'a@b.com')

    def test_get_balances_parses_xml(self):
        from domains.namecheap_client import NamecheapClient
        from unittest.mock import MagicMock
        xml = (b'<?xml version="1.0"?>'
               b'<ApiResponse Status="OK" xmlns="http://api.namecheap.com/xml.response">'
               b'<CommandResponse><UserGetBalancesResult '
               b'Currency="USD" AvailableBalance="42.50" '
               b'AccountBalance="50.00" EarnedAmount="0" '
               b'WithdrawableAmount="42.50" '
               b'FundsRequiredForAutoRenew="0"/>'
               b'</CommandResponse></ApiResponse>')
        mock_resp = MagicMock(status_code=200, text=xml.decode('utf-8'))
        with patch('domains.namecheap_client.requests.post',
                   return_value=mock_resp):
            client = NamecheapClient(sandbox=True)
            balances = client.get_balances()
        self.assertEqual(balances['available_balance'], Decimal('42.50'))
        self.assertEqual(balances['account_balance'], Decimal('50.00'))
        self.assertEqual(balances['currency'], 'USD')


# ── Parking page view ─────────────────────────────────────────────────────

class ParkingPageViewTests(TestCase):
    def test_parking_page_renders_with_safe_domain(self):
        from django.test import Client as DjangoTestClient
        tc = DjangoTestClient()
        resp = tc.get('/parked/?for=parked-test.com')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'parked-test.com')
        self.assertContains(resp, 'aspiredwebsites.com')

    def test_parking_page_strips_unsafe_for_param(self):
        from django.test import Client as DjangoTestClient
        tc = DjangoTestClient()
        # XSS attempt
        resp = tc.get('/parked/?for=<script>alert(1)</script>')
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, '<script>alert(1)</script>')

    def test_parking_page_works_without_for_param(self):
        from django.test import Client as DjangoTestClient
        tc = DjangoTestClient()
        resp = tc.get('/parked/')
        self.assertEqual(resp.status_code, 200)


# ── Expiration warning emails (cron) ──────────────────────────────────────

@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x', NAMECHEAP_USERNAME='x',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class ExpirationCascadeTests(TestCase):
    def setUp(self):
        from django.utils import timezone as _tz
        from datetime import timedelta
        from domains.models import DomainRegistration, NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        self.user = User.objects.create_user(
            username='ex', email='ex@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='Ex Co')
        # In grace, expires in 3 days
        self.reg = DomainRegistration.objects.create(
            client=self.profile, domain_name='ex.com', tld='com',
            status='grace',
            expires_at=_tz.now() + timedelta(days=3))

    def test_3_day_warning_fires_in_window(self):
        from django.core.management import call_command
        from io import StringIO
        with patch('domains.management.commands.reconcile_domains.sync_one'), \
             patch('domains.management.commands.reconcile_domains.get_client') as mock_get, \
             patch('domains.management.commands.reconcile_domains.send_expiring_warning_email') as mock_send:
            mock_get.return_value.get_balances.return_value = _healthy_balance()
            call_command('reconcile_domains', stdout=StringIO())
        mock_send.assert_called_once()
        args, _ = mock_send.call_args
        self.assertEqual(args[0].pk, self.reg.pk)
        self.assertEqual(args[1], 3)

    def test_outside_window_no_warning(self):
        from datetime import timedelta
        from django.core.management import call_command
        from io import StringIO
        self.reg.expires_at = timezone.now() + timedelta(days=14)
        self.reg.save()
        with patch('domains.management.commands.reconcile_domains.sync_one'), \
             patch('domains.management.commands.reconcile_domains.get_client') as mock_get, \
             patch('domains.management.commands.reconcile_domains.send_expiring_warning_email') as mock_send:
            mock_get.return_value.get_balances.return_value = _healthy_balance()
            call_command('reconcile_domains', stdout=StringIO())
        mock_send.assert_not_called()

    def test_active_status_does_not_get_expiry_warning(self):
        from django.core.management import call_command
        from io import StringIO
        self.reg.status = 'active'
        self.reg.save()
        with patch('domains.management.commands.reconcile_domains.sync_one'), \
             patch('domains.management.commands.reconcile_domains.get_client') as mock_get, \
             patch('domains.management.commands.reconcile_domains.send_expiring_warning_email') as mock_send:
            mock_get.return_value.get_balances.return_value = _healthy_balance()
            call_command('reconcile_domains', stdout=StringIO())
        mock_send.assert_not_called()


# ── Failed-renewal retry (cron) ───────────────────────────────────────────

@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x', NAMECHEAP_USERNAME='x',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class RenewRetryTests(TestCase):
    def setUp(self):
        from domains.models import DomainRegistration, NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        self.user = User.objects.create_user(
            username='rr', email='rr@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='RR Co')
        # Active row with last_api_error indicating a failed renew
        self.reg = DomainRegistration.objects.create(
            client=self.profile, domain_name='rr.com', tld='com',
            status='active',
            stripe_subscription_id='sub_rr',
            last_api_error='renew: Namecheap returned ERROR',
        )

    def test_retry_fires_when_marker_present(self):
        from django.core.management import call_command
        from io import StringIO
        with patch('domains.management.commands.reconcile_domains.sync_one'), \
             patch('domains.management.commands.reconcile_domains.get_client') as mock_get:
            mock_get.return_value.renew_domain.return_value = {
                'renewed': True,
                'charged_amount': Decimal('9'),
            }
            mock_get.return_value.get_balances.return_value = _healthy_balance()
            call_command('reconcile_domains', stdout=StringIO())
        self.reg.refresh_from_db()
        # Should have cleared the error + appended retry marker
        self.assertEqual(self.reg.last_api_error, '')
        self.assertIn('[renew-retry]', self.reg.internal_notes)

    def test_retry_stops_after_3_attempts(self):
        from django.core.management import call_command
        from io import StringIO
        # Pretend we already retried 3 times
        self.reg.internal_notes = (
            '[renew-retry] t1\n[renew-retry] t2\n[renew-retry] t3')
        self.reg.save()
        with patch('domains.management.commands.reconcile_domains.sync_one'), \
             patch('domains.management.commands.reconcile_domains.get_client') as mock_get:
            mock_get.return_value.get_balances.return_value = _healthy_balance()
            call_command('reconcile_domains', stdout=StringIO())
            # No new renew call
            mock_get.return_value.renew_domain.assert_not_called()


# ── Account balance widget + low-balance alert ────────────────────────────

@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x', NAMECHEAP_USERNAME='x',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class BalanceCheckTests(TestCase):
    def setUp(self):
        from domains.models import NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)

    def test_low_balance_triggers_admin_email(self):
        from django.core.management import call_command
        from django.core import mail
        from io import StringIO
        with patch('domains.management.commands.reconcile_domains.get_client') as mock_get:
            mock_get.return_value.get_balances.return_value = {
                'available_balance': Decimal('10'),
                'account_balance':   Decimal('10'),
                'earned_amount':     Decimal('0'),
                'withdrawable_amount': Decimal('0'),
                'funds_required_for_auto_renew': Decimal('0'),
                'currency':          'USD',
            }
            mail.outbox = []
            call_command('reconcile_domains', stdout=StringIO())
        # An admin alert should have been sent
        balance_alerts = [
            m for m in mail.outbox
            if '[Namecheap balance low]' in m.subject
        ]
        self.assertEqual(len(balance_alerts), 1)

    def test_healthy_balance_no_alert(self):
        from django.core.management import call_command
        from django.core import mail
        from io import StringIO
        with patch('domains.management.commands.reconcile_domains.get_client') as mock_get:
            mock_get.return_value.get_balances.return_value = {
                'available_balance': Decimal('100'),
                'account_balance':   Decimal('100'),
                'earned_amount':     Decimal('0'),
                'withdrawable_amount': Decimal('0'),
                'funds_required_for_auto_renew': Decimal('0'),
                'currency':          'USD',
            }
            mail.outbox = []
            call_command('reconcile_domains', stdout=StringIO())
        balance_alerts = [
            m for m in mail.outbox
            if '[Namecheap balance low]' in m.subject
        ]
        self.assertEqual(len(balance_alerts), 0)


# ── Resume view (portal) integration ──────────────────────────────────────

@override_settings(
    NAMECHEAP_SANDBOX=True,
    NAMECHEAP_API_USER='x', NAMECHEAP_API_KEY='x', NAMECHEAP_USERNAME='x',
    NAMECHEAP_CLIENT_IP='127.0.0.1',
)
class PortalResumeViewTests(TestCase):
    def setUp(self):
        from django.test import Client as DjangoTestClient
        from domains.models import DomainRegistration, NamecheapConfig
        NamecheapConfig.objects.all().delete()
        NamecheapConfig.objects.create(sandbox_mode=True)
        self.user = User.objects.create_user(
            username='prv', email='prv@example.com', password='x')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='PRV Co')
        self.reg = DomainRegistration.objects.create(
            client=self.profile, domain_name='resumetest.com', tld='com',
            status='grace')
        self.tc = DjangoTestClient()
        self.tc.force_login(self.user)

    def test_post_resume_calls_service(self):
        with patch('domains.views.resume_domain') as mock_resume:
            resp = self.tc.post(
                f'/portal/domains/{self.reg.id}/resume/')
            mock_resume.assert_called_once()
        self.assertEqual(resp.status_code, 302)

    def test_get_resume_405(self):
        resp = self.tc.get(
            f'/portal/domains/{self.reg.id}/resume/')
        # require_POST returns 405
        self.assertEqual(resp.status_code, 405)

    def test_post_resume_on_active_status_redirects_with_info(self):
        self.reg.status = 'active'
        self.reg.save()
        with patch('domains.views.resume_domain') as mock_resume:
            resp = self.tc.post(
                f'/portal/domains/{self.reg.id}/resume/')
            mock_resume.assert_not_called()
        self.assertEqual(resp.status_code, 302)
