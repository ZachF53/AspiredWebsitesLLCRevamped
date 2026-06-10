"""
Phase 5a — Social media manager tests.

Coverage map (per the plan):
  1. OAuth callback persists encrypted token (ciphertext != plaintext +
     decrypt round-trips).
  2. OAuth state mismatch → no token row created.
  3. _refresh_if_needed skips when token is fresh.
  4. _refresh_if_needed preserves the refresh token on re-consent.
  5. publish_due_posts happy path (status flips, PostResult written).
  6. publish_due_posts failure path (PostResult.success=False,
     SystemAlert recorded).
  7. post_composer AI draft uses client_location_phrase.

All external I/O is mocked: requests, claude_complete, record_alert.
Server-key encryption is exercised against a stable VAULT_SERVER_SECRET
via override_settings (matches billing/tests.py TEST_SETTINGS pattern).
"""

import datetime as _dt
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account
from clients.models import ClientProfile
from clients.service_models import SocialChannel, SocialMediaPlan

User = get_user_model()

TEST_SETTINGS = {
    # Same shape as billing/tests.py — stable secret so server-key
    # derivation is deterministic across tests.
    'VAULT_SERVER_SECRET': 'test-vault-server-secret-for-social-tests',
    'GOOGLE_CLIENT_ID':     'test-google-client-id',
    'GOOGLE_CLIENT_SECRET': 'test-google-client-secret',
}


_seq = 0


def _user(*, is_staff=True):
    """Make a unique user (admin by default — tests need staff to
    pass the @admin_required gate)."""
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f'social{_seq}', password='x',
        email=f'social{_seq}@example.com',
        is_staff=is_staff,
    )


def _channel():
    """Make a SocialChannel(platform='gbp') hanging off a fresh
    Account + SocialMediaPlan + linked ClientProfile."""
    user = _user(is_staff=False)
    account = Account.objects.create(user=user, name='Test LLC')
    # Link Account ↔ ClientProfile so post_composer can resolve the client.
    client = ClientProfile.objects.create(
        user=user, firm_name='Test LLC', city='Atlanta', state='GA')
    account.legacy_client_profile = client
    account.save(update_fields=['legacy_client_profile', 'updated_at'])
    plan = SocialMediaPlan.objects.create(
        account=account, tier_slug='social-basic', max_channels=2)
    return SocialChannel.objects.create(
        plan=plan, platform='gbp', handle='', status='pending')


@override_settings(**TEST_SETTINGS)
class OauthCallbackTests(TestCase):
    """Tests 1 + 2 — callback persists encrypted token; state mismatch
    rejected without a token row."""

    def setUp(self):
        self.staff = _user()
        self.client.force_login(self.staff)
        self.channel = _channel()

    @patch('social.google_oauth_views.requests.get')
    @patch('social.google_oauth_views.requests.post')
    def test_callback_persists_encrypted_token(self, mock_post,
                                               mock_userinfo):
        """Plaintext access token must NOT appear in the DB ciphertext
        column AND decrypt must round-trip it back."""
        from social.crypto import decrypt_token
        from social.google_oauth_views import SESSION_STATE_KEY
        from social.models import SocialToken

        plaintext_access = 'ya29.PLAIN_ACCESS_TOKEN_FOR_TEST'
        plaintext_refresh = '1//REFRESH_TOKEN_FOR_TEST'

        # Seed the session state binding as connect_start would have.
        session = self.client.session
        session[SESSION_STATE_KEY] = f'STATE123|{self.channel.id}'
        session.save()

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.raise_for_status.return_value = None
        token_resp.json.return_value = {
            'access_token':  plaintext_access,
            'refresh_token': plaintext_refresh,
            'expires_in':    3600,
            'scope':         'https://www.googleapis.com/auth/business.manage',
        }
        mock_post.return_value = token_resp

        userinfo_resp = MagicMock()
        userinfo_resp.status_code = 200
        userinfo_resp.json.return_value = {'email': 'op@example.com'}
        mock_userinfo.return_value = userinfo_resp

        url = reverse('social:oauth_callback')
        r = self.client.get(url, {'state': 'STATE123', 'code': 'AUTH_CODE'})
        self.assertEqual(r.status_code, 302)

        token = SocialToken.objects.get(channel=self.channel)
        # Ciphertext column must NOT contain the plaintext.
        self.assertNotIn(plaintext_access, token.access_token_encrypted)
        self.assertNotIn(plaintext_refresh, token.refresh_token_encrypted)
        # Decrypt MUST round-trip.
        self.assertEqual(
            decrypt_token(token.access_token_encrypted), plaintext_access)
        self.assertEqual(
            decrypt_token(token.refresh_token_encrypted), plaintext_refresh)
        self.assertEqual(token.provider_account_id, 'op@example.com')

    @patch('social.google_oauth_views.requests.post')
    def test_state_mismatch_creates_no_token(self, mock_post):
        from social.google_oauth_views import SESSION_STATE_KEY
        from social.models import SocialToken

        session = self.client.session
        session[SESSION_STATE_KEY] = f'STATE_CORRECT|{self.channel.id}'
        session.save()

        url = reverse('social:oauth_callback')
        r = self.client.get(
            url, {'state': 'STATE_WRONG', 'code': 'AUTH_CODE'})
        # 302 to a safe redirect; token row was NOT created.
        self.assertEqual(r.status_code, 302)
        mock_post.assert_not_called()
        self.assertFalse(
            SocialToken.objects.filter(channel=self.channel).exists())


