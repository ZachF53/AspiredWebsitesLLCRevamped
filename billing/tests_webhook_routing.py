"""
Stripe webhook routing through canonical owners.

The cutover contract puts `stripe_customer_id` on Account and
`stripe_invoice_id` / `stripe_hosting_subscription_id` on Website.

These tests were rewritten when the resolvers stopped trading the
canonical row back for a legacy one. Before, `_client_for_customer` found
the Account and then returned `account.legacy_client_profile` — and when
there was none, declared the webhook unroutable and dropped it. That is
the shape of every account created after the cutover, so a customer who
had just paid had their event discarded and an alert raised about it.

Resolving to the Account is the fix, and it makes the old
"unroutable" case disappear rather than merely reporting it better.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from billing.webhooks import (
    _client_for_customer,
    _client_for_invoice,
    _site_by_sub,
    _website_for_invoice,
)
from clients.account_models import Account, Website
from clients.models import ClientProfile


User = get_user_model()


class WebhookCustomerRoutingTests(TestCase):

    def setUp(self):
        user = User.objects.create_user(
            username='route', email='route@example.com',
            password='test-pass-123')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Routing Firm')
        self.account = self.profile.migrated_account
        self.website = self.account.websites.get()

    def test_a_customer_resolves_to_the_account(self):
        """The card and the billing relationship are account-level."""
        Account.objects.filter(pk=self.account.pk).update(
            stripe_customer_id='cus_routing_test')

        self.assertEqual(
            _client_for_customer('cus_routing_test'), self.account)

    def test_an_invoice_resolves_to_the_site_it_billed_for(self):
        Website.objects.filter(pk=self.website.pk).update(
            stripe_invoice_id='in_routing_test')

        self.assertEqual(
            _website_for_invoice('in_routing_test'), self.website)
        self.assertEqual(
            _client_for_invoice('in_routing_test'), self.account)

    def test_unknown_identifiers_return_none(self):
        with patch('core.system_alerts.record_alert'):
            self.assertIsNone(_client_for_customer('cus_does_not_exist'))
            self.assertIsNone(_client_for_customer(''))
        self.assertIsNone(_client_for_invoice('in_does_not_exist'))
        self.assertIsNone(_website_for_invoice(None))

    def test_an_unknown_customer_still_raises_an_alert(self):
        """A payment we cannot route to anyone is a genuine fault — that
        part of the old behaviour is worth keeping."""
        with patch('core.system_alerts.record_alert') as alert:
            self.assertIsNone(_client_for_customer('cus_nobody'))
        self.assertEqual(alert.call_count, 1)
        self.assertIn('cus_nobody', alert.call_args.kwargs['message'])


class CanonicalOnlyAccountTests(TestCase):
    """An Account with no legacy profile — the shape of every client
    created after the cutover."""

    def setUp(self):
        user = User.objects.create_user(
            username='orphanacct', email='orphanacct@example.com',
            password='test-pass-123')
        self.account = Account.objects.create(
            user=user, name='Canonical Only Ltd',
            stripe_customer_id='cus_canonical_only')

    def test_its_payments_route_normally(self):
        """This used to be the 'unroutable' case: the resolver found the
        Account, could not find a profile behind it, raised an alert and
        dropped the event. The customer had paid."""
        with patch('core.system_alerts.record_alert') as alert:
            resolved = _client_for_customer('cus_canonical_only')

        self.assertEqual(resolved, self.account)
        alert.assert_not_called()


class SubscriptionToSiteTests(TestCase):
    """Hosting and maintenance subscription ids live on Website."""

    def setUp(self):
        user = User.objects.create_user(
            username='subsite', email='subsite@example.com',
            password='test-pass-123')
        self.account = Account.objects.create(user=user, name='Two Site Co')
        self.first = Website.objects.create(
            account=self.account, name='First Site',
            stripe_hosting_subscription_id='sub_host_1')
        self.second = Website.objects.create(
            account=self.account, name='Second Site',
            stripe_hosting_subscription_id='sub_host_2')

    def test_each_site_is_found_by_its_own_subscription(self):
        """The reason this exists. Comparing the id against one profile
        could only ever match a single site, so the second site's
        renewals and cancellations matched nothing and did nothing."""
        self.assertEqual(
            _site_by_sub(self.account, 'stripe_hosting_subscription_id',
                         'sub_host_1'),
            self.first)
        self.assertEqual(
            _site_by_sub(self.account, 'stripe_hosting_subscription_id',
                         'sub_host_2'),
            self.second)

    def test_an_unknown_subscription_matches_nothing(self):
        self.assertIsNone(
            _site_by_sub(self.account, 'stripe_hosting_subscription_id',
                         'sub_nope'))

    def test_a_missing_account_or_id_is_none_not_a_crash(self):
        self.assertIsNone(
            _site_by_sub(None, 'stripe_hosting_subscription_id', 'sub_host_1'))
        self.assertIsNone(
            _site_by_sub(self.account, 'stripe_hosting_subscription_id', ''))

    def test_a_subscription_belonging_to_another_account_is_not_matched(self):
        """Scoping by account is what stops a webhook landing on the
        wrong customer's site."""
        other_user = User.objects.create_user(
            username='othersub', email='othersub@example.com', password='x')
        other = Account.objects.create(user=other_user, name='Other Co')

        self.assertIsNone(
            _site_by_sub(other, 'stripe_hosting_subscription_id',
                         'sub_host_1'))


