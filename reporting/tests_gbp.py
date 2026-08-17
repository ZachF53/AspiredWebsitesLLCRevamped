"""
Phase 5a-pivot — GBP feature tests.

Coverage:
  - Tier gating: has_gbp_features / has_gbp_premium_features
  - Token encrypt/decrypt round-trip via reporting.google_gbp helpers
  - OAuth callback persists encrypted token (operator-level)
  - OAuth state mismatch rejected (no token row)
  - sync_gbp_reviews flags low-star + unreplied
  - check_gbp_nap detects drift between our record + GBP listing
  - upgrade-required guard for sites on non-eligible tiers

Scoped per Website: a Google listing describes one business location, so
`gbp_location_name` and the tier gate both live on the site. Name, phone
and address still come from the Account — they describe the business,
not the site.
"""

import datetime as _dt
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Website
from clients.models import ClientProfile

User = get_user_model()


TEST_SETTINGS = {
    'VAULT_SERVER_SECRET': 'test-vault-server-secret-for-gbp-tests',
    'GOOGLE_CLIENT_ID':     'test-google-client-id',
    'GOOGLE_CLIENT_SECRET': 'test-google-client-secret',
}


_seq = 0


def _user(*, is_staff=True):
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f'gbp{_seq}', password='x',
        email=f'gbp{_seq}@example.com',
        is_staff=is_staff,
    )


def _client(*, package='maintenance_growth', **kw):
    """ClientProfile with the eligible default tier."""
    user = _user(is_staff=False)
    return ClientProfile.objects.create(
        user=user, firm_name='Test LLC',
        phone='210-555-0100',
        address='123 Main St',
        website='https://test.example',
        package=package,
        **kw,
    )


def _site(*, package='maintenance_growth', account_kw=None, **kw):
    """A Website on its own Account, with the eligible default tier.

    Goes through ClientProfile because the autocreate signal is what
    materialises the Account and its first Website; creating an Account
    directly would leave the two out of step with production.
    """
    profile = _client(package=package)
    account = profile.migrated_account
    for name, value in (account_kw or {}).items():
        setattr(account, name, value)
    if account_kw:
        account.save()
    site = account.websites.first()
    if site is None:
        site = Website.objects.create(account=account, name=account.name)
    site.package = package
    site.url = 'https://test.example'
    for name, value in kw.items():
        setattr(site, name, value)
    site.save()
    return site


# ─────────────────────────────────────────────────────────────────────────────
# Tier gating
# ─────────────────────────────────────────────────────────────────────────────

class TierGatingTests(TestCase):
    def test_growth_has_gbp_features(self):
        c = _client(package='maintenance_growth')
        self.assertTrue(c.has_gbp_features())
        self.assertFalse(c.has_gbp_premium_features())

    def test_dominant_has_both_levels(self):
        c = _client(package='maintenance_dominant')
        self.assertTrue(c.has_gbp_features())
        self.assertTrue(c.has_gbp_premium_features())

    def test_essentials_excluded(self):
        c = _client(package='maintenance_essentials')
        self.assertFalse(c.has_gbp_features())
        self.assertFalse(c.has_gbp_premium_features())

    def test_no_package_excluded(self):
        c = _client(package='')
        self.assertFalse(c.has_gbp_features())

    def test_comp_growth_unlocks_features(self):
        """Operator can comp a tier without billing — has_gbp_features
        returns True via comp_package even when `package` doesn't qualify."""
        c = _client(package='premium_build', comp_package='maintenance_growth')
        self.assertTrue(c.has_gbp_features())
        self.assertFalse(c.has_gbp_premium_features())

    def test_comp_dominant_unlocks_premium(self):
        c = _client(package='', comp_package='maintenance_dominant')
        self.assertTrue(c.has_gbp_features())
        self.assertTrue(c.has_gbp_premium_features())

    def test_comp_essentials_does_not_unlock_gbp(self):
        """Comping Essentials still excludes GBP — only Growth/Dominant qualify."""
        c = _client(package='', comp_package='maintenance_essentials')
        self.assertFalse(c.has_gbp_features())


@override_settings(**TEST_SETTINGS)
class CryptoRoundTripTests(TestCase):
    def test_encrypt_decrypt_roundtrip(self):
        from reporting.google_gbp import decrypt_token, encrypt_token
        plain = 'ya29.PLAIN_ACCESS_TOKEN'
        cipher = encrypt_token(plain)
        self.assertNotIn(plain, cipher)
        self.assertEqual(decrypt_token(cipher), plain)


