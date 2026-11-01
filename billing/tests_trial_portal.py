"""
The cancellation path: our own manage/cancel page, and the portal fix.

Cancellation is the compliance-critical half of a free trial that
converts. A trial has to be at least as easy to get out of as it was to
get into, which is why the page here is reachable without a password for
the length of the trial, and why the broken custom portal cancel view
mattered enough to fix alongside.

This page replaced a redirect into Stripe's hosted Customer Portal.
Every billing screen a customer sees has to be ours: our domain, our
branding, our wording. CLAUDE.md has said "custom UI, no Customer
Portal" since Phase 7. There is a test below asserting nothing on the
page points at stripe.com, because that is the regression that would
quietly undo it.

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


def _tier(slug=TRIAL_SLUG, name='AI Voice Receptionist'):
    tier, _ = ServiceTier.objects.update_or_create(
        slug=slug, defaults=dict(
            category='addon', name=name,
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
        stripe_subscription_id='sub_test123',
        trial_end=timezone.now() + timedelta(days=14))


@override_settings(TRIAL_DAYS=14, STRIPE_SECRET_KEY='sk_test_fake',
                   SECURE_SSL_REDIRECT=False)
class TrialManagePageTests(TestCase):
    """The custom manage/cancel page, and the window its link lives in."""

    def setUp(self):
        _tier()
        self.url = reverse('billing:trial_manage',
                           kwargs={'tier_slug': TRIAL_SLUG})

    def _stripe(self):
        stripe = MagicMock()
        stripe.Subscription.modify.return_value = {
            'id': 'sub_test123', 'status': 'trialing',
            'cancel_at_period_end': True}
        return stripe

    # -- The page itself ---------------------------------------------

    def test_fresh_ref_renders_our_own_page(self):
        row = _acceptance()
        resp = self.client.get(f'{self.url}?ref={row.pk}')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('AI Voice Receptionist', html)
        self.assertIn('Cancel my free trial', html)
        self.assertIn('$97/mo', html)
        # Our form, so it needs CSRF.
        self.assertIn('csrfmiddlewaretoken', html)

    def test_nothing_on_the_page_links_to_stripe(self):
        """The whole reason this page exists instead of a redirect."""
        row = _acceptance()
        html = self.client.get(f'{self.url}?ref={row.pk}').content.decode()
        self.assertNotIn('stripe.com', html)
        self.assertNotIn('billing.stripe', html)

    def test_it_states_the_amount_and_the_charge_date(self):
        row = _acceptance()
        row.trial_end = timezone.now() + timedelta(days=3)
        row.save()
        html = self.client.get(f'{self.url}?ref={row.pk}').content.decode()
        self.assertIn('$97/mo', html)
        self.assertIn(f'{row.trial_end:%B} {row.trial_end.day},', html)

    # -- The link window ---------------------------------------------

    def test_link_works_through_the_whole_trial(self):
        """Day 13 is exactly when someone cancels to avoid the charge."""
        row = _acceptance(
            accepted_at=timezone.now() - timedelta(days=13, hours=23))
        resp = self.client.get(f'{self.url}?ref={row.pk}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Cancel my free trial', resp.content.decode())

    def test_link_is_gone_after_the_trial_window(self):
        """Not a permanent bearer token."""
        row = _acceptance(
            accepted_at=timezone.now() - timedelta(days=14, minutes=1))
        resp = self.client.get(f'{self.url}?ref={row.pk}')
        self.assertEqual(resp.status_code, 410)
        body = resp.content.decode()
        self.assertIn('expired', body.lower())
        self.assertIn('Sign in to manage billing', body)

    def test_expired_link_cannot_cancel_either(self):
        """A 410 on GET would be cosmetic if POST still worked."""
        row = _acceptance(accepted_at=timezone.now() - timedelta(days=30))
        stripe = self._stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self.client.post(f'{self.url}?ref={row.pk}')
        self.assertEqual(resp.status_code, 410)
        stripe.Subscription.modify.assert_not_called()
        row.refresh_from_db()
        self.assertIsNone(row.cancelled_at)

    def test_long_expired_link_is_still_only_a_410(self):
        row = _acceptance(accepted_at=timezone.now() - timedelta(days=400))
        self.assertEqual(
            self.client.get(f'{self.url}?ref={row.pk}').status_code, 410)

    def test_the_window_follows_trial_days(self):
        """A longer trial must not leave a dead cancel link mid-trial."""
        row = _acceptance(accepted_at=timezone.now() - timedelta(days=20))
        with override_settings(TRIAL_DAYS=30):
            resp = self.client.get(f'{self.url}?ref={row.pk}')
        self.assertEqual(resp.status_code, 200)

    def test_unknown_ref_is_sent_to_the_portal(self):
        import uuid
        resp = self.client.get(f'{self.url}?ref={uuid.uuid4()}')
        self.assertEqual(resp.status_code, 302)

    def test_garbage_ref_is_not_a_500(self):
        resp = self.client.get(f'{self.url}?ref=not-a-uuid')
        self.assertIn(resp.status_code, (302, 410))

    def test_a_ref_for_the_other_product_is_not_honoured(self):
        _tier(slug='hvac-review-system', name='Review System')
        row = _acceptance()
        other = reverse('billing:trial_manage',
                        kwargs={'tier_slug': 'hvac-review-system'})
        resp = self.client.get(f'{other}?ref={row.pk}')
        self.assertEqual(resp.status_code, 302)

    def test_ref_never_reaches_the_logs(self):
        """The ref is a credential for the length of the trial."""
        row = _acceptance()
        stripe = self._stripe()
        with self.assertLogs('billing', level='INFO') as cm:
            with patch('billing.checkout_views._stripe', return_value=stripe):
                self.client.post(f'{self.url}?ref={row.pk}')
        self.assertNotIn(str(row.pk), '\n'.join(cm.output))

    # -- Cancelling --------------------------------------------------

    def test_cancel_sets_cancel_at_period_end_not_delete(self):
        """They keep the free days they have left."""
        row = _acceptance()
        stripe = self._stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self.client.post(f'{self.url}?ref={row.pk}')
        self.assertEqual(resp.status_code, 200)
        stripe.Subscription.modify.assert_called_once_with(
            'sub_test123', cancel_at_period_end=True)
        stripe.Subscription.delete.assert_not_called()

    def test_cancel_confirms_no_charge_and_names_the_end_date(self):
        row = _acceptance()
        row.trial_end = timezone.now() + timedelta(days=5)
        row.save()
        stripe = self._stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            html = self.client.post(
                f'{self.url}?ref={row.pk}').content.decode()
        self.assertIn('will not be charged', html)
        self.assertIn('keeps working until', html)
        self.assertIn(f'{row.trial_end:%B} {row.trial_end.day},', html)

    def test_cancel_stamps_cancelled_at(self):
        row = _acceptance()
        stripe = self._stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            self.client.post(f'{self.url}?ref={row.pk}')
        row.refresh_from_db()
        self.assertIsNotNone(row.cancelled_at)

    def test_cancelling_takes_the_row_out_of_the_reminder_sweep(self):
        """Nobody who cancelled on day 2 gets a day-11 charge warning."""
        from billing.trial_reminders import due_reminders

        row = _acceptance()
        row.trial_end = timezone.now() + timedelta(days=2)
        row.save()
        self.assertIn(row, list(due_reminders()))

        stripe = self._stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            self.client.post(f'{self.url}?ref={row.pk}')
        self.assertNotIn(row, list(due_reminders()))

    def test_an_already_cancelled_trial_shows_the_confirmed_state(self):
        row = _acceptance()
        row.cancelled_at = timezone.now()
        row.save()
        resp = self.client.get(f'{self.url}?ref={row.pk}')
        html = resp.content.decode()
        self.assertIn('will not be charged', html)
        self.assertNotIn('Cancel my free trial', html)

    def test_double_cancel_does_not_call_stripe_again(self):
        row = _acceptance()
        row.cancelled_at = timezone.now()
        row.save()
        stripe = self._stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            self.client.post(f'{self.url}?ref={row.pk}')
        stripe.Subscription.modify.assert_not_called()

    def test_a_stripe_failure_gives_a_phone_number_not_a_dead_end(self):
        row = _acceptance()
        stripe = MagicMock()
        stripe.Subscription.modify.side_effect = Exception('stripe down')
        with patch('billing.checkout_views._stripe', return_value=stripe):
            html = self.client.post(
                f'{self.url}?ref={row.pk}').content.decode()
        self.assertIn('210) 201-0375', html)
        row.refresh_from_db()
        self.assertIsNone(row.cancelled_at)
        # Still offering the button, since it did not work.
        self.assertIn('Cancel my free trial', html)

    def test_cancel_with_no_subscription_id_is_handled(self):
        row = _acceptance()
        row.stripe_subscription_id = ''
        row.save()
        stripe = self._stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            html = self.client.post(
                f'{self.url}?ref={row.pk}').content.decode()
        self.assertIn('could not find an active subscription', html)
        stripe.Subscription.modify.assert_not_called()


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