class ContractInvoicePaidTests(TestCase):
    """`invoice.paid` on the contract flow, for a canonical-only client.

    `_handle_invoice_paid` resolves an Account, then assigned
    `payment_status` / `deposit_paid_at` / `final_paid_at` straight onto
    it. Those are Website fields — a build is paid for, not an account —
    so this raised AttributeError inside the webhook that had just taken
    the money, and `_on_deposit_paid` then passed the Account to a
    ClientProfile FK for a second failure behind it.
    """

    @classmethod
    def setUpTestData(cls):
        user = User.objects.create_user(
            username='contractpaid', email='contractpaid@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(
            user=user, name='Contract Paid Co',
            stripe_customer_id='cus_contract_paid')
        cls.website = Website.objects.create(
            account=cls.account, name='Contract Paid Site',
            stage='design', payment_status='awaiting_deposit')

        from clients.models import Contract
        cls.contract = Contract.objects.create(
            website_new=cls.website, signed=True)

    def _event(self, kind):
        return {
            'id': 'in_contract_paid',
            'customer': 'cus_contract_paid',
            'metadata': {'kind': kind,
                         'contract_id': str(self.contract.id)},
            'lines': {'data': []},
        }

    def test_a_deposit_marks_the_site_deposit_paid(self):
        from billing.webhooks import _handle_invoice_paid

        with patch('billing.webhooks.send_welcome_email'), \
                patch('billing.webhooks._schedule_intake_reminders'):
            _handle_invoice_paid({'data': {'object': self._event('deposit')}})

        self.website.refresh_from_db()
        self.assertEqual(self.website.payment_status, 'deposit_paid')
        self.assertIsNotNone(self.website.deposit_paid_at)

    def test_a_deposit_bootstraps_intake_and_the_vault(self):
        from clients.models import IntakeResponse
        from vault.models import ClientVault
        from billing.webhooks import _handle_invoice_paid

        with patch('billing.webhooks.send_welcome_email'), \
                patch('billing.webhooks._schedule_intake_reminders'):
            _handle_invoice_paid({'data': {'object': self._event('deposit')}})

        self.assertTrue(
            IntakeResponse.objects.filter(website_new=self.website).exists())
        self.assertTrue(
            ClientVault.objects.filter(account_new=self.account).exists())

    def test_the_final_payment_marks_the_site_fully_paid(self):
        from billing.webhooks import _handle_invoice_paid

        _handle_invoice_paid({'data': {'object': self._event('final')}})

        self.website.refresh_from_db()
        self.assertEqual(self.website.payment_status, 'fully_paid')
        self.assertEqual(self.website.final_invoice_url, '')

    def test_the_account_is_never_given_website_fields(self):
        """The specific regression: Account has no `payment_status`."""
        self.assertFalse(
            hasattr(self.account, 'payment_status'),
            'Account grew a payment_status — the split this guards is gone')
