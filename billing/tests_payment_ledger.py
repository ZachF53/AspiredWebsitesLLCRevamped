"""
The PaymentRecord ledger behind the portal Invoices page.

Ninth instance of the legacy-shape problem, and the one with the longest
blast radius: _record_payment assigned whatever the caller resolved to
`PaymentRecord.client`, a ClientProfile FK. Every canonical caller
resolves an Account, so it raised ValueError — swallowed by the
function's own except, which exists so a ledger hiccup never fails a
webhook that has already taken money.

The result: no payment of any kind was ever written to the ledger for an
account-based client. Their Invoices page read "No invoices yet" forever
and no receipt could be produced, while the money had gone through fine.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from billing.webhooks import _record_payment
from clients.account_models import Account, Website
from clients.models import ClientProfile, PaymentRecord

User = get_user_model()


class LedgerWritesForAccounts(TestCase):

    def setUp(self):
        u = User.objects.create_user(
            username='ledger', email='ledger@example.com', password='x')
        self.account = Account.objects.filter(user=u).first() or (
            Account.objects.create(user=u, name='Ledger Co'))
        self.account.websites.all().delete()
        self.website = Website.objects.create(
            account=self.account, name='Ledger Site')

    def test_account_as_client_is_recorded(self):
        _record_payment(
            client=self.account, stripe_id='pi_ledger_1', kind='deposit',
            amount=Decimal('375'), description='Deposit (50%)',
            website=self.website)

        rec = PaymentRecord.objects.get(stripe_id='pi_ledger_1')
        self.assertEqual(rec.account, self.account)
        self.assertEqual(rec.website, self.website)
        self.assertIsNone(rec.client, 'an Account is not a ClientProfile')
        self.assertEqual(rec.amount, Decimal('375'))
        self.assertEqual(rec.kind, 'deposit')

    def test_it_shows_up_on_the_accounts_ledger(self):
        _record_payment(
            client=self.account, stripe_id='pi_ledger_2', kind='final',
            amount=Decimal('375'), website=self.website)
        self.assertEqual(self.account.payment_records.count(), 1)

    def test_account_is_derived_from_the_website_when_not_passed(self):
        _record_payment(
            client=None, stripe_id='pi_ledger_3', kind='deposit',
            amount=Decimal('100'), website=self.website)
        rec = PaymentRecord.objects.get(stripe_id='pi_ledger_3')
        self.assertEqual(rec.account, self.account)

    def test_redelivery_does_not_duplicate(self):
        for _ in range(3):
            _record_payment(
                client=self.account, stripe_id='pi_ledger_4', kind='deposit',
                amount=Decimal('375'), website=self.website)
        self.assertEqual(
            PaymentRecord.objects.filter(stripe_id='pi_ledger_4').count(), 1)

    def test_legacy_profile_still_lands_on_client(self):
        u2 = User.objects.create_user(
            username='legacyledger', email='legacyledger@example.com',
            password='x')
        profile = ClientProfile.objects.create(user=u2, firm_name='Legacy Co')
        _record_payment(
            client=profile, stripe_id='pi_ledger_5', kind='deposit',
            amount=Decimal('500'))
        rec = PaymentRecord.objects.get(stripe_id='pi_ledger_5')
        self.assertEqual(rec.client, profile)

    def test_nothing_to_attach_to_is_skipped_not_crashed(self):
        _record_payment(
            client=None, stripe_id='pi_ledger_6', kind='deposit',
            amount=Decimal('10'))
        self.assertFalse(
            PaymentRecord.objects.filter(stripe_id='pi_ledger_6').exists())


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class InvoicesPageShowsThem(TestCase):

    def test_portal_invoices_lists_recorded_payments(self):
        u = User.objects.create_user(
            username='invportal', email='invportal@example.com',
            password='test-pass-123')
        account = Account.objects.filter(user=u).first() or (
            Account.objects.create(user=u, name='Inv Portal Co'))
        account.websites.all().delete()
        website = Website.objects.create(
            account=account, name='Inv Site',
            onboarding_status='onboarding_complete')
        account.onboarding_status = 'onboarding_complete'
        account.onboarding_complete = True
        account.save()

        _record_payment(
            client=account, stripe_id='pi_portal_1', kind='deposit',
            amount=Decimal('375'), description='Deposit (50%)',
            website=website)

        self.client.force_login(u)
        resp = self.client.get('/portal/invoices/', follow=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertNotIn(
            'No invoices yet', html,
            f'landed on {resp.redirect_chain}')
        self.assertIn('375', html)
