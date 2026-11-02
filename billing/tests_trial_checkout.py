"""
The free-trial checkout: terms gate, SetupIntent order, trial metadata.

Stripe is mocked throughout. Nothing here touches the network.

The expensive bug this file exists to prevent is the one described in
billing/trials.py: adding trial_period_days to the charge-now path
without a SetupIntent produces a trial that looks fine at signup and
fails its first real charge 14 days later, after the customer has
forwarded their phone line. So the order of Stripe calls is asserted
explicitly, not just the end state.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.pricing_models import ServiceTier
from billing.terms_models import TermsAcceptance

User = get_user_model()

TRIAL_SLUG = 'hvac-voice-receptionist'
REVIEW_SLUG = 'hvac-review-system'


def _tier(slug=TRIAL_SLUG, name='AI Voice Receptionist'):
    tier, _ = ServiceTier.objects.update_or_create(
        slug=slug, defaults=dict(
            category='addon', name=name, price=Decimal('97.00'),
            is_recurring=True, billing_interval='month', is_active=True,
            stripe_price_id=f'price_test_{slug.replace("-", "_")}'))
    return tier


def _charge_now_tier():
    """A tier that must keep the old behaviour, as a control."""
    tier, _ = ServiceTier.objects.update_or_create(
        slug='hvac-hosting-security', defaults=dict(
            category='hosting', name='Hosting + Security',
            price=Decimal('45.00'), is_recurring=True,
            billing_interval='month', is_active=True,
            stripe_price_id='price_test_hosting'))
    return tier


def _fake_stripe(*, setup_status='succeeded'):
    """A stripe module stand-in that records the order of calls."""
    stripe = MagicMock()
    calls = []

    stripe.Customer.list.return_value = MagicMock(data=[])
    cust = MagicMock()
    cust.id = 'cus_test123'
    cust.name = ''

    def customer_create(**kwargs):
        calls.append('Customer.create')
        return cust
    stripe.Customer.create.side_effect = customer_create

    def pm_attach(pm, **kwargs):
        calls.append('PaymentMethod.attach')
        return MagicMock()
    stripe.PaymentMethod.attach.side_effect = pm_attach

    def cust_modify(cid, **kwargs):
        calls.append('Customer.modify')
        return cust
    stripe.Customer.modify.side_effect = cust_modify

    def si_create(**kwargs):
        calls.append('SetupIntent.create')
        si_create.kwargs = kwargs
        return {'status': setup_status,
                'client_secret': 'seti_test_secret',
                'id': 'seti_test'}
    stripe.SetupIntent.create.side_effect = si_create

    def sub_create(**kwargs):
        calls.append('Subscription.create')
        sub_create.kwargs = kwargs
        return {'id': 'sub_test123', 'status': 'trialing',
                'trial_end': 1794000000}
    stripe.Subscription.create.side_effect = sub_create

    stripe.Subscription.retrieve.return_value = {
        'id': 'sub_test123', 'status': 'trialing',
        'trial_end': 1794000000}
    stripe.SetupIntent.list.return_value = MagicMock(data=[
        {'status': 'succeeded', 'payment_method': 'pm_test'},
    ])
    stripe.calls = calls
    stripe.si_create = si_create
    stripe.sub_create = sub_create
    return stripe


@override_settings(TRIAL_DAYS=14, TERMS_VERSION='2026-11-01',
                   STRIPE_SECRET_KEY='sk_test_fake',
                   SECURE_SSL_REDIRECT=False)
class TrialCheckoutTests(TestCase):

    def setUp(self):
        self.tier = _tier()
        self.url = reverse('billing:checkout_confirm',
                           kwargs={'tier_slug': TRIAL_SLUG})

    def _post(self, **body):
        payload = {'email': 'buyer@example.com',
                   'payment_method_id': 'pm_test',
                   'terms_accepted': True}
        payload.update(body)
        return self.client.post(self.url, payload,
                                content_type='application/json')

    # ── The terms gate ──────────────────────────────────────────────

    def test_refuses_without_the_terms_checkbox(self):
        """The whole point: no acceptance, no subscription."""
        with patch('billing.checkout_views._stripe') as stripe_fn:
            resp = self._post(terms_accepted=False)
            self.assertEqual(resp.status_code, 400)
            self.assertIn('Terms of Service', resp.json()['error'])
            # Not "Stripe was called but we rolled back" — Stripe must
            # never have been reached at all.
            stripe_fn.assert_not_called()
        self.assertFalse(TermsAcceptance.objects.exists())

    def test_refuses_when_the_key_is_absent_entirely(self):
        """A hand-rolled POST omitting the field, not just sending false."""
        with patch('billing.checkout_views._stripe') as stripe_fn:
            resp = self.client.post(
                self.url,
                {'email': 'buyer@example.com',
                 'payment_method_id': 'pm_test'},
                content_type='application/json')
            self.assertEqual(resp.status_code, 400)
            stripe_fn.assert_not_called()

    # ── The acceptance record ───────────────────────────────────────

    def test_acceptance_row_written_with_version_and_ip(self):
        stripe = _fake_stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self._post(REMOTE_ADDR='203.0.113.9')
        self.assertEqual(resp.status_code, 200, resp.content)

        row = TermsAcceptance.objects.get()
        self.assertEqual(row.email, 'buyer@example.com')
        self.assertEqual(row.product_slug, TRIAL_SLUG)
        self.assertEqual(row.terms_version, '2026-11-01')
        self.assertEqual(row.stripe_customer_id, 'cus_test123')
        self.assertEqual(row.stripe_subscription_id, 'sub_test123')
        self.assertIsNotNone(row.accepted_at)

    def test_ip_comes_from_x_forwarded_for_behind_the_proxy(self):
        stripe = _fake_stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            self.client.post(
                self.url,
                {'email': 'buyer@example.com',
                 'payment_method_id': 'pm_test', 'terms_accepted': True},
                content_type='application/json',
                HTTP_X_FORWARDED_FOR='198.51.100.4, 10.0.0.1',
                REMOTE_ADDR='10.0.0.1')
        self.assertEqual(TermsAcceptance.objects.get().ip_address,
                         '198.51.100.4')

    def test_garbage_forwarded_header_does_not_break_the_sale(self):
        """An unparseable IP must be None, not a 500 mid-purchase."""
        stripe = _fake_stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self.client.post(
                self.url,
                {'email': 'buyer@example.com',
                 'payment_method_id': 'pm_test', 'terms_accepted': True},
                content_type='application/json',
                HTTP_X_FORWARDED_FOR='not-an-ip')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(TermsAcceptance.objects.get().ip_address)

    # ── Order of operations ─────────────────────────────────────────

    def test_setup_intent_is_confirmed_before_the_subscription_exists(self):
        """The bug this guards: a trial on an unauthenticated card."""
        stripe = _fake_stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            self._post()
        self.assertIn('SetupIntent.create', stripe.calls)
        self.assertIn('Subscription.create', stripe.calls)
        self.assertLess(stripe.calls.index('SetupIntent.create'),
                        stripe.calls.index('Subscription.create'),
                        f'wrong order: {stripe.calls}')

    def test_setup_intent_records_an_off_session_mandate(self):
        stripe = _fake_stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            self._post()
        kwargs = stripe.si_create.kwargs
        self.assertEqual(kwargs['usage'], 'off_session')
        self.assertTrue(kwargs['confirm'])
        self.assertEqual(kwargs['customer'], 'cus_test123')

    def test_no_subscription_when_the_card_setup_fails(self):
        stripe = _fake_stripe(setup_status='requires_payment_method')
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self._post()
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn('Subscription.create', stripe.calls)

    def test_sca_returns_a_setup_intent_challenge_and_no_subscription(self):
        stripe = _fake_stripe(setup_status='requires_action')
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['requires_action'])
        self.assertTrue(data['setup_intent'])
        self.assertIn('finalize', data['finalize_url'])
        self.assertTrue(data['acceptance_ref'])
        # Card not proven yet, so nothing may have been created.
        self.assertNotIn('Subscription.create', stripe.calls)
        self.assertEqual(
            TermsAcceptance.objects.get().stripe_subscription_id, '')

    # ── What Stripe is actually asked to create ─────────────────────

    def test_subscription_carries_the_trial_and_the_metadata(self):
        stripe = _fake_stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            self._post()
        kwargs = stripe.sub_create.kwargs
        self.assertEqual(kwargs['trial_period_days'], 14)
        self.assertEqual(kwargs['items'],
                         [{'price': self.tier.stripe_price_id}])
        self.assertEqual(kwargs['default_payment_method'], 'pm_test')
        self.assertEqual(
            kwargs['trial_settings']['end_behavior']
            ['missing_payment_method'], 'cancel')
        # No charge-now machinery on a $0 first invoice.
        self.assertNotIn('payment_behavior', kwargs)

        meta = kwargs['metadata']
        row = TermsAcceptance.objects.get()
        self.assertEqual(meta['product_slug'], TRIAL_SLUG)
        self.assertEqual(meta['terms_version'], '2026-11-01')
        self.assertEqual(meta['site_lead_id'], str(row.pk))
        self.assertEqual(meta['trial_signup'], '1')
        self.assertEqual(meta['tier_slug'], TRIAL_SLUG)
        self.assertTrue(meta['terms_accepted_at'])
        # Stripe rejects non-string metadata values outright.
        for key, value in meta.items():
            self.assertIsInstance(value, str, f'{key} is {type(value)}')

    def test_trial_days_setting_is_honoured(self):
        stripe = _fake_stripe()
        with override_settings(TRIAL_DAYS=30):
            with patch('billing.checkout_views._stripe', return_value=stripe):
                self._post()
        self.assertEqual(stripe.sub_create.kwargs['trial_period_days'], 30)

    def test_nonsense_trial_days_does_not_charge_immediately(self):
        """0 days would charge a buyer the page promised not to charge."""
        stripe = _fake_stripe()
        with override_settings(TRIAL_DAYS=0):
            with patch('billing.checkout_views._stripe', return_value=stripe):
                self._post()
        self.assertGreaterEqual(
            stripe.sub_create.kwargs['trial_period_days'], 1)

    # ── Both products, one flow ─────────────────────────────────────

    def test_review_automation_runs_the_same_trial_flow(self):
        _tier(slug=REVIEW_SLUG, name='Review System')
        stripe = _fake_stripe()
        url = reverse('billing:checkout_confirm',
                      kwargs={'tier_slug': REVIEW_SLUG})
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self.client.post(
                url, {'email': 'r@example.com',
                      'payment_method_id': 'pm_test',
                      'terms_accepted': True},
                content_type='application/json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(stripe.sub_create.kwargs['trial_period_days'], 14)
        self.assertEqual(
            TermsAcceptance.objects.get().product_slug, REVIEW_SLUG)

    # ── The control: nothing else changed ───────────────────────────

    def test_charge_now_tier_still_charges_and_needs_no_checkbox(self):
        """Hosting, maintenance and social must be untouched by all this."""
        _charge_now_tier()
        stripe = _fake_stripe()
        # The charge-now path reads subscription.id as an ATTRIBUTE
        # (real StripeObjects support both), so the stand-in has to be
        # an object here rather than the dict the trial path reads by
        # subscripting.
        stripe.Subscription.create.side_effect = None
        charge_now_sub = MagicMock()
        charge_now_sub.id = 'sub_hosting'
        charge_now_sub.__contains__ = lambda self, key: key in (
            'latest_invoice', 'status')
        charge_now_sub.__getitem__ = lambda self, key: {
            'latest_invoice': None, 'status': 'active'}[key]
        stripe.Subscription.create.return_value = charge_now_sub
        url = reverse('billing:checkout_confirm',
                      kwargs={'tier_slug': 'hvac-hosting-security'})
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self.client.post(
                url, {'email': 'h@example.com',
                      'payment_method_id': 'pm_test'},
                content_type='application/json')
        # No terms_accepted sent, and it still goes through.
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(TermsAcceptance.objects.exists())
        self.assertNotIn('SetupIntent.create', stripe.calls)
        self.assertNotIn('trial_period_days',
                         stripe.Subscription.create.call_args.kwargs)


@override_settings(TRIAL_DAYS=14, TERMS_VERSION='2026-11-01',
                   STRIPE_SECRET_KEY='sk_test_fake',
                   SECURE_SSL_REDIRECT=False)
class TrialCheckoutPageTests(TestCase):
    """The pre-purchase disclosure, which is the compliance-critical half."""

    def setUp(self):
        self.tier = _tier()

    def test_page_discloses_amount_date_and_how_to_cancel(self):
        resp = self.client.get(reverse('billing:checkout_page',
                                       kwargs={'tier_slug': TRIAL_SLUG}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('14 days free', html)
        self.assertIn('will not be charged until', html)
        self.assertIn('$97/mo', html)
        self.assertIn('cancel any time', html.lower())
        self.assertIn('Start 14-Day Free Trial', html)
        # The checkbox, and the button it gates.
        self.assertIn('id="terms-accept"', html)
        self.assertIn('Terms of Service', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('disabled', html)
        # Today is $0.00, not $97.
        self.assertIn('$0.00', html)

    def test_the_date_shown_matches_trial_days(self):
        """The disclosed date must be derived from the same setting
        Stripe is handed, not typed into the template."""
        from datetime import timedelta

        from django.utils import timezone
        with override_settings(TRIAL_DAYS=21):
            resp = self.client.get(reverse('billing:checkout_page',
                                           kwargs={'tier_slug': TRIAL_SLUG}))
        html = resp.content.decode()
        # localtime, matching trials.trial_end_date(). A UTC-derived
        # date would be a day ahead every evening after 7pm Eastern,
        # and the buyer would see one charge date before purchase and
        # another immediately after.
        expected = timezone.localtime(
            timezone.now() + timedelta(days=21)).date()
        # Matches the template's `date:"F j, Y"` format exactly.
        wanted = f'{expected:%B} {expected.day}, {expected.year}'
        self.assertIn(wanted, html)
        self.assertIn('21 days free', html)

    def test_charge_now_page_has_no_trial_copy_or_checkbox(self):
        _charge_now_tier()
        resp = self.client.get(reverse(
            'billing:checkout_page',
            kwargs={'tier_slug': 'hvac-hosting-security'}))
        html = resp.content.decode()
        self.assertNotIn('id="terms-accept"', html)
        self.assertNotIn('days free', html)
        self.assertIn('Pay', html)


@override_settings(TRIAL_DAYS=14, STRIPE_SECRET_KEY='sk_test_fake',
                   SECURE_SSL_REDIRECT=False)
class TrialStartedPageTests(TestCase):

    def setUp(self):
        from django.utils import timezone
        self.tier = _tier()
        self.row = TermsAcceptance.objects.create(
            email='buyer@example.com', product_slug=TRIAL_SLUG,
            terms_version='2026-11-01', accepted_at=timezone.now(),
            stripe_customer_id='cus_test123',
            stripe_subscription_id='sub_test123')
        self.url = reverse('billing:trial_started',
                           kwargs={'tier_slug': TRIAL_SLUG})

    def test_shows_product_date_price_and_cancel_link(self):
        stripe = _fake_stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self.client.get(f'{self.url}?ref={self.row.pk}')
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('AI Voice Receptionist', html)
        self.assertIn('$97/mo', html)
        self.assertIn('Nothing is charged until', html)
        # Trial end read off the Stripe subscription (1794000000 is
        # 2026-11-15), not recomputed from TRIAL_DAYS.
        self.assertIn('2026', html)
        # Our own manage/cancel page, never Stripe's hosted portal.
        self.assertIn('/billing/trial/', html)
        self.assertIn('/manage/', html)
        self.assertIn(str(self.row.pk), html)
        self.assertNotIn('stripe.com', html)
        # The reminder is ours now, so the page must not credit Stripe
        # with sending it.
        self.assertIn('We email you a reminder', html)

    def test_still_names_a_date_when_stripe_is_unreachable(self):
        """No date is the disclosure gap; a computed date is acceptable."""
        stripe = MagicMock()
        stripe.Subscription.retrieve.side_effect = Exception('stripe down')
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self.client.get(f'{self.url}?ref={self.row.pk}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Nothing is charged until',
                      resp.content.decode())

    def test_a_garbage_ref_is_not_a_500(self):
        resp = self.client.get(f'{self.url}?ref=not-a-uuid')
        self.assertEqual(resp.status_code, 200)

    def test_a_ref_from_the_other_product_is_not_honoured(self):
        _tier(slug=REVIEW_SLUG, name='Review System')
        other = reverse('billing:trial_started',
                        kwargs={'tier_slug': REVIEW_SLUG})
        resp = self.client.get(f'{other}?ref={self.row.pk}')
        self.assertEqual(resp.status_code, 200)
        # Acceptance not adopted, so no subscription-specific details.
        self.assertNotIn(str(self.row.pk), resp.content.decode())


@override_settings(STRIPE_SECRET_KEY='sk_test_fake', SECURE_SSL_REDIRECT=False)
class TrialFinalizeTests(TestCase):
    """The post-3DS path. A trial must never start on an unproven card."""

    def setUp(self):
        from django.utils import timezone
        self.tier = _tier()
        self.row = TermsAcceptance.objects.create(
            email='buyer@example.com', product_slug=TRIAL_SLUG,
            terms_version='2026-11-01', accepted_at=timezone.now(),
            stripe_customer_id='cus_test123')
        self.url = reverse('billing:checkout_finalize',
                           kwargs={'tier_slug': TRIAL_SLUG})

    def _post(self, **body):
        payload = {'acceptance_ref': str(self.row.pk),
                   'payment_method_id': 'pm_test'}
        payload.update(body)
        return self.client.post(self.url, payload,
                                content_type='application/json')

    def test_creates_the_trial_once_the_setup_intent_succeeded(self):
        stripe = _fake_stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self._post()
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn('Subscription.create', stripe.calls)
        self.row.refresh_from_db()
        self.assertEqual(self.row.stripe_subscription_id, 'sub_test123')

    def test_refuses_when_no_setup_intent_succeeded(self):
        """Guards a client that skips 3DS and POSTs here directly."""
        stripe = _fake_stripe()
        stripe.SetupIntent.list.return_value = MagicMock(data=[
            {'status': 'requires_action', 'payment_method': 'pm_test'},
        ])
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self._post()
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn('Subscription.create', stripe.calls)

    def test_refuses_a_setup_intent_for_a_different_card(self):
        stripe = _fake_stripe()
        stripe.SetupIntent.list.return_value = MagicMock(data=[
            {'status': 'succeeded', 'payment_method': 'pm_someone_else'},
        ])
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self._post()
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn('Subscription.create', stripe.calls)

    def test_unknown_ref_is_404_not_a_new_subscription(self):
        import uuid
        stripe = _fake_stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self._post(acceptance_ref=str(uuid.uuid4()))
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn('Subscription.create', stripe.calls)

    def test_double_submit_does_not_start_a_second_trial(self):
        self.row.stripe_subscription_id = 'sub_already'
        self.row.save()
        stripe = _fake_stripe()
        with patch('billing.checkout_views._stripe', return_value=stripe):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        self.assertNotIn('Subscription.create', stripe.calls)
