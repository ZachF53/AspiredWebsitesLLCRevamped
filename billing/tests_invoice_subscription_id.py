"""
billing.webhooks._invoice_subscription_id — regression for a real,
silent production bug found on staging.

Newer Stripe API versions moved an invoice's subscription reference
from a flat `invoice.subscription` field to
`invoice.parent.subscription_details.subscription`. The old flat read
(`invoice.get('subscription')`) silently returned '' for every
subscription invoice once the Stripe account moved onto one of those
versions — confirmed on staging via Stripe's own event log
(webhooks_delivered_at was set, i.e. the invoice.paid webhook WAS
received and returned 200 — the handler just found no sub_id to act
on). Two real consequences, both silent:
  - No PaymentRecord was ever written for a subscription renewal.
  - invoice.upcoming's droplet/domain-alive gate no-op'd on its first
    line (`if not sub_id: return`), so a hosting/domain subscription
    could renew after the resource behind it was already gone.
"""

import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.webhooks import _invoice_subscription_id
from clients.account_models import Account, Website
from clients.models import PaymentRecord
from clients.service_models import MaintenancePlan

User = get_user_model()

_seq = 0


def _account_and_website(**website_kwargs):
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'invsub{_seq}', email=f'invsub{_seq}@example.com',
        password='x')
    account = Account.objects.create(
        user=u, name=f'InvSub Co {_seq}',
        stripe_customer_id=f'cus_invsub{_seq}')
    website = Website.objects.create(
        account=account, name=f'InvSub Site {_seq}', build_platform='custom',
        **website_kwargs)
    return account, website


class InvoiceSubscriptionIdExtractionTests(TestCase):
    """Unit coverage for the extraction helper itself."""

    def test_new_nested_shape(self):
        invoice = {
            'parent': {
                'subscription_details': {'subscription': 'sub_new_123'},
            },
        }
        self.assertEqual(_invoice_subscription_id(invoice), 'sub_new_123')

    def test_old_flat_shape_still_works(self):
        invoice = {'subscription': 'sub_old_456'}
        self.assertEqual(_invoice_subscription_id(invoice), 'sub_old_456')

    def test_nested_takes_priority_when_both_present(self):
        invoice = {
            'subscription': 'sub_old_456',
            'parent': {
                'subscription_details': {'subscription': 'sub_new_123'},
            },
        }
        self.assertEqual(_invoice_subscription_id(invoice), 'sub_new_123')

    def test_neither_present_returns_empty_string(self):
        self.assertEqual(_invoice_subscription_id({}), '')
        self.assertEqual(_invoice_subscription_id({'parent': {}}), '')
        self.assertEqual(_invoice_subscription_id(
            {'parent': {'subscription_details': {}}}), '')


@override_settings(
    STRIPE_WEBHOOK_SECRET='',
    DEBUG=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
)
class InvoicePaidRecordsPaymentWithNewApiShapeTests(TestCase):
    """Full webhook-dispatch path with a realistic new-API-shape
    payload — this is the path that was silently broken on staging."""

    def _post(self, body):
        return self.client.post(
            reverse('billing:stripe_webhook'),
            data=json.dumps(body), content_type='application/json')

    def test_records_payment_for_new_shape_subscription_invoice(self):
        account, website = _account_and_website()
        MaintenancePlan.objects.create(
            account=account, website=website, tier_slug='hvac-full-plan',
            status='active', stripe_subscription_id='sub_new_shape_1')

        body = {
            'type': 'invoice.paid',
            'data': {'object': {
                'id': 'in_new_shape_1',
                'customer': account.stripe_customer_id,
                'amount_paid': 29750,
                'lines': {'data': [{'description': 'Denis Custom'}]},
                'parent': {
                    'subscription_details': {
                        'subscription': 'sub_new_shape_1'},
                },
                'metadata': {},
            }},
        }
        r = self._post(body)
        self.assertEqual(r.status_code, 200)

        pr = PaymentRecord.objects.filter(stripe_id='in_new_shape_1').first()
        self.assertIsNotNone(pr)
        self.assertEqual(pr.amount, Decimal('297.50'))
        self.assertEqual(pr.kind, 'maintenance')
        self.assertEqual(pr.account_id, account.id)

    def test_still_records_payment_for_old_flat_shape(self):
        """Backward compatibility — must not regress on whichever API
        version an environment happens to be pinned to."""
        account, website = _account_and_website()
        MaintenancePlan.objects.create(
            account=account, website=website, tier_slug='hvac-full-plan',
            status='active', stripe_subscription_id='sub_old_shape_1')

        body = {
            'type': 'invoice.paid',
            'data': {'object': {
                'id': 'in_old_shape_1',
                'customer': account.stripe_customer_id,
                'subscription': 'sub_old_shape_1',
                'amount_paid': 25000,
                'lines': {'data': [{'description': 'Full Plan'}]},
                'metadata': {},
            }},
        }
        r = self._post(body)
        self.assertEqual(r.status_code, 200)

        pr = PaymentRecord.objects.filter(stripe_id='in_old_shape_1').first()
        self.assertIsNotNone(pr)
        self.assertEqual(pr.amount, Decimal('250.00'))


@override_settings(
    STRIPE_WEBHOOK_SECRET='',
    DEBUG=True,
)
class InvoiceUpcomingGateFiresWithNewApiShapeTests(TestCase):
    """The droplet/domain-alive gate must not silently no-op just
    because the invoice uses the new API shape."""

    def _post(self, body):
        return self.client.post(
            reverse('billing:stripe_webhook'),
            data=json.dumps(body), content_type='application/json')

    def test_gate_is_reached_for_new_shape_hosting_invoice(self):
        account, website = _account_and_website(
            stripe_hosting_subscription_id='sub_hosting_new_1')

        body = {
            'type': 'invoice.upcoming',
            'data': {'object': {
                'customer': account.stripe_customer_id,
                'parent': {
                    'subscription_details': {
                        'subscription': 'sub_hosting_new_1'},
                },
            }},
        }
        # If the gate's sub_id extraction is broken, _domain_renewal_gate
        # and the droplet-alive check are never reached at all. Patching
        # the droplet-alive check and asserting it WAS called proves the
        # gate got past the `if not sub_id: return` line.
        with patch('billing.webhooks._domain_renewal_gate',
                   return_value=False) as mock_domain_gate:
            r = self._post(body)
        self.assertEqual(r.status_code, 200)
        mock_domain_gate.assert_called_once_with('sub_hosting_new_1')
