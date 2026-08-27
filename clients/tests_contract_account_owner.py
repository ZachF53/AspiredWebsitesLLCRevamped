"""
Account-based contracts: signing must not crash, and the email must go
to somebody.

`contract.client` is the legacy ClientProfile FK. Contracts raised from
the Website or Account pages set `account` and leave `client` null, so:

  - contract_sign did `contract.client.package` -> AttributeError on
    None. Signing 500'd for every account-based contract.
  - send_contract_ready_email addressed `contract.client`, so
    `_recipient` returned [] and send_mail was a silent no-op. The
    signing link went nowhere while the operator saw "Contract sent".

Both shipped together, so the second hid the first: nobody could reach
the signing page to discover it crashed.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from clients.account_models import Account, Website
from clients.emails import _contract_owner, _recipient
from clients.models import Contract, ContractService

User = get_user_model()


def _contract(account, website):
    c = Contract.objects.create(
        account=account, website_new=website,
        build_price=Decimal('750'), deposit_amount=Decimal('375'),
        timeline_weeks=4,
        contract_text='<h1>Services Agreement</h1><p>Body.</p>')
    ContractService.objects.create(
        contract=c, service_type='build', tier_slug='custom',
        tier_name='Custom Website Build', price=Decimal('750'),
        deposit_amount=Decimal('375'), is_recurring=False,
        billing_interval='')
    return c


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class AccountContractSigning(TestCase):

    def setUp(self):
        owner = User.objects.create_user(
            username='acctsign', email='acctsign@example.com',
            password='test-pass-123')
        self.account = Account.objects.filter(user=owner).first() or (
            Account.objects.create(user=owner, name='Acct Sign Co'))
        self.account.websites.all().delete()
        self.website = Website.objects.create(
            account=self.account, name='Acct Sign Site')
        self.contract = _contract(self.account, self.website)

    def test_the_contract_has_no_legacy_client(self):
        """Guards the premise of both bugs."""
        self.assertIsNone(self.contract.client)

    def test_owner_resolves_to_the_account(self):
        self.assertEqual(_contract_owner(self.contract), self.account)

    def test_recipient_is_not_empty(self):
        self.assertEqual(
            _recipient(_contract_owner(self.contract)),
            ['acctsign@example.com'])

    def test_signing_does_not_crash_and_advances_the_website(self):
        resp = self.client.post(
            f'/portal/contract/{self.contract.contract_token}/',
            {'signed_name': 'Ernest', 'agree': 'on'})
        # Redirects to the deposit / pay-in-full choice.
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/pay/', resp['Location'])

        self.contract.refresh_from_db()
        self.assertTrue(self.contract.signed)
        self.assertEqual(self.contract.signed_name, 'Ernest')
        self.assertTrue(self.contract.signed_content_hash)

        self.website.refresh_from_db()
        self.assertEqual(self.website.lifecycle_status, 'contract_signed')
        self.assertEqual(self.website.payment_status, 'awaiting_deposit')

    def test_pay_choice_page_offers_both_amounts(self):
        self.client.post(
            f'/portal/contract/{self.contract.contract_token}/',
            {'signed_name': 'Ernest', 'agree': 'on'})
        html = self.client.get(
            f'/portal/contract/{self.contract.contract_token}/pay/'
        ).content.decode()
        self.assertIn('375', html)
        self.assertIn('750', html)


class AccountWithNoEmailIsReported(TestCase):
    """No address must be visible, not silently swallowed."""

    def test_recipient_empty_when_account_user_has_no_email(self):
        u = User.objects.create_user(username='noaddr', password='x')
        u.email = ''
        u.save(update_fields=['email'])
        acct = Account.objects.filter(user=u).first() or (
            Account.objects.create(user=u, name='No Addr Co'))
        site = Website.objects.create(account=acct, name='No Addr Site')
        self.assertEqual(_recipient(_contract_owner(_contract(acct, site))), [])
