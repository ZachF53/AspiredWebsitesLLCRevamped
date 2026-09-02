"""
Regression tests for the payment-failure dunning path.

All three cover the same production incident (2026-09-02): Burgland
Technology paid for a $149.50/mo maintenance plan on 2026-08-26, their
card declined on the first attempt and succeeded on the retry a minute
later — and a week after paying they received a burst of "we were unable
to process your recent payment" emails.

Three independent defects lined up to produce that:

  1. The dunning cascade started at all, on a `subscription_create`
     invoice that was paid moments later.
  2. `send_payment_failed_email_task` never re-read the reinstatement
     guard, so the queued Day-7 email sent to a paid-up account.
  3. Redis redelivered the parked ETA message ~42 times, so every one of
     those copies sent.

Nothing here talks to Stripe or the broker.
"""

import json
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing.tests import _new_client


@override_settings(
    STRIPE_WEBHOOK_SECRET='',
    DEBUG=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class DunningEmailGuardTests(TestCase):
    """Defect 2 — the follow-up email must re-read the guard on fire."""

    def setUp(self):
        cache.clear()

    def _account(self, firm):
        return _new_client(firm=firm).migrated_account

    def test_noop_when_client_has_paid(self):
        """Guard is None (reinstated) → no email, however it was queued."""
        from billing.tasks import send_payment_failed_email_task

        account = self._account('Paid Up LLC')
        account.payment_failure_started_at = None
        account.save(update_fields=[
            'payment_failure_started_at', 'updated_at'])

        mail.outbox = []
        send_payment_failed_email_task(str(account.id), 7)
        self.assertEqual(len(mail.outbox), 0)

    def test_sends_when_still_delinquent(self):
        """Guard still set → the client really is behind, so send."""
        from billing.tasks import send_payment_failed_email_task

        account = self._account('Still Behind LLC')
        account.payment_failure_started_at = timezone.now()
        account.save(update_fields=[
            'payment_failure_started_at', 'updated_at'])

        mail.outbox = []
        send_payment_failed_email_task(str(account.id), 7)
        self.assertEqual(len(mail.outbox), 1)

    def test_duplicate_delivery_sends_once(self):
        """Defect 3 — N redeliveries of the same message send one email.

        The broker handed the worker ~42 copies of the Day-7 task. Each
        copy saw the same delinquent account, so the guard alone could
        not stop them.
        """
        from billing.tasks import send_payment_failed_email_task

        account = self._account('Redelivered LLC')
        account.payment_failure_started_at = timezone.now()
        account.save(update_fields=[
            'payment_failure_started_at', 'updated_at'])

        mail.outbox = []
        for _ in range(42):
            send_payment_failed_email_task(str(account.id), 7)
        self.assertEqual(len(mail.outbox), 1)

    def test_day_7_and_day_14_are_claimed_separately(self):
        """The send-once marker is per stage — Day 14 still gets through."""
        from billing.tasks import send_payment_failed_email_task

        account = self._account('Two Stage LLC')
        account.payment_failure_started_at = timezone.now()
        account.save(update_fields=[
            'payment_failure_started_at', 'updated_at'])

        mail.outbox = []
        send_payment_failed_email_task(str(account.id), 7)
        send_payment_failed_email_task(str(account.id), 14)
        self.assertEqual(len(mail.outbox), 2)

    def test_sends_when_cache_is_down(self):
        """The marker fails open — a cache outage must not eat dunning."""
        from billing.tasks import send_payment_failed_email_task

        account = self._account('Cache Down LLC')
        account.payment_failure_started_at = timezone.now()
        account.save(update_fields=[
            'payment_failure_started_at', 'updated_at'])

        mail.outbox = []
        with patch('django.core.cache.cache.add',
                   side_effect=RuntimeError('redis is gone')):
            send_payment_failed_email_task(str(account.id), 7)
        self.assertEqual(len(mail.outbox), 1)


@override_settings(
    STRIPE_WEBHOOK_SECRET='',
    DEBUG=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class FirstInvoiceDeclineTests(TestCase):
    """Defect 1 — a checkout-time decline is not a delinquency."""

    def _post(self, body):
        return self.client.post(
            reverse('billing:stripe_webhook'),
            data=json.dumps(body), content_type='application/json')

    def _body(self, customer, billing_reason):
        return {
            'type': 'invoice.payment_failed',
            'data': {'object': {
                'id': 'in_dunning_test',
                'customer': customer,
                'subscription': '',
                'billing_reason': billing_reason,
            }},
        }

    @patch('billing.tasks.set_site_maintenance_mode_task.apply_async')
    @patch('billing.tasks.set_site_offline_task.apply_async')
    @patch('billing.tasks.destroy_client_droplet_task.apply_async')
    @patch('billing.tasks.delete_client_snapshot_task.apply_async')
    @patch('billing.tasks.send_payment_failed_email_task.apply_async')
    def test_subscription_create_does_not_start_dunning(
            self, mock_email, mock_delete, mock_destroy, mock_offline,
            mock_maint):
        account = _new_client(firm='Checkout Retry LLC').migrated_account
        account.stripe_customer_id = 'cus_checkout_retry'
        account.save()

        mail.outbox = []
        r = self._post(self._body('cus_checkout_retry', 'subscription_create'))
        self.assertEqual(r.status_code, 200)

        account.refresh_from_db()
        # No window opened, so nothing can escalate later.
        self.assertIsNone(account.payment_failure_started_at)
        # No offence recorded — this is what would have triggered the $75
        # second-offence fee on the client's next genuine failure.
        self.assertEqual(account.payment_failure_offenses or 0, 0)
        self.assertEqual(len(mail.outbox), 0)
        for m in (mock_email, mock_delete, mock_destroy, mock_offline,
                  mock_maint):
            m.assert_not_called()

    @patch('billing.tasks.set_site_maintenance_mode_task.apply_async')
    @patch('billing.tasks.set_site_offline_task.apply_async')
    @patch('billing.tasks.destroy_client_droplet_task.apply_async')
    @patch('billing.tasks.delete_client_snapshot_task.apply_async')
    @patch('billing.tasks.send_payment_failed_email_task.apply_async')
    def test_subscription_cycle_still_starts_dunning(
            self, mock_email, mock_delete, mock_destroy, mock_offline,
            mock_maint):
        """A renewal failure is the real thing — chain must still arm."""
        account = _new_client(firm='Real Delinquent LLC').migrated_account
        account.stripe_customer_id = 'cus_real_delinquent'
        account.save()

        r = self._post(
            self._body('cus_real_delinquent', 'subscription_cycle'))
        self.assertEqual(r.status_code, 200)

        account.refresh_from_db()
        self.assertIsNotNone(account.payment_failure_started_at)
        mock_maint.assert_called_once()
        mock_offline.assert_called_once()
        mock_destroy.assert_called_once()
        mock_delete.assert_called_once()


class BrokerVisibilityTimeoutTests(TestCase):
    """Defect 3 at the source — the broker must not redeliver parked ETAs.

    `visibility_timeout` has to outlast the longest countdown the dunning
    chain schedules, or Redis decides the worker died and hands the same
    message out again. Kombu's default of one hour against a 60-day
    countdown is what produced 42 copies of every task.
    """

    def test_visibility_timeout_outlasts_longest_countdown(self):
        from django.conf import settings

        longest_countdown = 60 * 24 * 60 * 60  # the Day-60 snapshot delete
        opts = getattr(settings, 'CELERY_BROKER_TRANSPORT_OPTIONS', {})
        self.assertGreater(opts.get('visibility_timeout', 0),
                           longest_countdown)