@override_settings(**TEST_SETTINGS)
class OauthCallbackTests(TestCase):
    def setUp(self):
        self.staff = _user()
        self.client.force_login(self.staff)

    @patch('reporting.gbp_oauth_views.requests.get')
    @patch('reporting.gbp_oauth_views.requests.post')
    def test_callback_persists_encrypted_token(self, mock_post,
                                               mock_userinfo):
        from reporting.gbp_oauth_views import SESSION_STATE_KEY
        from reporting.google_gbp import decrypt_token
        from reporting.models import GbpOperatorToken

        plain_access = 'ya29.PLAIN_ACCESS'
        plain_refresh = '1//PLAIN_REFRESH'

        session = self.client.session
        session[SESSION_STATE_KEY] = 'STATE_OK'
        session.save()

        tok = MagicMock()
        tok.status_code = 200
        tok.raise_for_status.return_value = None
        tok.json.return_value = {
            'access_token':  plain_access,
            'refresh_token': plain_refresh,
            'expires_in':    3600,
            'scope':         'https://www.googleapis.com/auth/business.manage',
        }
        mock_post.return_value = tok

        ui = MagicMock()
        ui.status_code = 200
        ui.json.return_value = {'email': 'op@example.com'}
        mock_userinfo.return_value = ui

        r = self.client.get(
            reverse('gbp:oauth_callback'),
            {'state': 'STATE_OK', 'code': 'CODE'})
        self.assertEqual(r.status_code, 302)

        token = GbpOperatorToken.objects.get(user=self.staff)
        self.assertNotIn(plain_access, token.access_token_encrypted)
        self.assertEqual(decrypt_token(token.access_token_encrypted),
                         plain_access)
        self.assertEqual(decrypt_token(token.refresh_token_encrypted),
                         plain_refresh)
        self.assertEqual(token.provider_account_email, 'op@example.com')

    @patch('reporting.gbp_oauth_views.requests.post')
    def test_state_mismatch_no_token(self, mock_post):
        from reporting.gbp_oauth_views import SESSION_STATE_KEY
        from reporting.models import GbpOperatorToken

        session = self.client.session
        session[SESSION_STATE_KEY] = 'CORRECT'
        session.save()

        r = self.client.get(
            reverse('gbp:oauth_callback'),
            {'state': 'WRONG', 'code': 'CODE'})
        self.assertEqual(r.status_code, 302)
        mock_post.assert_not_called()
        self.assertFalse(GbpOperatorToken.objects.exists())


@override_settings(**TEST_SETTINGS)
class ReviewSyncTests(TestCase):
    def setUp(self):
        from reporting.google_gbp import encrypt_token
        from reporting.models import GbpOperatorToken
        self.staff = _user()
        GbpOperatorToken.objects.create(
            user=self.staff,
            access_token_encrypted=encrypt_token('ya29.test'),
            refresh_token_encrypted=encrypt_token('refresh.test'),
            expires_at=timezone.now() + _dt.timedelta(hours=1),
        )
        self.site = _site(
            package='maintenance_growth',
            gbp_location_name='accounts/111/locations/222',
        )

    @patch('reporting.google_gbp.list_reviews')
    def test_low_star_flagged(self, mock_list):
        from reporting.models import GbpReview
        from reporting.tasks_gbp import sync_gbp_reviews_task

        mock_list.return_value = [{
            'reviewId':   'r1',
            'reviewer':   {'displayName': 'Sam'},
            'starRating': 'TWO',
            'comment':    'It was fine.',
            'createTime': '2026-06-01T10:00:00Z',
        }]
        sync_gbp_reviews_task()

        rev = GbpReview.objects.get(provider_review_id='r1')
        self.assertEqual(rev.star_rating, 2)
        self.assertTrue(rev.needs_attention)
        self.assertEqual(rev.needs_attention_reason, 'low_star')

    @patch('reporting.google_gbp.list_reviews')
    def test_unreplied_flagged(self, mock_list):
        from reporting.models import GbpReview
        from reporting.tasks_gbp import sync_gbp_reviews_task

        mock_list.return_value = [{
            'reviewId':   'r2',
            'reviewer':   {'displayName': 'Pat'},
            'starRating': 'FIVE',
            'comment':    'Loved the service.',
            'createTime': '2026-06-01T10:00:00Z',
        }]
        sync_gbp_reviews_task()

        rev = GbpReview.objects.get(provider_review_id='r2')
        self.assertEqual(rev.star_rating, 5)
        self.assertTrue(rev.needs_attention)
        self.assertEqual(rev.needs_attention_reason, 'unreplied')

    @patch('reporting.google_gbp.list_reviews')
    def test_replied_not_flagged(self, mock_list):
        from reporting.models import GbpReview
        from reporting.tasks_gbp import sync_gbp_reviews_task

        mock_list.return_value = [{
            'reviewId':    'r3',
            'reviewer':    {'displayName': 'Jordan'},
            'starRating':  'FIVE',
            'comment':     'Great.',
            'createTime':  '2026-06-01T10:00:00Z',
            'reviewReply': {'comment': 'Thanks!',
                            'updateTime': '2026-06-02T11:00:00Z'},
        }]
        sync_gbp_reviews_task()

        rev = GbpReview.objects.get(provider_review_id='r3')
        self.assertFalse(rev.needs_attention)


