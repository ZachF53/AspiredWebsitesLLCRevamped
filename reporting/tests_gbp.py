"""
Phase 5a-pivot — GBP feature tests.

Coverage:
  - Tier gating: has_gbp_features / has_gbp_premium_features
  - Token encrypt/decrypt round-trip via reporting.google_gbp helpers
  - OAuth callback persists encrypted token (operator-level)
  - OAuth state mismatch rejected (no token row)
  - sync_gbp_reviews flags low-star + unreplied
  - check_gbp_nap detects drift between client record + GBP listing
  - upgrade-required guard for clients on non-eligible tiers
"""

import datetime as _dt
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

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
        self.profile = _client(
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
        """ClientProfile.phone differs from GBP phone → mismatch row."""
        from reporting.models import GBPSyncCheck
        from reporting.tasks_gbp import check_gbp_nap_task

        profile = _client(
            package='maintenance_growth',
            gbp_location_name='accounts/111/locations/222',
        )
        profile.phone = '210-555-9999'
        profile.firm_name = 'Test LLC'
        profile.save()

        mock_fetch.return_value = {
            'title':            'Test LLC',
            'phoneNumbers':     {'primaryPhone': '210-555-0000'},  # different
            'storefrontAddress': {'addressLines': ['123 Main St']},
            'websiteUri':       'https://test.example',
        }

        check_gbp_nap_task()

        phone_check = GBPSyncCheck.objects.get(
            client=profile, field_name='phone')
        self.assertTrue(phone_check.is_mismatch)
        name_check = GBPSyncCheck.objects.get(
            client=profile, field_name='business_name')
        self.assertFalse(name_check.is_mismatch)


class UpgradeRequiredTests(TestCase):
    def setUp(self):
        self.staff = _user()
        self.client.force_login(self.staff)

    def test_essentials_client_sees_upgrade_page(self):
        profile = _client(package='maintenance_essentials')
        r = self.client.get(
            reverse('gbp:client_gbp', kwargs={'client_id': profile.id}))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'not in this tier')

    def test_growth_client_sees_real_page(self):
        profile = _client(package='maintenance_growth')
        r = self.client.get(
            reverse('gbp:client_gbp', kwargs={'client_id': profile.id}))
        self.assertEqual(r.status_code, 200)
        # Should NOT contain the upgrade banner heading
        self.assertNotContains(r, 'not in this tier')
