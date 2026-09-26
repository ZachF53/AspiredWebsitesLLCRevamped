"""
Sept 2026 terms: no deposit; build paid in full or in 24 installments,
plans billed from signing, 30-day guarantee with 25% retention.

Stripe is mocked throughout — nothing here talks to the network.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing.pricing_models import ServiceTier
from clients.account_models import Account, Website
from clients.models import Contract, ContractService, PaymentRecord
from clients.services import GuardError

User = get_user_model()

PRICE_IDS = {
    'hvac-build-full': 'price_full',
    'hvac-build-installment': 'price_inst',
    'hvac-full-plan': 'price_fullplan',
    'hvac-plan-paid-in-full': 'price_pif',
    'hvac-hosting-security': 'price_host',
}


def _seed():
    call_command('seed_pricing', stdout=MagicMock())
    for slug, pid in PRICE_IDS.items():
        ServiceTier.objects.filter(slug=slug).update(stripe_price_id=pid)


def _account(email='owner@example.com', name='Cool Air HVAC'):
    user = User.objects.create_user(
        username=email, email=email, password='pw-123456')
    account = (Account.objects.filter(user=user).first()
               or Account.objects.create(user=user, name=name))
    account.name = name
    account.contact_name = 'Casey Cool'
    account.save()
    account.websites.all().delete()
    site = Website.objects.create(account=account, name=name,
                                  slug=email.split('@')[0])
    return account, site


def _contract(account, site, build_option, plan_option, *, signed=True,
              signed_at=None):
    from admin_dashboard.views import _create_new_terms_contract
    contract = _create_new_terms_contract(
        account=account, website=site, build_option=build_option,
        plan_option=plan_option)
    if signed:
        contract.signed = True
        contract.signed_at = signed_at or timezone.now()
        contract.save()
    return contract


# ── Discontinued tiers ─────────────────────────────────────────────────────

class DiscontinuedTierTests(TestCase):

    def test_seed_keeps_discontinued_tiers_inactive_and_hidden(self):
        _seed()
        for slug in ('website-essential', 'website-premium',
                     'maintenance-essentials', 'maintenance-growth',
                     'maintenance-dominant', 'social-basic',
                     'social-standard', 'social-full', 'hosting-annual',
                     'domain-law'):
            tier = ServiceTier.objects.get(slug=slug)
            self.assertFalse(tier.is_active, slug)
            self.assertFalse(tier.is_public, slug)
        self.assertTrue(ServiceTier.objects.get(
            slug='hvac-hosting-security').is_active)

    def test_migration_function_is_idempotent(self):
        import importlib
        from django.apps import apps
        mig = importlib.import_module(
            'billing.migrations.0011_deactivate_discontinued_tiers')
        ServiceTier.objects.create(
            slug='website-premium', category='website_build',
            name='Premium', price=Decimal('4500'))
        mig.deactivate(apps, None)
        mig.deactivate(apps, None)
        tier = ServiceTier.objects.get(slug='website-premium')
        self.assertFalse(tier.is_active)
        self.assertFalse(tier.is_public)

    def test_checkout_for_inactive_tier_redirects(self):
        _seed()
        r = self.client.get('/billing/checkout/maintenance-growth/')
        self.assertEqual(r.status_code, 302)
        r = self.client.get('/billing/checkout/does-not-exist/')
        self.assertEqual(r.status_code, 404)

    def test_hosting_security_is_self_checkout(self):
        _seed()
        r = self.client.get('/billing/checkout/hvac-hosting-security/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Hosting + Security')

    def test_law_domain_tier_gives_clean_message(self):
        from billing.stripe_helpers import get_domain_tier
        _seed()
        with self.assertRaisesMessage(ValueError, 'not available'):
            get_domain_tier('law')


# ── Contract text ──────────────────────────────────────────────────────────

class ContractTextTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        _seed()
        cls.account, cls.site = _account()

    def test_pay_in_full_text(self):
        c = _contract(self.account, self.site, 'pay_in_full', 'none',
                      signed=False)
        text = c.contract_text
        self.assertNotIn('50%', text)
        self.assertIn('$2,000, paid in full at', text)
        self.assertIn('until the build has been paid in full', text)
        self.assertIn('75% of all amounts', text)
        self.assertIn('restart fee of 25%', text)
        self.assertIn('three (3)', text)
        self.assertIn('State of Georgia', text)
        self.assertIn('two (2) rounds of revisions', text)
        self.assertNotIn('ocial media', text)
        self.assertEqual(c.payment_option, 'pay_in_full')
        self.assertIsNone(c.deposit_amount)
        self.assertEqual(c.build_price, Decimal('2000.00'))

    def test_installment_text(self):
        c = _contract(self.account, self.site, 'installment', 'none',
                      signed=False)
        text = c.contract_text
        self.assertNotIn('50%', text)
        self.assertIn('24 monthly payments of $105 ($2,520 total)', text)
        self.assertIn('first', text)
        self.assertIn('fourteen (14)', text)
        self.assertIn('24th and final installment', text)
        self.assertEqual(c.payment_option, 'installment')
        self.assertEqual(c.build_price, Decimal('2520.00'))
        build = c.services.get(service_type='build')
        self.assertEqual(build.price, Decimal('105.00'))
        self.assertIsNone(build.deposit_amount)

    def test_installment_plus_full_plan_is_one_payment(self):
        c = _contract(self.account, self.site, 'installment', 'full_plan',
                      signed=False)
        text = c.contract_text
        self.assertIn('$250 per month', text)
        self.assertIn('$145 per month', text)
        self.assertIn('not charged separately', text)
        plan = c.services.get(service_type='maintenance')
        self.assertEqual(plan.tier_slug, 'hvac-full-plan')

    def test_pay_in_full_plus_plan_uses_paid_in_full_tier(self):
        c = _contract(self.account, self.site, 'pay_in_full', 'full_plan',
                      signed=False)
        self.assertEqual(c.services.get(service_type='maintenance').tier_slug,
                         'hvac-plan-paid-in-full')
        self.assertIn('month-to-month', c.contract_text)

    def test_hosting_only(self):
        c = _contract(self.account, self.site, 'none', 'hosting',
                      signed=False)
        self.assertEqual(c.payment_option, 'none')
        self.assertFalse(c.includes_build)
        self.assertIn('$45 per month', c.contract_text)
        self.assertNotIn('Ownership', c.contract_text)

    def test_empty_contract_refused(self):
        from clients.contract_options import ContractOptionError
        with self.assertRaises(ContractOptionError):
            _contract(self.account, self.site, 'none', 'none')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class SendContractViewTests(TestCase):

    def setUp(self):
        _seed()
        staff = User.objects.create_user(
            username='st', email='st@example.com', password='pw-123456',
            is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        self.account, self.site = _account()

    def test_installment_tier_no_longer_a_one_time_105_contract(self):
        r = self.client.post(
            f'/admin-dashboard/websites/{self.site.id}/send-contract/',
            {'build_option': 'installment', 'plan_option': 'none'})
        self.assertEqual(r.status_code, 302)
        c = Contract.objects.get(website_new=self.site)
        self.assertEqual(c.payment_option, 'installment')
        self.assertEqual(c.build_price, Decimal('2520.00'))
        self.assertIsNone(c.deposit_amount)
        self.assertNotIn('50%', c.contract_text)

    def test_unknown_option_refused(self):
        self.client.post(
            f'/admin-dashboard/websites/{self.site.id}/send-contract/',
            {'build_option': 'deposit', 'plan_option': 'none'})
        self.assertFalse(Contract.objects.filter(
            website_new=self.site).exists())


# ── Signing checkout (Stripe mocked) ───────────────────────────────────────

def _fake_stripe():
    stripe = MagicMock()
    stripe.SubscriptionSchedule.create.return_value = {
        'id': 'sub_sched_1', 'subscription': 'sub_inst_1'}
    stripe.Subscription.create.side_effect = lambda **kw: MagicMock(
        id=f'sub_{kw["items"][0]["price"]}')
    stripe.Subscription.retrieve.return_value = {'status': 'active'}
    pi = MagicMock(id='pi_build_1')
    stripe.PaymentIntent.create.return_value = pi
    stripe.PaymentIntent.retrieve.return_value = {'status': 'succeeded'}
    return stripe


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class ContractCheckoutTests(TestCase):

    def setUp(self):
        _seed()
        self.account, self.site = _account()
        self.stripe = _fake_stripe()
        self.patches = [
            patch('billing.contract_billing._stripe',
                  return_value=self.stripe),
            patch('billing.stripe_helpers.create_or_get_customer',
                  return_value=MagicMock(id='cus_1')),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_installment_plus_full_plan_creates_one_two_phase_schedule(self):
        from billing.contract_billing import run_contract_checkout
        c = _contract(self.account, self.site, 'installment', 'full_plan')
        result = run_contract_checkout(c, payment_method_id='pm_1')
        self.assertEqual(result, {'ok': True})

        self.stripe.SubscriptionSchedule.create.assert_called_once()
        kwargs = self.stripe.SubscriptionSchedule.create.call_args.kwargs
        phases = kwargs['phases']
        self.assertEqual(len(phases), 2)
        self.assertEqual(phases[0]['items'][0]['price'], 'price_fullplan')
        self.assertEqual(phases[0]['duration'],
                         {'interval': 'month', 'interval_count': 24})
        self.assertEqual(phases[1]['items'][0]['price'], 'price_pif')
        self.assertNotIn('duration', phases[1])
        self.assertEqual(kwargs['end_behavior'], 'release')
        self.assertNotIn('tier_slug', kwargs['metadata'])
        # The Full Plan is NOT a second subscription.
        self.stripe.Subscription.create.assert_not_called()
        self.stripe.PaymentIntent.create.assert_not_called()

        self.site.refresh_from_db()
        self.assertEqual(self.site.stripe_build_installment_subscription_id,
                         'sub_inst_1')
        self.assertEqual(self.site.stripe_build_installment_schedule_id,
                         'sub_sched_1')
        self.assertEqual(self.site.stripe_maintenance_subscription_id,
                         'sub_inst_1')
        self.assertEqual(self.site.payment_status, 'installments_active')
        c.refresh_from_db()
        self.assertIsNotNone(c.paid_at_signing_at)
        from clients.service_models import MaintenancePlan
        plan = MaintenancePlan.objects.get(stripe_subscription_id='sub_inst_1')
        self.assertEqual(plan.tier_slug, 'hvac-full-plan')
        self.assertEqual(plan.status, 'active')

    def test_installment_only_cancels_after_24(self):
        from billing.contract_billing import run_contract_checkout
        c = _contract(self.account, self.site, 'installment', 'none')
        run_contract_checkout(c, payment_method_id='pm_1')
        kwargs = self.stripe.SubscriptionSchedule.create.call_args.kwargs
        self.assertEqual(kwargs['end_behavior'], 'cancel')
        self.assertEqual(len(kwargs['phases']), 1)
        self.assertEqual(kwargs['phases'][0]['items'][0]['price'],
                         'price_inst')

    def test_installment_plus_hosting_is_schedule_plus_subscription(self):
        from billing.contract_billing import run_contract_checkout
        c = _contract(self.account, self.site, 'installment', 'hosting')
        run_contract_checkout(c, payment_method_id='pm_1')
        self.stripe.SubscriptionSchedule.create.assert_called_once()
        self.stripe.Subscription.create.assert_called_once()
        self.assertEqual(
            self.stripe.Subscription.create.call_args.kwargs['items'],
            [{'price': 'price_host'}])
        self.site.refresh_from_db()
        self.assertEqual(self.site.stripe_hosting_subscription_id,
                         'sub_price_host')

    def test_pay_in_full_plus_plan(self):
        from billing.contract_billing import run_contract_checkout
        c = _contract(self.account, self.site, 'pay_in_full', 'full_plan')
        with patch('billing.webhooks._handle_payment_intent_succeeded'):
            result = run_contract_checkout(c, payment_method_id='pm_1')
        self.assertEqual(result, {'ok': True})
        pi_kwargs = self.stripe.PaymentIntent.create.call_args.kwargs
        self.assertEqual(pi_kwargs['amount'], 200000)
        self.assertEqual(pi_kwargs['metadata']['kind'], 'onboarding')
        self.stripe.SubscriptionSchedule.create.assert_not_called()
        self.assertEqual(
            self.stripe.Subscription.create.call_args.kwargs['items'],
            [{'price': 'price_pif'}])

    def test_rerun_is_idempotent(self):
        from billing.contract_billing import run_contract_checkout
        c = _contract(self.account, self.site, 'installment', 'hosting')
        run_contract_checkout(c, payment_method_id='pm_1')
        c.refresh_from_db()
        c.paid_at_signing_at = None  # force the steps to run again
        c.save()
        run_contract_checkout(c)
        self.assertEqual(self.stripe.SubscriptionSchedule.create.call_count, 1)
        self.assertEqual(self.stripe.Subscription.create.call_count, 1)

    def test_sca_returns_client_secret(self):
        from billing.contract_billing import run_contract_checkout
        self.stripe.Subscription.retrieve.return_value = {
            'status': 'incomplete',
            'latest_invoice': {
                'id': 'in_1', 'status': 'open',
                'confirmation_secret': {'client_secret': 'pi_9_secret_x'}}}
        self.stripe.PaymentIntent.retrieve.return_value = {
            'status': 'requires_action'}
        c = _contract(self.account, self.site, 'installment', 'none')
        result = run_contract_checkout(c, payment_method_id='pm_1')
        self.assertEqual(result, {'requires_action': True,
                                  'client_secret': 'pi_9_secret_x'})
        c.refresh_from_db()
        self.assertIsNone(c.paid_at_signing_at)

    def test_legacy_contract_refused(self):
        from billing.contract_billing import run_contract_checkout
        legacy = Contract.objects.create(
            account=self.account, website_new=self.site,
            package='essential_build', build_price=Decimal('2500'),
            deposit_amount=Decimal('1250'), contract_text='<p>x</p>',
            signed=True, signed_at=timezone.now())
        self.assertIn('error', run_contract_checkout(legacy, 'pm_1'))

    def test_signing_redirects_to_pay_page(self):
        c = _contract(self.account, self.site, 'installment', 'none',
                      signed=False)
        with patch('clients.views.render_contract_pdf', return_value=''):
            r = self.client.post(
                reverse('clients:contract_sign', args=[c.contract_token]),
                {'signed_name': 'Casey Cool', 'agree': 'on'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r['Location'],
                         f'/pay/contract/{c.contract_token}/')
        # The legacy deposit page forwards new-terms contracts too.
        r = self.client.get(
            reverse('clients:contract_pay', args=[c.contract_token]))
        self.assertEqual(r['Location'], f'/pay/contract/{c.contract_token}/')

    def test_pay_page_renders_summary(self):
        c = _contract(self.account, self.site, 'installment', 'full_plan')
        r = self.client.get(f'/pay/contract/{c.contract_token}/')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '$250.00')
        self.assertNotContains(r, '50%')


# ── Webhooks ───────────────────────────────────────────────────────────────

class InstallmentWebhookTests(TestCase):

    def setUp(self):
        _seed()
        self.account, self.site = _account()
        self.account.stripe_customer_id = 'cus_1'
        self.account.save()
        self.site.stripe_build_installment_subscription_id = 'sub_inst'
        self.site.payment_status = 'installments_active'
        self.site.save()

    def _invoice_paid(self, n, sub='sub_inst', amount=10500):
        from billing.webhooks import _handle_invoice_paid
        _handle_invoice_paid({'data': {'object': {
            'id': f'in_{sub}_{n}', 'customer': 'cus_1',
            'amount_paid': amount,
            'parent': {'subscription_details': {'subscription': sub}},
            'lines': {'data': [{'description': 'Installment'}]},
        }}})

    def test_kind_inference(self):
        from billing.webhooks import _infer_subscription_kind
        kind, site = _infer_subscription_kind('sub_inst', self.account, {})
        self.assertEqual(kind, 'installment')
        self.assertEqual(site, self.site)
        self.site.build_paid_off_at = timezone.now()
        self.site.save()
        kind, _ = _infer_subscription_kind('sub_inst', self.account, {})
        self.assertEqual(kind, 'maintenance')

    def test_hosting_kind_carries_the_site(self):
        from billing.webhooks import _infer_subscription_kind
        self.site.stripe_hosting_subscription_id = 'sub_host'
        self.site.save()
        kind, site = _infer_subscription_kind('sub_host', self.account, {})
        self.assertEqual((kind, site), ('hosting', self.site))

    def test_24th_installment_marks_paid_off(self):
        for n in range(23):
            self._invoice_paid(n)
        self.site.refresh_from_db()
        self.assertEqual(self.site.build_installments_paid, 23)
        self.assertIsNone(self.site.build_paid_off_at)
        # Re-delivery of an already-recorded invoice does not count twice.
        self._invoice_paid(22)
        self.site.refresh_from_db()
        self.assertEqual(self.site.build_installments_paid, 23)
        self._invoice_paid(23)
        self.site.refresh_from_db()
        self.assertEqual(self.site.build_installments_paid, 24)
        self.assertIsNotNone(self.site.build_paid_off_at)
        self.assertEqual(self.site.payment_status, 'fully_paid')
        self.assertEqual(PaymentRecord.objects.filter(
            website=self.site, kind='installment').count(), 24)

    def test_subscription_deleted_clears_installment_ids(self):
        from billing.webhooks import _handle_subscription_deleted
        self.site.stripe_build_installment_schedule_id = 'sub_sched'
        self.site.save()
        _handle_subscription_deleted({'data': {'object': {
            'id': 'sub_inst', 'customer': 'cus_1'}}})
        self.site.refresh_from_db()
        self.assertEqual(self.site.stripe_build_installment_subscription_id,
                         '')
        self.assertEqual(self.site.stripe_build_installment_schedule_id, '')


# ── 30-day guarantee ───────────────────────────────────────────────────────

class GuaranteeMathTests(TestCase):

    def _rec(self, amount):
        return PaymentRecord(amount=Decimal(amount), stripe_id='x')

    def test_split_is_75_percent_and_never_exceeds_a_charge(self):
        from billing.guarantee import split_refund
        recs = [self._rec('2000.00'), self._rec('145.00')]
        total, refund, alloc = split_refund(recs)
        self.assertEqual(total, 214500)
        self.assertEqual(refund, 160875)
        self.assertEqual(sum(a for _, a in alloc), refund)
        for rec, cents in alloc:
            self.assertLessEqual(cents, int(rec.amount * 100))

    def test_rounding_cents_are_allocated(self):
        from billing.guarantee import split_refund
        recs = [self._rec('0.01'), self._rec('0.01'), self._rec('0.01')]
        total, refund, alloc = split_refund(recs)
        self.assertEqual(total, 3)
        self.assertEqual(refund, 2)  # 2.25 rounds half-up to 2
        self.assertEqual(sum(a for _, a in alloc), 2)
        self.assertTrue(all(a <= 1 for _, a in alloc))


class GuaranteeRefundTests(TestCase):

    def setUp(self):
        _seed()
        self.account, self.site = _account()
        self.contract = _contract(
            self.account, self.site, 'installment', 'hosting',
            signed_at=timezone.now() - timedelta(days=10))
        self.contract.stripe_objects = {
            'installment_schedule': 'sub_sched_1',
            'installment_subscription': 'sub_inst_1',
            'hosting_subscription': 'sub_host_1'}
        self.contract.save()
        self.site.stripe_build_installment_subscription_id = 'sub_inst_1'
        self.site.stripe_build_installment_schedule_id = 'sub_sched_1'
        self.site.stripe_hosting_subscription_id = 'sub_host_1'
        self.site.save()
        now = timezone.now()
        PaymentRecord.objects.create(
            account=self.account, website=self.site, kind='installment',
            amount=Decimal('105.00'), stripe_id='in_inst_1', paid_at=now)
        PaymentRecord.objects.create(
            account=self.account, website=self.site, kind='hosting',
            amount=Decimal('45.00'), stripe_id='in_host_1', paid_at=now)
        # Paid before signing — not part of this agreement.
        PaymentRecord.objects.create(
            account=self.account, website=self.site, kind='maintenance',
            amount=Decimal('999.00'), stripe_id='in_old',
            paid_at=now - timedelta(days=60))

    def _stripe_patches(self):
        refund_ids = iter(f're_{i}' for i in range(100))
        mocks = {
            'init': patch('billing.stripe_helpers._init'),
            'refund': patch('stripe.Refund.create',
                            side_effect=lambda **kw: {'id': next(refund_ids)}),
            'ip': patch('stripe.InvoicePayment.list', side_effect=lambda **kw: {
                'data': [{'payment': {'payment_intent':
                                      'pi_' + kw['invoice']}}]}),
            'sched_get': patch('stripe.SubscriptionSchedule.retrieve',
                               return_value={'status': 'active'}),
            'sched_cancel': patch('stripe.SubscriptionSchedule.cancel'),
            'sub_get': patch('stripe.Subscription.retrieve',
                             return_value={'status': 'active'}),
            'sub_cancel': patch('stripe.Subscription.cancel'),
        }
        return {k: v.start() for k, v in mocks.items()}, mocks

    def test_status_within_window(self):
        from billing.guarantee import guarantee_status
        st = guarantee_status(self.site)
        self.assertTrue(st['eligible'])
        self.assertEqual(st['total_paid'], Decimal('150.00'))
        self.assertEqual(st['refund_amount'], Decimal('112.50'))
        self.assertEqual(st['retained_amount'], Decimal('37.50'))
        self.assertEqual(st['days_remaining'], 20)

    def test_outside_window_not_eligible(self):
        from billing.guarantee import (
            execute_guarantee_refund, guarantee_status)
        self.contract.signed_at = timezone.now() - timedelta(days=31)
        self.contract.save()
        self.assertFalse(guarantee_status(self.site)['eligible'])
        with self.assertRaises(GuardError):
            execute_guarantee_refund(self.site, requested_by='t')

    def test_legacy_contract_not_eligible(self):
        from billing.guarantee import guarantee_status
        Contract.objects.filter(pk=self.contract.pk).update(payment_option='')
        self.assertFalse(guarantee_status(self.site)['eligible'])

    def test_execute_refunds_75_and_cancels_everything_once(self):
        from billing.guarantee import execute_guarantee_refund
        mocks, patchers = self._stripe_patches()
        try:
            row = execute_guarantee_refund(self.site, requested_by='Zach')
            self.assertEqual(row.status, 'completed')
            self.assertEqual(row.refund_amount, Decimal('112.50'))
            amounts = sorted(c.kwargs['amount']
                             for c in mocks['refund'].call_args_list)
            self.assertEqual(amounts, [3375, 7875])
            targets = {c.kwargs['payment_intent']
                       for c in mocks['refund'].call_args_list}
            self.assertEqual(targets, {'pi_in_inst_1', 'pi_in_host_1'})
            mocks['sched_cancel'].assert_called_once_with('sub_sched_1')
            cancelled = {c.args[0] for c in
                         mocks['sub_cancel'].call_args_list}
            self.assertEqual(cancelled, {'sub_inst_1', 'sub_host_1'})
            self.site.refresh_from_db()
            self.assertEqual(self.site.stripe_hosting_subscription_id, '')
            self.assertEqual(
                self.site.stripe_build_installment_subscription_id, '')
            refunds = PaymentRecord.objects.filter(kind='refund')
            self.assertEqual(sum(r.amount for r in refunds),
                             Decimal('-112.50'))

            # Idempotent: a second run refuses and charges nothing more.
            with self.assertRaises(GuardError):
                execute_guarantee_refund(self.site, requested_by='Zach')
            self.assertEqual(mocks['refund'].call_count, 2)
        finally:
            for p in patchers.values():
                p.stop()

    def test_failed_run_retries_only_what_is_left(self):
        from billing.guarantee import execute_guarantee_refund
        mocks, patchers = self._stripe_patches()
        try:
            calls = {'n': 0}

            def flaky(**kw):
                calls['n'] += 1
                if calls['n'] == 2:
                    raise RuntimeError('stripe down')
                return {'id': f're_{calls["n"]}'}
            mocks['refund'].side_effect = flaky
            with self.assertRaises(GuardError):
                execute_guarantee_refund(self.site, requested_by='Zach')
            row = self.contract.guarantee_refund
            self.assertEqual(row.status, 'failed')
            self.assertEqual(len(row.refunds), 1)

            row = execute_guarantee_refund(self.site, requested_by='Zach')
            self.assertEqual(row.status, 'completed')
            self.assertEqual(len(row.refunds), 2)
            self.assertEqual(mocks['refund'].call_count, 3)
        finally:
            for p in patchers.values():
                p.stop()


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class GuaranteeAdminViewTests(TestCase):

    def setUp(self):
        _seed()
        staff = User.objects.create_user(
            username='st2', email='st2@example.com', password='pw-123456',
            is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        self.account, self.site = _account()
        _contract(self.account, self.site, 'installment', 'none')
        # A live installment subscription must NOT block this action.
        self.site.stripe_build_installment_subscription_id = 'sub_live'
        self.site.save()

    def test_billing_tab_shows_panel(self):
        r = self.client.get(
            reverse('admin_dashboard:v2_website_detail', args=[self.site.id])
            + '?tab=billing')
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, '30-day guarantee')
        self.assertContains(r, 'Cancel agreement')

    def test_post_calls_service_despite_live_subscription(self):
        with patch('billing.guarantee.execute_guarantee_refund') as ex:
            ex.return_value = MagicMock(
                refund_amount=Decimal('78.75'),
                retained_amount=Decimal('26.25'),
                cancelled_subscriptions=['sub_live'])
            r = self.client.post(
                reverse('admin_dashboard:v2_website_guarantee_refund',
                        args=[self.site.id]),
                {'confirmed': 'yes', 'reason': 'client asked'})
        self.assertEqual(r.status_code, 302)
        ex.assert_called_once()

    def test_requires_confirmation(self):
        with patch('billing.guarantee.execute_guarantee_refund') as ex:
            self.client.post(
                reverse('admin_dashboard:v2_website_guarantee_refund',
                        args=[self.site.id]))
        ex.assert_not_called()