@override_settings(**TEST_SETTINGS)
class NapCheckTests(TestCase):
    def setUp(self):
        from reporting.google_gbp import encrypt_token
        from reporting.models import GbpOperatorToken
        self.staff = _user()
        GbpOperatorToken.objects.create(
            user=self.staff,
            access_token_encrypted=encrypt_token('ya29.test'),
            refresh_token_encrypted=encrypt_token('refresh.test'),
            expires_at=timezone.now() + _dt.timedelta(hours=1),
        )

    @patch('reporting.google_gbp.fetch_location')
    def test_nap_drift_detected(self, mock_fetch):
        """Account.phone differs from GBP phone → mismatch row."""
        from reporting.models import GBPSyncCheck
        from reporting.tasks_gbp import check_gbp_nap_task

        site = _site(
            package='maintenance_growth',
            account_kw={'phone': '210-555-9999', 'name': 'Test LLC'},
            gbp_location_name='accounts/111/locations/222',
        )

        mock_fetch.return_value = {
            'title':            'Test LLC',
            'phoneNumbers':     {'primaryPhone': '210-555-0000'},  # different
            'storefrontAddress': {'addressLines': ['123 Main St']},
            'websiteUri':       'https://test.example',
        }

        check_gbp_nap_task()

        phone_check = GBPSyncCheck.objects.get(
            website_new=site, field_name='phone')
        self.assertTrue(phone_check.is_mismatch)
        name_check = GBPSyncCheck.objects.get(
            website_new=site, field_name='business_name')
        self.assertFalse(name_check.is_mismatch)

    @patch('reporting.google_gbp.fetch_location')
    def test_the_site_url_is_compared_not_the_accounts(self, mock_fetch):
        """The reason the check is per-site. Two sites under one account
        have different URLs; a listing pointing at the wrong one sends
        the client's callers to the wrong brand, and an account-level
        comparison could never see it."""
        from reporting.models import GBPSyncCheck
        from reporting.tasks_gbp import check_gbp_nap_task

        site = _site(
            package='maintenance_growth',
            gbp_location_name='accounts/111/locations/333',
        )
        site.url = 'https://mediation.example'
        site.save()

        mock_fetch.return_value = {
            'title':            site.account.name,
            'phoneNumbers':     {'primaryPhone': ''},
            'storefrontAddress': {'addressLines': []},
            'websiteUri':       'https://familylaw.example',
        }

        check_gbp_nap_task()

        url_check = GBPSyncCheck.objects.get(
            website_new=site, field_name='website')
        self.assertTrue(url_check.is_mismatch)
        self.assertEqual(url_check.website_value, 'https://mediation.example')


class UpgradeRequiredTests(TestCase):
    def setUp(self):
        self.staff = _user()
        self.client.force_login(self.staff)

    def test_essentials_client_sees_upgrade_page(self):
        site = _site(package='maintenance_essentials')
        r = self.client.get(
            reverse('gbp:client_gbp', kwargs={'website_id': site.id}))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'not in this tier')

    def test_growth_client_sees_real_page(self):
        site = _site(package='maintenance_growth')
        r = self.client.get(
            reverse('gbp:client_gbp', kwargs={'website_id': site.id}))
        self.assertEqual(r.status_code, 200)
        # Should NOT contain the upgrade banner heading
        self.assertNotContains(r, 'not in this tier')

    def test_a_comped_account_grants_its_sites_the_feature(self):
        """Comps live on the Account, the billed package on the Website.
        Reading only the site's own package would silently drop every
        operator-granted entitlement."""
        site = _site(package='')
        site.account.comp_maintenance_package = 'maintenance_growth'
        site.account.save()
        r = self.client.get(
            reverse('gbp:client_gbp', kwargs={'website_id': site.id}))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'not in this tier')
