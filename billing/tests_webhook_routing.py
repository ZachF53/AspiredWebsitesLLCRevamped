"""
Stripe webhook routing through canonical owners.

The cutover contract puts `stripe_customer_id` on Account and
`stripe_invoice_id` / `stripe_hosting_subscription_id` on Website. The
webhook handlers looked those identifiers up on ClientProfile, which works
only while the legacy mirror exists — and produces silence, not an error,
once it does not.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from billing.webhooks import _client_for_customer, _client_for_invoice
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

    def test_customer_is_resolved_through_the_account(self):
        Account.objects.filter(pk=self.account.pk).update(
            stripe_customer_id='cus_routing_test')
        # Deliberately NOT set on the legacy profile: the Account is the
        # contract's owner of this identifier.
        self.assertEqual(self.profile.stripe_customer_id, '')

        self.assertEqual(
            _client_for_customer('cus_routing_test'), self.profile)

    def test_legacy_lookup_still_works_for_unbackfilled_rows(self):
        ClientProfile.objects.filter(pk=self.profile.pk).update(
            stripe_customer_id='cus_legacy_only')
        Account.objects.filter(pk=self.account.pk).update(
            stripe_customer_id='')

        self.assertEqual(
            _client_for_customer('cus_legacy_only'), self.profile)

    def test_invoice_is_resolved_through_the_website(self):
        Website.objects.filter(pk=self.website.pk).update(
            stripe_invoice_id='in_routing_test')

        self.assertEqual(
            _client_for_invoice('in_routing_test'), self.profile)

    def test_unknown_identifiers_return_none(self):
        self.assertIsNone(_client_for_customer('cus_does_not_exist'))
        self.assertIsNone(_client_for_invoice('in_does_not_exist'))
        self.assertIsNone(_client_for_customer(''))
        self.assertIsNone(_client_for_invoice(None))


class UnroutablePaymentTests(TestCase):
    """An Account with no legacy profile is what every post-cutover
    client looks like. A payment for one must not disappear quietly."""

    def setUp(self):
        user = User.objects.create_user(
            username='orphanacct', email='orphanacct@example.com',
            password='test-pass-123')
        self.account = Account.objects.create(
            user=user, name='Canonical Only Ltd',
            stripe_customer_id='cus_canonical_only')

    def test_it_raises_a_system_alert_instead_of_returning_silently(self):
        with patch('core.system_alerts.record_alert') as alert:
            result = _client_for_customer('cus_canonical_only')

        self.assertIsNone(result)
        self.assertEqual(alert.call_count, 1)
        detail = alert.call_args.kwargs
        self.assertEqual(detail['severity'], 'error')
        self.assertIn('NOT recorded', detail['detail'])

    def test_the_alert_names_the_owner_so_it_can_be_reconciled(self):
        with patch('core.system_alerts.record_alert') as alert:
            _client_for_customer('cus_canonical_only')

        message = alert.call_args.kwargs['message']
        self.assertIn('cus_canonical_only', message)
