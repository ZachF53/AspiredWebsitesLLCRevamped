"""
Attaching overage to a Stripe invoice.

Stripe is mocked throughout — these tests are about our decisions (which
period, how much, and never twice), not about Stripe's API.

The subtle one is ``test_item_is_pinned_to_the_invoice``: passing
``subscription`` without ``invoice`` makes Stripe treat the item as
*pending* and sweep it onto the FOLLOWING invoice. The charge would still
happen, a month late, with no error anywhere — which is why it gets an
explicit assertion rather than being left to the happy path.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from clients.account_models import Account
from clients.service_models import MaintenancePlan
from clients.contract_options import VOICE_RECEPTIONIST_SLUG
from voice import billing as voice_billing
from voice import usage as usage_mod
from voice.entitlement import program_for
from voice.models import VoiceCallRecord

User = get_user_model()

_seq = 0


class _FakeItem:
    def __init__(self, id='ii_test_123'):
        self.id = id


def _calendar_cycle():
    """A Stripe-shaped billing cycle covering now, for the retrieve mock.

    These tests have to set a non-empty STRIPE_SECRET_KEY to exercise the
    billing path, which also re-enables the Stripe lookup in
    ``usage.period_bounds_for``. Left unmocked that reaches
    api.stripe.com for real — it degrades to a calendar month so nothing
    fails, but it is a live network call inside the test suite, which is
    exactly what the blanked keys in settings_test exist to prevent.
    """
    now = timezone.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start.replace(year=start.year + 1, month=1)
           if start.month == 12 else start.replace(month=start.month + 1))
    return {
        'current_period_start': int(start.timestamp()),
        'current_period_end': int(end.timestamp()),
    }


class _NoLiveStripeMixin:
    def setUp(self):
        super().setUp()
        patcher = patch('stripe.Subscription.retrieve',
                        return_value=_calendar_cycle())
        patcher.start()
        self.addCleanup(patcher.stop)


def _account(**kwargs):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'vbil{_seq}', email=f'vbil{_seq}@example.com',
        password='test-pass-123')
    defaults = dict(user=user, name=f'Bill Co {_seq}',
                    stripe_customer_id=f'cus_test_{_seq}',
                    onboarding_status='complete')
    defaults.update(kwargs)
    return Account.objects.create(**defaults)


def _program_with_usage(minutes, *, subscription_id='sub_voice_1'):
    account = _account()
    MaintenancePlan.objects.create(
        account=account, tier_slug=VOICE_RECEPTIONIST_SLUG, status='active',
        started_at=timezone.now(), stripe_subscription_id=subscription_id)
    program = program_for(account, create=True)
    program.setup_status = 'live'
    program.ghl_location_id = f'loc-{program.pk.hex[:8]}'
    program.save()

    now = timezone.now()
    VoiceCallRecord.objects.create(
        program=program, ghl_call_id=f'bil-{program.pk.hex[:8]}',
        started_at=now, duration_seconds=minutes * 60)
    period = usage_mod.recompute(usage_mod.period_for(program, now))
    return program, period


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
                   STRIPE_SECRET_KEY='sk_test_fake')
class AttachOverageTests(_NoLiveStripeMixin, TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_overage_is_billed_with_the_right_quantity_and_rate(self):
        _, period = _program_with_usage(340)
        with patch('stripe.InvoiceItem.create',
                   return_value=_FakeItem()) as create:
            item_id = voice_billing.attach_overage(
                period, invoice_id='in_test_1')

        self.assertEqual(item_id, 'ii_test_123')
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs['quantity'], 40)
        self.assertEqual(kwargs['unit_amount_decimal'], '25.00')
        self.assertEqual(kwargs['customer'], period.program.account
                         .stripe_customer_id)

        period.refresh_from_db()
        self.assertEqual(period.stripe_invoice_item_id, 'ii_test_123')
        self.assertIsNotNone(period.billed_at)

    def test_item_is_pinned_to_the_invoice_not_left_pending(self):
        """Without an explicit `invoice`, Stripe defers the item to the
        NEXT invoice — the client is charged a month late and nothing
        errors."""
        _, period = _program_with_usage(310)
        with patch('stripe.InvoiceItem.create',
                   return_value=_FakeItem()) as create:
            voice_billing.attach_overage(period, invoice_id='in_test_2')
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs['invoice'], 'in_test_2')
        self.assertNotIn('subscription', kwargs, (
            'passing subscription alongside invoice is redundant and '
            'invites the pending-item behaviour this test guards'))

    def test_a_period_is_never_billed_twice(self):
        _, period = _program_with_usage(350)
        with patch('stripe.InvoiceItem.create',
                   return_value=_FakeItem()) as create:
            voice_billing.attach_overage(period, invoice_id='in_a')
            voice_billing.attach_overage(period, invoice_id='in_b')
        self.assertEqual(create.call_count, 1, (
            'the same usage period was billed twice'))

    def test_no_overage_creates_no_item_and_stays_recountable(self):
        """A period under the allowance must not be stamped — a late
        call could still push it over before the invoice finalises."""
        _, period = _program_with_usage(120)
        with patch('stripe.InvoiceItem.create',
                   return_value=_FakeItem()) as create:
            self.assertEqual(voice_billing.attach_overage(
                period, invoice_id='in_c'), '')
        create.assert_not_called()
        period.refresh_from_db()
        self.assertEqual(period.stripe_invoice_item_id, '')

    def test_missing_stripe_customer_does_not_raise(self):
        _, period = _program_with_usage(400)
        account = period.program.account
        account.stripe_customer_id = ''
        account.save(update_fields=['stripe_customer_id'])
        with patch('stripe.InvoiceItem.create') as create:
            self.assertEqual(voice_billing.attach_overage(
                period, invoice_id='in_d'), '')
        create.assert_not_called()

    def test_a_stripe_failure_leaves_the_period_unbilled(self):
        """So the daily sweep can retry it rather than it being recorded
        as billed."""
        _, period = _program_with_usage(360)
        with patch('stripe.InvoiceItem.create',
                   side_effect=RuntimeError('stripe down')):
            self.assertEqual(voice_billing.attach_overage(
                period, invoice_id='in_e'), '')
        period.refresh_from_db()
        self.assertEqual(period.stripe_invoice_item_id, '')
        self.assertIsNone(period.billed_at)

    def test_a_comped_client_is_never_billed_overage(self):
        """Comping the receptionist comps the minutes.

        This is not theoretical: both comped accounts in production carry
        a stripe_customer_id from other products, so without the guard
        the charge goes through cleanly and looks entirely legitimate on
        the invoice. The operator granted the product free and would be
        invoicing for it.
        """
        _, period = _program_with_usage(400)
        account = period.program.account
        account.comp_voice_receptionist = True
        account.save(update_fields=['comp_voice_receptionist'])

        with patch('stripe.InvoiceItem.create') as create:
            self.assertEqual(voice_billing.attach_overage(
                period, invoice_id='in_comped'), '')
        create.assert_not_called()

        period.refresh_from_db()
        self.assertTrue(period.billing_waived)
        self.assertEqual(period.billing_waived_reason, 'comped')

    def test_a_waived_period_is_not_reconsidered(self):
        """Waived is a decision, not a backlog. Re-examining it nightly
        forever is how a real unbilled period gets lost in the noise."""
        _, period = _program_with_usage(400)
        period.billing_waived = True
        period.save(update_fields=['billing_waived'])
        with patch('stripe.InvoiceItem.create') as create:
            voice_billing.attach_overage(period, invoice_id='in_x')
        create.assert_not_called()

    def test_a_paying_client_is_still_billed(self):
        """The guard must not have been bought by making nobody pay."""
        _, period = _program_with_usage(400)
        with patch('stripe.InvoiceItem.create',
                   return_value=_FakeItem('ii_paid')) as create:
            self.assertEqual(voice_billing.attach_overage(
                period, invoice_id='in_paid'), 'ii_paid')
        create.assert_called_once()
        period.refresh_from_db()
        self.assertFalse(period.billing_waived)

    def test_an_unresolvable_entitlement_fails_toward_not_billing(self):
        """An unbilled overage is a conversation; a wrong one is a refund
        and a dented relationship."""
        _, period = _program_with_usage(400)
        with patch('voice.entitlement.voice_entitlement',
                   side_effect=RuntimeError('db down')), \
                patch('stripe.InvoiceItem.create') as create:
            self.assertEqual(voice_billing.attach_overage(
                period, invoice_id='in_err'), '')
        create.assert_not_called()

    def test_recount_happens_before_billing(self):
        """A call inserted by the nightly sweep after the period was last
        computed must be included in the charge."""
        program, period = _program_with_usage(310)
        VoiceCallRecord.objects.create(
            program=program, ghl_call_id='late-bill-1',
            started_at=period.period_start + timezone.timedelta(hours=2),
            duration_seconds=20 * 60)
        with patch('stripe.InvoiceItem.create',
                   return_value=_FakeItem()) as create:
            voice_billing.attach_overage(period, invoice_id='in_f')
        self.assertEqual(create.call_args.kwargs['quantity'], 30)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
                   STRIPE_SECRET_KEY='sk_test_fake')
class InvoiceCreatedRoutingTests(_NoLiveStripeMixin, TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def _invoice(self, subscription_id, period_start):
        return {
            'id': 'in_routed_1',
            'subscription': subscription_id,
            'period_start': int(period_start.timestamp()),
        }

    def test_a_non_receptionist_invoice_is_ignored(self):
        with patch('stripe.InvoiceItem.create') as create:
            result = voice_billing.handle_invoice_created(
                self._invoice('sub_unrelated', timezone.now()))
        self.assertEqual(result, '')
        create.assert_not_called()

    def test_an_invoice_with_no_subscription_is_ignored(self):
        self.assertEqual(
            voice_billing.handle_invoice_created({'id': 'in_x'}), '')

    def test_the_closing_period_is_the_one_billed(self):
        """The invoice bills next month's fee; the minutes on it must be
        last month's."""
        program, period = _program_with_usage(
            330, subscription_id='sub_closing')
        boundary = period.period_end
        with patch('stripe.InvoiceItem.create',
                   return_value=_FakeItem('ii_closing')) as create:
            result = voice_billing.handle_invoice_created(
                self._invoice('sub_closing', boundary))
        self.assertEqual(result, 'ii_closing')
        self.assertEqual(create.call_args.kwargs['quantity'], 30)

    def test_handler_never_raises_on_a_broken_payload(self):
        """Stripe would redeliver an event whose side effect is a charge."""
        for payload in ({}, {'subscription': None},
                        {'subscription': 'sub_x', 'period_start': None}):
            self.assertEqual(
                voice_billing.handle_invoice_created(payload), '')
