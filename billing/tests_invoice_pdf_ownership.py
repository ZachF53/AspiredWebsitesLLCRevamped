"""
The invoice-PDF endpoint must refuse invoices that aren't yours.

`/billing/portal/invoices/<invoice_id>/pdf/` used to retrieve whatever
invoice id the URL carried and redirect straight to its Stripe-hosted
PDF, with @login_required as the only gate. Any authenticated client
could therefore read any other client's invoice — billing name, address,
line items, amounts — by substituting someone else's invoice id.

Stripe invoice ids are not secrets. They appear in receipt emails, on
payment pages, and in Stripe's own customer-facing URLs, so "hard to
guess" was never the control.

Stripe is mocked throughout: these tests are about the ownership
comparison, not about Stripe's behaviour.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from clients.account_models import Account

User = get_user_model()


def _account_for(user, name, customer_id):
    account = Account.objects.filter(user=user).first()
    if account is None:
        account = Account.objects.create(user=user, name=name)
    account.name = name
    account.stripe_customer_id = customer_id
    account.onboarding_status = 'complete'
    account.save()
    return account


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class InvoicePdfOwnershipTests(TestCase):

    def setUp(self):
        self.alice = User.objects.create_user(
            username='alice', email='alice@example.com', password='pw-123456')
        _account_for(self.alice, 'Alice Co', 'cus_alice')

        self.bob = User.objects.create_user(
            username='bob', email='bob@example.com', password='pw-123456')
        _account_for(self.bob, 'Bob Co', 'cus_bob')

    def _url(self, invoice_id):
        return reverse('billing:invoice_pdf', args=[invoice_id])

    def test_a_client_can_open_their_own_invoice(self):
        self.client.force_login(self.alice)
        with patch('billing.portal_views._stripe') as stripe:
            stripe.return_value.Invoice.retrieve.return_value = {
                'customer': 'cus_alice',
                'invoice_pdf': 'https://stripe.example/alice.pdf',
            }
            resp = self.client.get(self._url('in_alice1'))

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp['Location'], 'https://stripe.example/alice.pdf')

    def test_a_client_cannot_open_someone_elses_invoice(self):
        """The regression this file exists for."""
        self.client.force_login(self.bob)
        with patch('billing.portal_views._stripe') as stripe:
            stripe.return_value.Invoice.retrieve.return_value = {
                'customer': 'cus_alice',
                'invoice_pdf': 'https://stripe.example/alice.pdf',
            }
            resp = self.client.get(self._url('in_alice1'), follow=False)

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('stripe.example', resp['Location'])

    def test_a_client_with_no_customer_id_is_refused(self):
        """Otherwise a client Stripe has never seen would match an
        invoice whose `customer` is also falsy."""
        nomad = User.objects.create_user(
            username='nomad', email='nomad@example.com', password='pw-123456')
        _account_for(nomad, 'Nomad Co', '')

        self.client.force_login(nomad)
        with patch('billing.portal_views._stripe') as stripe:
            stripe.return_value.Customer.list.return_value.data = []
            stripe.return_value.Invoice.retrieve.return_value = {
                'customer': None,
                'invoice_pdf': 'https://stripe.example/orphan.pdf',
            }
            resp = self.client.get(self._url('in_orphan'))

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('stripe.example', resp['Location'])

    def test_the_refusal_is_indistinguishable_from_a_missing_invoice(self):
        """So the endpoint cannot be used to probe which ids exist."""
        self.client.force_login(self.bob)

        with patch('billing.portal_views._stripe') as stripe:
            stripe.return_value.Invoice.retrieve.return_value = {
                'customer': 'cus_alice',
                'invoice_pdf': 'https://stripe.example/alice.pdf',
            }
            refused = self.client.get(self._url('in_alice1'))

        with patch('billing.portal_views._stripe') as stripe:
            stripe.return_value.Invoice.retrieve.side_effect = Exception('404')
            missing = self.client.get(self._url('in_nonexistent'))

        self.assertEqual(refused.status_code, missing.status_code)
        self.assertEqual(refused['Location'], missing['Location'])
