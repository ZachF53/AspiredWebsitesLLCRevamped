"""
Regression cover for reconcile_subscriptions treating "no droplet record"
as "droplet confirmed dead" for WordPress-hosted sites, which have no
droplet at all (needs_droplet is False). Before the fix, such a site
would have its live hosting subscription cancelled by the nightly cron
the first time it acquired a subscription id.
"""

from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from clients.account_models import Account, Website

User = get_user_model()


class ReconcileSubscriptionsWordpressSkipTests(TestCase):
    def setUp(self):
        user = User.objects.create_user(
            username='reconcile-wp', email='reconcile-wp@example.com',
            password='test-pass-123')
        self.account = Account.objects.filter(user=user).first() or (
            Account.objects.create(user=user, name='WP Reconcile Co'))

    def test_wordpress_no_droplet_not_cancelled_or_flagged(self):
        website = Website.objects.create(
            account=self.account, name='WP Site',
            build_platform='wordpress',
            stripe_hosting_subscription_id='sub_wp123',
            do_droplet_id='',
        )
        with patch('billing.webhooks._droplet_alive') as mock_alive, \
                patch('billing.stripe_helpers.cancel_hosting_subscription'
                      ) as mock_cancel:
            out = StringIO()
            call_command('reconcile_subscriptions', stdout=out)

        mock_alive.assert_not_called()
        mock_cancel.assert_not_called()
        website.refresh_from_db()
        self.assertEqual(website.stripe_hosting_subscription_id, 'sub_wp123')
        self.assertNotIn('DRIFT', out.getvalue())
        self.assertIn('1 skipped', out.getvalue())

    def test_custom_build_confirmed_dead_droplet_still_cancels(self):
        website = Website.objects.create(
            account=self.account, name='Custom Site',
            build_platform='custom',
            stripe_hosting_subscription_id='sub_custom123',
            do_droplet_id='12345',
        )
        with patch('billing.webhooks._droplet_alive', return_value=False
                    ) as mock_alive, \
                patch('billing.stripe_helpers.cancel_hosting_subscription'
                      ) as mock_cancel:
            out = StringIO()
            call_command('reconcile_subscriptions', stdout=out)

        mock_alive.assert_called_once_with(website)
        mock_cancel.assert_called_once()
        self.assertIn('DRIFT', out.getvalue())
        self.assertIn('1 cancelled', out.getvalue())

    def test_dry_run_does_not_cancel(self):
        Website.objects.create(
            account=self.account, name='Custom Dry Site',
            build_platform='custom',
            stripe_hosting_subscription_id='sub_custom456',
            do_droplet_id='67890',
        )
        with patch('billing.webhooks._droplet_alive', return_value=False), \
                patch('billing.stripe_helpers.cancel_hosting_subscription'
                      ) as mock_cancel:
            out = StringIO()
            call_command('reconcile_subscriptions', '--dry-run', stdout=out)

        mock_cancel.assert_not_called()
        self.assertIn('[DRY-RUN]', out.getvalue())
