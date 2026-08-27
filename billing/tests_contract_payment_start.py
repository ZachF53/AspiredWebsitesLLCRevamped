"""
start_contract_payment on an account-based contract.

Third instance of the same root cause: `contract.client` is the legacy
ClientProfile FK and is null on contracts raised from the Website or
Account pages. This function read `client.user.email` and
`client.firm_name`, so payment could not be started at all — the view
caught the AttributeError, got None, and bounced the client to the
generic "contract signed" page with no way to pay.

It also did `OnboardingInvoice.objects.filter(client=client)`. With a
null client that matches ANY invoice whose legacy FK is unset — another
account's row — which it then overwrote with this contract's line items.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from clients.account_models import Account, Website
from clients.models import Contract, ContractService, OnboardingInvoice

User = get_user_model()


def _fake_stripe_objects():
    customer = type('C', (), {'id': 'cus_fake'})()
    intent = type('P', (), {'id': 'pi_fake',
                            'client_secret': 'pi_fake_secret_x'})()
    return customer, intent


@override_settings(STRIPE_SECRET_KEY='sk_test_dummy')
class StartContractPaymentForAccount(TestCase):

    def setUp(self):
        owner = User.objects.create_user(
            username='payowner', email='payowner@example.com',
            password='test-pass-123')
        self.account = Account.objects.filter(user=owner).first() or (
            Account.objects.create(user=owner, name='Pay Owner Co'))
        self.account.websites.all().delete()
        self.website = Website.objects.create(
            account=self.account, name='Pay Owner Site')
        self.contract = Contract.objects.create(
            account=self.account, website_new=self.website,
            build_price=Decimal('750'), deposit_amount=Decimal('375'),
            timeline_weeks=4, contract_text='<p>x</p>',
            signed=True)
        ContractService.objects.create(
            contract=self.contract, service_type='build',
            tier_slug='custom', tier_name='Custom Website Build',
            price=Decimal('750'), deposit_amount=Decimal('375'),
            is_recurring=False, billing_interval='')

    def _start(self, amount='375', is_deposit=True):
        from billing.stripe_helpers import start_contract_payment
        with patch('billing.stripe_helpers.create_onboarding_payment_intent',
                   return_value=_fake_stripe_objects()) as m:
            invoice = start_contract_payment(
                self.contract, Decimal(amount), is_deposit=is_deposit)
        return invoice, m

    def test_contract_has_no_legacy_client(self):
        self.assertIsNone(self.contract.client)

    def test_deposit_payment_starts_and_returns_an_invoice(self):
        invoice, _ = self._start()
        self.assertIsNotNone(invoice, 'payment could not be started')
        self.assertEqual(invoice.total_amount, Decimal('375'))
        self.assertTrue(invoice.is_deposit)
        self.assertEqual(invoice.status, 'sent')
        self.assertEqual(invoice.stripe_payment_intent_id, 'pi_fake')

    def test_invoice_is_linked_to_the_account_and_website(self):
        invoice, _ = self._start()
        self.assertEqual(invoice.account_new, self.account)
        self.assertEqual(invoice.website_new, self.website)
        self.assertEqual(invoice.contract, self.contract)

    def test_stripe_gets_the_accounts_email_and_name(self):
        _, mock = self._start()
        kwargs = mock.call_args.kwargs
        self.assertEqual(kwargs['email'], 'payowner@example.com')
        self.assertEqual(kwargs['name'], 'Pay Owner Co')

    def test_customer_id_is_stored_on_the_account(self):
        self._start()
        self.account.refresh_from_db()
        self.assertEqual(self.account.stripe_customer_id, 'cus_fake')

    def test_pay_in_full_is_not_flagged_as_deposit(self):
        invoice, _ = self._start(amount='750', is_deposit=False)
        self.assertFalse(invoice.is_deposit)
        self.assertEqual(invoice.total_amount, Decimal('750'))

    def test_another_accounts_null_client_invoice_is_not_hijacked(self):
        """filter(client=None) used to match an unrelated account's row."""
        other_user = User.objects.create_user(
            username='otherowner', email='otherowner@example.com',
            password='test-pass-123')
        other = Account.objects.filter(user=other_user).first() or (
            Account.objects.create(user=other_user, name='Other Co'))
        other.websites.all().delete()
        other_site = Website.objects.create(account=other, name='Other Site')
        stranger = OnboardingInvoice.objects.create(
            account_new=other, website_new=other_site,
            line_items=[{'description': 'Theirs', 'amount': '99.00'}],
            total_amount=Decimal('99'), status='draft')

        self._start()

        stranger.refresh_from_db()
        self.assertEqual(stranger.total_amount, Decimal('99'))
        self.assertEqual(stranger.line_items[0]['description'], 'Theirs')
