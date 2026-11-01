"""
The cancellation path: Stripe Customer Portal, and the custom cancel fix.

Cancellation is the compliance-critical half of a free trial that
converts. A trial has to be at least as easy to get out of as it was to
get into, which is why there are two ways in here and why the broken
custom cancel view mattered enough to fix alongside.

Stripe is mocked throughout.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing.pricing_models import ServiceTier
from billing.terms_models import TermsAcceptance

User = get_user_model()

TRIAL_SLUG = 'hvac-voice-receptionist'


def _tier():
    tier, _ = ServiceTier.objects.update_or_create(
        slug=TRIAL_SLUG, defaults=dict(
            category='addon', name='AI Voice Receptionist',
            price=Decimal('97.00'), is_recurring=True,
            billing_interval='month', is_active=True,
            stripe_price_id='price_test_voice'))
    return tier


def _acceptance(*, accepted_at=None, customer='cus_test123'):
    return TermsAcceptance.objects.create(
        email='buyer@example.com', product_slug=TRIAL_SLUG,
        terms_version='2026-11-01',
        accepted_at=accepted_at or timezone.now(),
        stripe_customer_id=customer,
        stripe_subscription_id='sub_test123')


@override_settings(TRIAL_DAYS=14, STRIPE_SECRET_KEY='sk_test_fake',
                   SECURE_SSL_REDIRECT=False)
class StripePortalRefTests(TestCase):
    """The no-password cancel link, and the window it lives in."""

    def setUp(self):
        _tier()
        self.url = reverse('billing:stripe_portal')

    def _stripe(self):
        stripe = MagicMock()
        stripe.billing_portal.Session.create.return_value = {
            'url': 'https://billing.stripe.com/session/test123'}
        return stripe

    def test_fresh_ref_redirects_into_the_stripe_portal(self):
        row = _acceptance()
        stripe = self._stripe()
        with patch('billing.portal_views._stripe', return_value=stripe):
            resp = self.client.get(f'{self.url}?ref={row.pk}')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'],
                         'https://billing.stripe.com/session/test123')
        stripe.billing_portal.Session.create.assert_called_once()
        self.assertEqual(
            stripe.billing_portal.Session.create.call_args.kwargs['customer'],
            'cus_test123')

    def test_ref_works_through_the_whole_trial(self):
        """Day 13 is exactly when someone cancels to avoid the charge."""
        row = _acceptance(
            accepted_at=timezone.now() - timedelta(days=13, hours=23))
        stripe = self._stripe()
        with patch('billing.portal_views._stripe', return_value=stripe):
            resp = self.client.get(f'{self.url}?ref={row.pk}')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('billing.stripe.com', resp['Location'])

    def test_ref_is_gone_after_the_trial_window(self):
        """Not a permanent bearer token."""
        row = _acceptance(
            accepted_at=timezone.now() - timedelta(days=14, minutes=1))
        stripe = self._stripe()
        with patch('billing.portal_views._stripe', return_value=stripe):
            resp = self.client.get(f'{self.url}?ref={row.pk}')
        self.assertEqual(resp.status_code, 410)
        body = resp.content.decode()
        self.assertIn('expired', body)
        self.assertIn('sign in to manage billing', body.lower())
        # The whole point: no session is minted for an expired ref.
        stripe.billing_portal.Session.create.assert_not_called()

    def test_long_expired_ref_is_still_only_a_410(self):
        row = _acceptance(accepted_at=timezone.now() - timedelta(days=400))
        stripe = self._stripe()
        with patch('billing.portal_views._stripe', return_value=stripe):
            resp = self.client.get(f'{self.url}?ref={row.pk}')
        self.assertEqual(resp.status_code, 410)
        stripe.billing_portal.Session.create.assert_not_called()

    def test_the_window_follows_trial_days(self):
        """A longer trial must not leave a dead cancel link mid-trial."""
        row = _acceptance(accepted_at=timezone.now() - timedelta(days=20))
        stripe = self._stripe()
        with override_settings(TRIAL_DAYS=30):
            with patch('billing.portal_views._stripe', return_value=stripe):
                resp = self.client.get(f'{self.url}?ref={row.pk}')
        self.assertEqual(resp.status_code, 302)

    def test_unknown_ref_mints_nothing(self):
        import uuid
        stripe = self._stripe()
        with patch('billing.portal_views._stripe', return_value=stripe):
            resp = self.client.get(f'{self.url}?ref={uuid.uuid4()}')
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('billing.stripe.com', resp['Location'])
        stripe.billing_portal.Session.create.assert_not_called()

    def test_garbage_ref_is_not_a_500(self):
        stripe = self._stripe()
        with patch('billing.portal_views._stripe', return_value=stripe):
            resp = self.client.get(f'{self.url}?ref=not-a-uuid')
        self.assertIn(resp.status_code, (302, 410))
        stripe.billing_portal.Session.create.assert_not_called()

    def test_ref_never_reaches_the_logs(self):
        """The ref is a credential for the length of the trial."""
        row = _acceptance()
        stripe = self._stripe()
        with patch('billing.portal_views._stripe', return_value=stripe):
            with self.assertLogs('billing.portal_views', level='DEBUG') as cm:
                # Something has to be logged for assertLogs to pass, so
                # force the no-customer branch alongside.
                logging_probe = _acceptance(customer='')
                self.client.get(f'{self.url}?ref={logging_probe.pk}')
                self.client.get(f'{self.url}?ref={row.pk}')
        joined = '\n'.join(cm.output)
        self.assertNotIn(str(row.pk), joined)
        self.assertNotIn(str(logging_probe.pk), joined)

    def test_stripe_failure_on_a_ref_does_not_dead_end(self):
        """A ref holder has no password, so a login bounce is a dead end."""
        row = _acceptance()
        stripe = MagicMock()
        stripe.billing_portal.Session.create.side_effect = Exception(
            'portal not configured')
        with patch('billing.portal_views._stripe', return_value=stripe):
            resp = self.client.get(f'{self.url}?ref={row.pk}')
        self.assertEqual(resp.status_code, 503)
        body = resp.content.decode()
        self.assertIn('210) 201-0375', body)
        self.assertIn('Nothing has been charged', body)

    def test_no_ref_and_anonymous_is_sent_to_sign_in(self):
        stripe = self._stripe()
        with patch('billing.portal_views._stripe', return_value=stripe):
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        stripe.billing_portal.Session.create.assert_not_called()


@override_settings(STRIPE_SECRET_KEY='sk_test_fake', SECURE_SSL_REDIRECT=False)
class CustomPortalCancelTests(TestCase):
    """The cancel view returned 500 on every SUCCESSFUL cancellation.

    Two stacked bugs: `.get()` on a stripe 15 StripeObject (no dict
    methods), and `current_period_end` having moved onto the
    subscription item in Stripe's 2025 API. The cancel applied, then the
    view raised while building the confirmation message, so the customer
    saw an error page and reasonably assumed it had not worked.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username='c@example.com', email='c@example.com',
            password='pw-123456')
        self.client.force_login(self.user)
        self.url = reverse('billing:subscription_cancel',
                           kwargs={'sub_id': 'sub_test123'})

    def _cancel(self, stripe, **post):
        """POST the cancel and return (response, messages).

        Deliberately does NOT follow the redirect. The redirect target
        is the portal subscriptions page, which needs an Account and a
        portal PIN and 403s without them — following it would make
        these tests fail for reasons that have nothing to do with
        cancelling. The messages are read off the request instead.
        """
        from django.contrib.messages import get_messages
        with patch('billing.portal_views._stripe', return_value=stripe):
            resp = self.client.post(self.url, post)
        return resp, [str(m) for m in get_messages(resp.wsgi_request)]

    def _sub(self, payload):
        """A StripeObject stand-in: `in` and [] work, `.get()` does not."""
        class FakeStripeObject(dict):
            def get(self, *a, **kw):
                raise AttributeError(
                    "'Subscription' object has no attribute 'get'")
        return FakeStripeObject(payload)

    def test_cancel_returns_200_with_period_end_on_the_item(self):
        """The current API shape."""
        sub = self._sub({
            'id': 'sub_test123',
            'items': {'data': [{'current_period_end': 1794000000}]},
        })
        stripe = MagicMock()
        stripe.Subscription.modify.return_value = sub
        resp, messages = self._cancel(stripe, reason='too expensive')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(any('Cancelled' in m for m in messages), messages)
        self.assertTrue(any('2026' in m for m in messages), messages)

    def test_cancel_returns_200_on_the_legacy_shape(self):
        """Pre-2025 subscriptions still carry it at the top level."""
        sub = self._sub({'id': 'sub_test123',
                         'current_period_end': 1794000000})
        stripe = MagicMock()
        stripe.Subscription.modify.return_value = sub
        resp, messages = self._cancel(stripe)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(any('Cancelled' in m for m in messages), messages)

    def test_cancelling_during_a_trial_reports_the_trial_end(self):
        """A trialing subscription has no paid period yet."""
        sub = self._sub({'id': 'sub_test123', 'status': 'trialing',
                         'items': {'data': [{}]},
                         'trial_end': 1794000000})
        stripe = MagicMock()
        stripe.Subscription.modify.return_value = sub
        resp, messages = self._cancel(stripe)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(any('2026' in m for m in messages), messages)

    def test_cancel_still_succeeds_when_no_date_can_be_read(self):
        """Confirmation copy must never fail after Stripe has cancelled."""
        sub = self._sub({'id': 'sub_test123', 'items': {'data': []}})
        stripe = MagicMock()
        stripe.Subscription.modify.return_value = sub
        resp, messages = self._cancel(stripe)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            any('your next billing date' in m for m in messages), messages)

    def test_cancel_is_applied_at_stripe_before_any_rendering(self):
        sub = self._sub({'id': 'sub_test123', 'items': {'data': []}})
        stripe = MagicMock()
        stripe.Subscription.modify.return_value = sub
        self._cancel(stripe)
        stripe.Subscription.modify.assert_called_once_with(
            'sub_test123', cancel_at_period_end=True)

    def test_the_reason_is_recorded(self):
        from billing.cancellation_models import CancellationReason
        sub = self._sub({'id': 'sub_test123', 'items': {'data': []}})
        stripe = MagicMock()
        stripe.Subscription.modify.return_value = sub
        self._cancel(stripe, reason='found someone cheaper')
        row = CancellationReason.objects.get()
        self.assertEqual(row.reason, 'found someone cheaper')
        self.assertEqual(row.subscription_id, 'sub_test123')
