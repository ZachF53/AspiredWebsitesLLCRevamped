"""
The remaining-balance (final 50%) payment raised at Pre-Launch.

Eighth instance of the legacy-FK problem, and the most silent one:
start_contract_final_payment read contract.client for the Stripe
customer, the payer's email and the invoice lookup. On an
account-based contract that is null, so it raised — and
_issue_website_final_invoice wraps the call in a best-effort except.

Moving a build to Pre-Launch therefore raised NO final invoice, stored
no `final_invoice_url`, and sent no email, while the launch gate went on
blocking for a payment the client was never asked to make. Nothing
surfaced to the operator.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from clients.account_models import Account, Website
from clients.models import Contract, ContractService, OnboardingInvoice

User = get_user_model()


class _PI:
    id = 'pi_final_fake'
    client_secret = 'pi_final_fake_secret'


class _Cust:
    id = 'cus_final_fake'


@override_settings(STRIPE_SECRET_KEY='sk_test_dummy')
class FinalPaymentForAccountContract(TestCase):

    def setUp(self):
        u = User.objects.create_user(
            username='finalowner', email='finalowner@example.com',
            password='x')
        self.account = Account.objects.filter(user=u).first() or (
            Account.objects.create(user=u, name='Final Co'))
        self.account.websites.all().delete()
        self.website = Website.objects.create(
            account=self.account, name='Final Site',
            payment_status='deposit_paid', stage='review')
        self.contract = Contract.objects.create(
            account=self.account, website_new=self.website,
            build_price=Decimal('750'), deposit_amount=Decimal('375'),
            timeline_weeks=4, contract_text='<p>x</p>', signed=True)
        ContractService.objects.create(
            contract=self.contract, service_type='build',
            tier_slug='custom', tier_name='Custom Website Build',
            price=Decimal('750'), deposit_amount=Decimal('375'),
            is_recurring=False, billing_interval='')

    def test_contract_has_no_legacy_client(self):
        self.assertIsNone(self.contract.client)

    def test_final_amount_is_the_remaining_half(self):
        self.assertEqual(Decimal(self.contract.final_amount), Decimal('375'))

    def _run(self):
        from billing.stripe_helpers import start_contract_final_payment
        with patch('billing.stripe_helpers.stripe') as st:
            st.Customer.create.return_value = _Cust()
            st.PaymentIntent.create.return_value = _PI()
            return start_contract_final_payment(self.contract)

    def test_final_invoice_is_created(self):
        invoice = self._run()
        self.assertIsNotNone(invoice, 'no final invoice was raised')
        self.assertEqual(invoice.total_amount, Decimal('375'))
        self.assertFalse(invoice.is_deposit)
        self.assertEqual(invoice.status, 'sent')
        self.assertEqual(invoice.website_new, self.website)
        self.assertEqual(invoice.account_new, self.account)

    def test_customer_id_is_stored_on_the_account(self):
        self._run()
        self.account.refresh_from_db()
        self.assertEqual(self.account.stripe_customer_id, 'cus_final_fake')

    def test_stage_change_to_pre_launch_stores_the_pay_url(self):
        from admin_dashboard.views import _issue_website_final_invoice
        with patch('billing.stripe_helpers.stripe') as st:
            st.Customer.create.return_value = _Cust()
            st.PaymentIntent.create.return_value = _PI()
            _issue_website_final_invoice(self.website)

        self.website.refresh_from_db()
        self.assertTrue(
            self.website.final_invoice_url,
            'Pre-Launch must store the on-site pay link for the portal button')
        self.assertIn('/pay/', self.website.final_invoice_url)

    def test_fully_paid_build_is_not_invoiced_again(self):
        from admin_dashboard.views import _issue_website_final_invoice
        self.website.payment_status = 'fully_paid'
        self.website.save(update_fields=['payment_status'])
        _issue_website_final_invoice(self.website)
        self.assertFalse(
            OnboardingInvoice.objects.filter(website_new=self.website).exists())

    def test_another_accounts_null_client_invoice_is_not_hijacked(self):
        other_u = User.objects.create_user(
            username='otherfinal', email='otherfinal@example.com', password='x')
        other = Account.objects.filter(user=other_u).first() or (
            Account.objects.create(user=other_u, name='Other Final Co'))
        other.websites.all().delete()
        other_site = Website.objects.create(account=other, name='Other Final')
        stranger = OnboardingInvoice.objects.create(
            account_new=other, website_new=other_site,
            line_items=[{'description': 'Theirs', 'amount': '42.00'}],
            total_amount=Decimal('42'), status='draft')

        self._run()

        stranger.refresh_from_db()
        self.assertEqual(stranger.total_amount, Decimal('42'))
