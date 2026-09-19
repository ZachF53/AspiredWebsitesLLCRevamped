"""
v2 website-create — the "existing client, no build" shape.

Denis Law Group is the motivating case: an existing Account with no
Website, no Contract, no intake, who needs a Website carrying
stage='live', payment_status='fully_paid', onboarding_status=
'intake_complete', build_platform='wordpress', and her real URL — in one
step, because build_platform has no clean edit path after creation (v1's
generic field editor doesn't include it; changing it there means going
through Send Contract, which is wrong for a no-build client) and no other
v2 page can set onboarding_status/payment_status/stage/url on an existing
Website at all.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from clients.account_models import Account, Website

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class NoBuildWebsiteCreateTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username='v2staff2', email='v2staff2@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)

        u = User.objects.create_user(
            username='denislaw2', email='aisha@denislawgroup.example.com',
            password=None, is_active=True)
        cls.account = Account.objects.create(
            user=u, name='Denis Law Group',
            onboarding_status='complete', onboarding_complete=False)

    def setUp(self):
        self.client.force_login(self.staff)

    def test_create_form_get_renders(self):
        r = self.client.get(reverse('admin_dashboard:v2_website_create'))
        self.assertEqual(r.status_code, 200)
        content = r.content.decode()
        self.assertIn('Existing client', content)
        self.assertIn('website_create.js', content)

    # ── item 10 ──────────────────────────────────────────────────────────

    def test_no_build_shape_creates_exact_field_values(self):
        r = self.client.post(
            reverse('admin_dashboard:v2_website_create'), {
                'account_id': str(self.account.id),
                'name': 'Denis Law Group',
                'build_platform': 'wordpress',
                'url': 'https://denislawgroup.com',
                'stage': 'live',
                'payment_status': 'fully_paid',
                'onboarding_status': 'intake_complete',
            })
        self.assertEqual(r.status_code, 302)

        site = Website.objects.get(account=self.account)
        self.assertEqual(site.stage, 'live')
        self.assertEqual(site.payment_status, 'fully_paid')
        self.assertEqual(site.onboarding_status, 'intake_complete')
        self.assertEqual(site.build_platform, 'wordpress')
        self.assertEqual(site.url, 'https://denislawgroup.com')
        self.assertEqual(site.account_id, self.account.id)

        # No subscription id was set anywhere in this flow — the guards
        # must therefore see this row as unblocked.
        self.assertEqual(site.stripe_hosting_subscription_id, '')
        self.assertEqual(site.stripe_maintenance_subscription_id, '')

    def test_no_build_shape_fires_no_signal_no_celery_no_email(self):
        """Same single Website.objects.create() call as before — this
        form growing three more optional kwargs does not add a second
        write or touch any signal-bearing path."""
        from django.core import mail

        with patch('sync.signals.SyncJob.objects.create') as mock_sync:
            r = self.client.post(
                reverse('admin_dashboard:v2_website_create'), {
                    'account_id': str(self.account.id),
                    'name': 'Denis Law Group',
                    'build_platform': 'wordpress',
                    'url': 'https://denislawgroup.com',
                    'stage': 'live',
                    'payment_status': 'fully_paid',
                    'onboarding_status': 'intake_complete',
                })
        self.assertEqual(r.status_code, 302)
        mock_sync.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)

    def test_no_build_shape_renders_across_v2_tabs(self):
        r = self.client.post(
            reverse('admin_dashboard:v2_website_create'), {
                'account_id': str(self.account.id),
                'name': 'Denis Law Group',
                'build_platform': 'wordpress',
                'url': 'https://denislawgroup.com',
                'stage': 'live',
                'payment_status': 'fully_paid',
                'onboarding_status': 'intake_complete',
            })
        site = Website.objects.get(account=self.account)

        for tab in ('overview', 'onboarding', 'intake', 'security',
                    'domains', 'billing', 'monitoring'):
            resp = self.client.get(
                reverse('admin_dashboard:v2_website_detail',
                        args=[site.id]),
                {'tab': tab})
            self.assertEqual(
                resp.status_code, 200, f'tab={tab} did not render')

        # item 9 — Infrastructure tab hidden for a wordpress site, and
        # asking for it directly does not render Infrastructure content.
        list_resp = self.client.get(
            reverse('admin_dashboard:v2_website_detail', args=[site.id]))
        self.assertNotIn('?tab=infrastructure', list_resp.content.decode())

        infra_resp = self.client.get(
            reverse('admin_dashboard:v2_website_detail', args=[site.id]),
            {'tab': 'infrastructure'})
        # 'Droplet' alone also matches the persistent sidebar's "Droplets"
        # nav link, present on every admin page regardless of this tab —
        # the admin-card title is what actually proves the tab body did
        # not render.
        self.assertNotIn(
            'admin-card__title">Droplet<', infra_resp.content.decode())

    def test_no_build_shape_leaves_live_subscription_guards_intact(self):
        """A freshly created no-build Website has no subscription id, so
        v2 write actions must NOT be blocked for it — confirming the
        guard still keys correctly off the (empty) subscription fields,
        not off stage/payment_status/onboarding_status."""
        r = self.client.post(
            reverse('admin_dashboard:v2_website_create'), {
                'account_id': str(self.account.id),
                'name': 'Denis Law Group',
                'build_platform': 'wordpress',
                'stage': 'live',
                'payment_status': 'fully_paid',
                'onboarding_status': 'intake_complete',
            })
        site = Website.objects.get(account=self.account)

        # An ordinary v2 write action (toggle auto-send-scan) must go
        # through, not get refused by the live-subscription guard.
        toggle = self.client.post(
            reverse('admin_dashboard:v2_website_toggle_auto_send_scan',
                    args=[site.id]),
            follow=True)
        self.assertEqual(toggle.status_code, 200)
        self.assertNotIn(
            'live Stripe subscription', toggle.content.decode())

        # And if a subscription id later appears on this same row, the
        # guard must still block, proving this test didn't just disable
        # the check.
        site.stripe_maintenance_subscription_id = 'sub_fake123'
        site.save(update_fields=['stripe_maintenance_subscription_id'])
        blocked = self.client.post(
            reverse('admin_dashboard:v2_website_toggle_auto_send_scan',
                    args=[site.id]),
            follow=True)
        self.assertIn('live Stripe subscription', blocked.content.decode())

    def test_invalid_lifecycle_value_is_rejected_creates_nothing(self):
        r = self.client.post(
            reverse('admin_dashboard:v2_website_create'), {
                'account_id': str(self.account.id),
                'name': 'Denis Law Group',
                'build_platform': 'wordpress',
                'stage': 'not-a-real-stage',
            })
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Website.objects.filter(account=self.account).exists())

    # ── item 11 — normal build-client path is unchanged ────────────────

    def test_normal_build_path_still_defaults_to_model_defaults(self):
        u = User.objects.create_user(
            username='buildclient', email='build@example.com',
            password='x')
        account = Account.objects.create(user=u, name='Normal Build Co')

        r = self.client.post(
            reverse('admin_dashboard:v2_website_create'), {
                'account_id': str(account.id),
                'name': 'Normal Build Site',
                'build_platform': 'custom',
            })
        self.assertEqual(r.status_code, 302)

        site = Website.objects.get(account=account)
        self.assertEqual(site.stage, 'intake')
        self.assertEqual(site.payment_status, 'awaiting_deposit')
        self.assertEqual(site.onboarding_status, 'pending_intake')
        self.assertEqual(site.build_platform, 'custom')
        self.assertEqual(site.url, '')
        # Unchanged redirect target too.
        self.assertIn(f'/admin-dashboard/websites/{site.id}/', r.url)
