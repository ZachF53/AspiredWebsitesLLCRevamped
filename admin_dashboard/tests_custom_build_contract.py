"""
Custom-priced build contracts, and the WordPress no-Droplet rule.

A one-off rate (friend discount, WordPress port, scoped project) goes
through the normal contract → pay at signing → intake flow. Since the
Sept 2026 terms there is no deposit: a custom price applies to a
pay-in-full build and is charged in full at signing.

Separately, intake completion enqueued Droplet provisioning
unconditionally, which would create and bill a server for a WordPress
client who hosts their own site.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from clients.account_models import Account, Website
from clients.models import Contract, ContractService

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class SendContractWithCustomPrice(TestCase):

    def setUp(self):
        from unittest.mock import MagicMock

        from django.core.management import call_command
        call_command('seed_pricing', stdout=MagicMock())
        staff = User.objects.create_user(
            username='cbstaff', email='cbstaff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)
        self.client.force_login(staff)

        owner = User.objects.create_user(
            username='cbowner', email='cbowner@example.com',
            password='test-pass-123')
        self.account = Account.objects.filter(user=owner).first() or (
            Account.objects.create(user=owner, name='Custom Co'))
        self.account.websites.all().delete()
        self.website = Website.objects.create(
            account=self.account, name='Custom Co Site', package='')

    def _url(self):
        return f'/admin-dashboard/websites/{self.website.id}/send-contract/'

    def test_custom_price_creates_pay_in_full_contract(self):
        resp = self.client.post(self._url(), {
            'custom_build_price': '750', 'build_platform': 'wordpress'})
        self.assertEqual(resp.status_code, 302)

        contract = Contract.objects.get(website_new=self.website)
        self.assertEqual(contract.build_price, Decimal('750.00'))
        self.assertIsNone(contract.deposit_amount)
        self.assertEqual(contract.payment_option, 'pay_in_full')
        self.assertTrue(contract.includes_build)

        svc = ContractService.objects.get(contract=contract)
        self.assertEqual(svc.service_type, 'build')
        self.assertEqual(svc.price, Decimal('750.00'))
        self.assertIsNone(svc.deposit_amount)

    def test_custom_price_refused_for_installments(self):
        self.client.post(self._url(), {
            'custom_build_price': '750', 'build_option': 'installment',
            'plan_option': 'none'})
        self.assertFalse(Contract.objects.filter(
            website_new=self.website).exists())

    def test_custom_price_and_platform_persist_on_the_website(self):
        self.client.post(self._url(), {
            'custom_build_price': '750', 'build_platform': 'wordpress'})
        self.website.refresh_from_db()
        self.assertEqual(self.website.custom_build_price, Decimal('750.00'))
        self.assertEqual(self.website.build_platform, 'wordpress')
        self.assertFalse(self.website.needs_droplet)
        self.assertEqual(self.website.lifecycle_status, 'contract_sent')

    def test_nothing_selected_is_refused(self):
        resp = self.client.post(self._url(), {
            'build_option': 'none', 'plan_option': 'none'})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Contract.objects.filter(
            website_new=self.website).exists())

    def test_negative_price_is_refused(self):
        resp = self.client.post(self._url(), {'custom_build_price': '-100'})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Contract.objects.filter(
            website_new=self.website).exists())

    def test_stored_price_is_reused_when_form_field_is_blank(self):
        """The quick-action button posts no price — it must still work."""
        self.website.custom_build_price = Decimal('750.00')
        self.website.save(update_fields=['custom_build_price'])
        self.client.post(self._url(), {})
        contract = Contract.objects.get(website_new=self.website)
        self.assertEqual(contract.build_price, Decimal('750.00'))

    def test_contract_text_omits_hand_coded_for_wordpress(self):
        self.client.post(self._url(), {
            'custom_build_price': '750', 'build_platform': 'wordpress'})
        text = Contract.objects.get(website_new=self.website).contract_text
        self.assertIn('WordPress', text)
        self.assertIn('$750', text)
        self.assertNotIn('$375', text)
        self.assertNotIn('50%', text)
        self.assertNotIn('hand-coded', text)
        self.assertNotIn('practice area pages', text)


class BuildPlatformDropletRule(TestCase):

    def setUp(self):
        owner = User.objects.create_user(
            username='dropowner', email='dropowner@example.com',
            password='test-pass-123')
        self.account = Account.objects.filter(user=owner).first() or (
            Account.objects.create(user=owner, name='Droplet Co'))
        self.account.websites.all().delete()

    def test_custom_is_the_default_and_wants_a_droplet(self):
        site = Website.objects.create(account=self.account, name='Default')
        self.assertEqual(site.build_platform, 'custom')
        self.assertTrue(site.needs_droplet)

    def test_wordpress_does_not_want_a_droplet(self):
        site = Website.objects.create(
            account=self.account, name='WP', build_platform='wordpress')
        self.assertFalse(site.needs_droplet)
