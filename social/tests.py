"""
Phase 5b/5c — Social media manager tests.

Coverage:
  - Token encryption round-trip (crypto.encrypt_token + decrypt_token)
  - Meta OAuth state mismatch rejection (no token written)
  - LinkedIn OAuth state mismatch rejection (no token written)
  - publish_due_posts dispatches by platform + records PostResult
  - publish_due_posts: publisher exception -> failed + PostResult
  - publish_due_posts: unwired platform -> failed (no infinite loop)
  - generate_post_draft truncates to platform limit
  - generate_post_draft propagates AINotConfigured cleanly

External I/O is mocked at the source module path (e.g.
'social.meta_publisher.publish_facebook_post', not 'social.tasks.*').
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account
from clients.models import ClientProfile
from clients.service_models import SocialChannel, SocialMediaPlan
from social.crypto import decrypt_token, encrypt_token
from social.models import PostResult, ScheduledPost, SocialToken


User = get_user_model()


def _seq(prefix='x'):
    """Per-test unique suffix — avoids collisions across tests."""
    import secrets
    return f'{prefix}-{secrets.token_hex(4)}'


def _user(username='admin'):
    return User.objects.create_user(
        username=_seq(username),
        email=f'{_seq(username)}@aspiredwebsites.com',
        password='x',
        is_staff=True,
    )


def _plan_channel(platform):
    """Create a ClientProfile + Account + Plan + Channel for testing."""
    user = User.objects.create_user(
        username=_seq('client'),
        email=f'{_seq("client")}@law.example',
        password='x',
    )
    client = ClientProfile.objects.create(
        user=user, firm_name='Johnson Law',
        package='maintenance_growth',
        business_type='law_firm',
    )
    account, _ = Account.objects.get_or_create(
        user=user, defaults={'name': _seq('Acct')},
    )
    plan = SocialMediaPlan.objects.create(
        account=account,
        tier_slug='social-standard',
        status='active',
    )
    channel = SocialChannel.objects.create(
        plan=plan,
        platform=platform,
        handle=_seq('@handle'),
    )
    return client, plan, channel


# ── Crypto ──────────────────────────────────────────────────────────────────


class TokenCryptoTests(TestCase):
    def test_encrypt_decrypt_round_trip(self):
        secret = 'ya29.example-access-token'
        cipher = encrypt_token(secret)
        self.assertNotIn(secret, cipher)
        self.assertEqual(decrypt_token(cipher), secret)

    def test_empty_plaintext_returns_empty(self):
        self.assertEqual(encrypt_token(''), '')
        self.assertEqual(decrypt_token(''), '')

    def test_decrypt_garbage_returns_empty(self):
        self.assertEqual(decrypt_token('not-valid-hex'), '')

    @override_settings(VAULT_SERVER_SECRET='')
    def test_encrypt_raises_friendly_error_without_secret(self):
        with self.assertRaises(RuntimeError) as cm:
            encrypt_token('foo')
        self.assertIn('VAULT_SERVER_SECRET', str(cm.exception))


# ── OAuth state CSRF ────────────────────────────────────────────────────────


class MetaOAuthStateTests(TestCase):
    def test_callback_rejects_mismatched_state(self):
        _client, _plan, channel = _plan_channel('facebook')
        admin = _user()
        self.client.force_login(admin)

        session = self.client.session
        session['social_meta_oauth_state'] = f'expected|{channel.id}'
        session.save()

        resp = self.client.get(
            reverse('social:meta_oauth_callback'),
            {'state': 'wrong', 'code': 'abc'},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(SocialToken.objects.count(), 0)


class LinkedInOAuthStateTests(TestCase):
    def test_callback_rejects_mismatched_state(self):
        _client, _plan, channel = _plan_channel('linkedin')
        admin = _user()
        self.client.force_login(admin)

        session = self.client.session
        session['social_linkedin_oauth_state'] = f'expected|{channel.id}'
        session.save()

        resp = self.client.get(
            reverse('social:linkedin_oauth_callback'),
            {'state': 'wrong', 'code': 'abc'},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(SocialToken.objects.count(), 0)


# ── publish_due_posts ──────────────────────────────────────────────────────


class PublishDuePostsTests(TestCase):
    def _due_post(self, platform, with_token=True):
        client, _plan, channel = _plan_channel(platform)
        if with_token:
            SocialToken.objects.create(
                channel=channel,
                access_token_encrypted=encrypt_token(
                    'fake-access-token'),
                provider_account_id='1234567890',
            )
        import datetime as _dt
        return ScheduledPost.objects.create(
            channel=channel,
            client=client,
            body='Hello world',
            scheduled_for=timezone.now() - _dt.timedelta(minutes=1),
            status='scheduled',
        )

    @patch('social.meta_publisher.publish_facebook_post')
    def test_dispatches_facebook(self, mock_fb):
        mock_fb.return_value = {
            'provider_post_id': 'fb_123',
            'permalink': 'https://www.facebook.com/post/fb_123/',
        }
        post = self._due_post('facebook')
        from social.tasks import publish_due_posts
        result = publish_due_posts()
        self.assertEqual(result, {'published': 1, 'failed': 0})
        post.refresh_from_db()
        self.assertEqual(post.status, 'published')
        pr = PostResult.objects.get(scheduled_post=post)
        self.assertEqual(pr.provider_post_id, 'fb_123')
        self.assertTrue(pr.success)

    @patch('social.linkedin_publisher.publish_linkedin_post')
    def test_dispatches_linkedin(self, mock_li):
        mock_li.return_value = {
            'provider_post_id': 'urn:li:share:111',
            'permalink': 'https://www.linkedin.com/feed/update/urn:li:share:111/',
        }
        post = self._due_post('linkedin')
        from social.tasks import publish_due_posts
        publish_due_posts()
        post.refresh_from_db()
        self.assertEqual(post.status, 'published')

    @patch('social.meta_publisher.publish_facebook_post')
    def test_publisher_exception_marks_failed(self, mock_fb):
        mock_fb.side_effect = RuntimeError('Meta said no')
        post = self._due_post('facebook')
        from social.tasks import publish_due_posts
        publish_due_posts()
        post.refresh_from_db()
        self.assertEqual(post.status, 'failed')
        pr = PostResult.objects.get(scheduled_post=post)
        self.assertFalse(pr.success)
        self.assertIn('Meta said no', pr.error_detail)

    def test_unwired_platform_marks_failed(self):
        """'twitter' is in PLATFORM_CHOICES but has no publisher in
        5b/5c. Should mark the row failed so it doesn't loop."""
        post = self._due_post('twitter')
        from social.tasks import publish_due_posts
        publish_due_posts()
        post.refresh_from_db()
        self.assertEqual(post.status, 'failed')


# ── AI degradation ─────────────────────────────────────────────────────────


class AIContentTests(TestCase):
    @patch('social.ai.claude_complete')
    def test_generate_post_draft_truncates_to_platform_limit(
            self, mock_cc):
        mock_cc.return_value = 'x' * 5000
        client = ClientProfile.objects.create(
            user=_user('client'),
            firm_name='Johnson Law',
            package='maintenance_growth',
            business_type='law_firm',
        )
        from social.ai import generate_post_draft
        body = generate_post_draft(
            client, 'twitter', 'announce new page')
        self.assertLessEqual(len(body), 280)

    def test_generate_post_draft_propagates_ainotconfigured(self):
        from reporting.ai import AINotConfigured
        with patch('social.ai.claude_complete',
                   side_effect=AINotConfigured('no key')):
            client = ClientProfile.objects.create(
                user=_user('client'),
                firm_name='Johnson Law',
                package='maintenance_growth',
                business_type='law_firm',
            )
            from social.ai import generate_post_draft
            with self.assertRaises(AINotConfigured):
                generate_post_draft(client, 'facebook', 'announce')