@override_settings(**TEST_SETTINGS)
class RefreshIfNeededTests(TestCase):
    """Tests 3 + 4 — fresh token skips network; re-consent preserves
    the existing refresh token when Google omits a new one."""

    def setUp(self):
        from social.crypto import encrypt_token
        from social.models import SocialToken

        self.channel = _channel()
        self.token = SocialToken.objects.create(
            channel=self.channel,
            access_token_encrypted=encrypt_token('fresh-access-token'),
            refresh_token_encrypted=encrypt_token('original-refresh'),
            expires_at=timezone.now() + _dt.timedelta(minutes=30),
            scopes='https://www.googleapis.com/auth/business.manage',
        )

    @patch('social.google_gbp.requests.post')
    def test_skips_when_token_fresh(self, mock_post):
        from social.google_gbp import _refresh_if_needed
        _refresh_if_needed(self.token)
        # Token expires in 30 min > 30s buffer → no network call.
        mock_post.assert_not_called()

    @patch('social.google_gbp.requests.post')
    def test_force_refresh_keeps_refresh_token(self, mock_post):
        """Forced refresh: existing encrypted refresh_token is preserved
        even when Google's refresh response omits a new one (matches
        Google's actual behaviour)."""
        from social.crypto import decrypt_token
        from social.google_gbp import _refresh_if_needed

        self.token.expires_at = timezone.now() - _dt.timedelta(minutes=5)
        self.token.save()

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            'access_token': 'new-access-token',
            'expires_in':   3600,
        }
        mock_post.return_value = resp

        _refresh_if_needed(self.token)

        self.token.refresh_from_db()
        self.assertEqual(
            decrypt_token(self.token.access_token_encrypted),
            'new-access-token')
        # Original refresh token preserved.
        self.assertEqual(
            decrypt_token(self.token.refresh_token_encrypted),
            'original-refresh')


@override_settings(**TEST_SETTINGS)
class PublishDuePostsTests(TestCase):
    """Tests 5 + 6 — auto-publisher happy path AND failure path
    (status flips, PostResult written, SystemAlert recorded on fail)."""

    def setUp(self):
        from social.crypto import encrypt_token
        from social.models import ScheduledPost, SocialToken

        self.channel = _channel()
        # Channel needs a GBP location resource name to publish.
        self.channel.handle = 'accounts/111/locations/222'
        self.channel.save()
        self.client_obj = self.channel.plan.account.legacy_client_profile
        SocialToken.objects.create(
            channel=self.channel,
            access_token_encrypted=encrypt_token('ya29.test'),
            refresh_token_encrypted=encrypt_token('refresh.test'),
            expires_at=timezone.now() + _dt.timedelta(hours=1),
        )
        self.post = ScheduledPost.objects.create(
            channel=self.channel,
            client=self.client_obj,
            body='Test post body.',
            scheduled_for=timezone.now() - _dt.timedelta(minutes=1),
            status='scheduled',
        )

    @patch('social.tasks._publish_gbp')
    def test_happy_path_flips_to_published(self, mock_publish):
        from social.tasks import publish_due_posts

        mock_publish.return_value = ('gbp_post_123',
                                     'https://posts.gle/abc', '')

        handled = publish_due_posts()
        self.assertEqual(handled, 1)

        self.post.refresh_from_db()
        self.assertEqual(self.post.status, 'published')
        self.assertIsNotNone(self.post.published_at)
        result = self.post.results.first()
        self.assertIsNotNone(result)
        self.assertTrue(result.success)
        self.assertEqual(result.provider_post_id, 'gbp_post_123')
        self.assertEqual(result.permalink, 'https://posts.gle/abc')

    @patch('social.tasks._publish_gbp')
    @patch('core.system_alerts.record_alert')
    def test_failure_records_postresult_and_alert(self, mock_alert,
                                                  mock_publish):
        from social.tasks import publish_due_posts

        mock_publish.return_value = ('', '', 'simulated publish failure')

        publish_due_posts()

        self.post.refresh_from_db()
        self.assertEqual(self.post.status, 'failed')
        result = self.post.results.first()
        self.assertIsNotNone(result)
        self.assertFalse(result.success)
        self.assertIn('simulated publish failure', result.error_detail)
        # SystemAlert recorded so dashboard surfaces it.
        mock_alert.assert_called_once()
        called_with = mock_alert.call_args.kwargs
        self.assertEqual(called_with.get('severity'), 'error')


@override_settings(**TEST_SETTINGS, ANTHROPIC_API_KEY='dummy')
class AIComposerTests(TestCase):
    """Test 7 — AI draft uses client_location_phrase in its system arg."""

    def setUp(self):
        self.staff = _user()
        self.client.force_login(self.staff)
        self.channel = _channel()

    @patch('social.ai.claude_complete')
    def test_ai_draft_includes_location_in_system_prompt(self, mock_complete):
        from social.ai import generate_post_draft
        mock_complete.return_value = 'A draft post body.'
        client = self.channel.plan.account.legacy_client_profile
        # client.city='Atlanta', state='GA' → phrase = ' based in Atlanta, GA'
        generate_post_draft(client, 'estate planning tips')

        # The system prompt passed to claude_complete must include the
        # location phrase so Claude grounds output locally.
        kwargs = mock_complete.call_args.kwargs
        system = kwargs.get('system') or ''
        self.assertIn('Atlanta, GA', system,
                      f'system prompt missing location phrase: {system!r}')
