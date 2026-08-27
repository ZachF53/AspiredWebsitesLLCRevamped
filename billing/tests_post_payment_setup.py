"""
What the buyer sees after paying: success page → account setup → intake.

Fourth and fifth instances of the legacy-FK problem. For an account-based
invoice:

  - pay_success read `invoice.client.firm_name` (None) so the page said
    "Thank you, ." and looked for the setup token on `onboarding_token`,
    which only exists on ClientProfile. setup_url stayed empty, so the
    template fell through to "We'll be in touch within 1 business day"
    and the buyer had no route into account setup at all.
  - onboarding_setup then did `onboarding_token.client.user`, which
    would have 500'd had anyone reached it.

Account carries the same PIN + onboarding_status columns as
ClientProfile, so the setup flow works on either once resolved.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from clients.account_models import Account, Website
from clients.models import OnboardingInvoice, OnboardingToken

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
                   SITE_BASE_URL='https://testserver')
class PostPaymentHandoff(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username='buyer2', email='buyer2@example.com',
            password='x', is_active=False)
        self.account = Account.objects.filter(user=self.user).first() or (
            Account.objects.create(user=self.user, name='Buyer Two Co'))
        self.account.name = 'Buyer Two Co'
        self.account.contact_name = 'Ernest Bear'
        self.account.save()
        self.account.websites.all().delete()
        self.website = Website.objects.create(
            account=self.account, name='Buyer Two Site')
        self.invoice = OnboardingInvoice.objects.create(
            account_new=self.account, website_new=self.website,
            line_items=[{'description': 'Deposit', 'amount': '375.00'}],
            total_amount=Decimal('375'), status='paid')
        self.token = OnboardingToken.objects.create(account_new=self.account)

    def test_invoice_has_no_legacy_client(self):
        self.assertIsNone(self.invoice.client)

    def test_success_page_greets_by_name(self):
        html = self.client.get(
            f'/pay/{self.invoice.payment_token}/success/').content.decode()
        self.assertIn('Buyer Two Co', html)
        self.assertNotIn('Thank you, .', html)

    def test_success_page_links_to_account_setup(self):
        html = self.client.get(
            f'/pay/{self.invoice.payment_token}/success/').content.decode()
        self.assertIn(f'/onboarding/setup/{self.token.token}/', html)
        self.assertIn('Set up my account', html)
        self.assertNotIn("We'll be in touch within 1 business day", html)

    def test_used_token_does_not_offer_setup_again(self):
        self.token.used = True
        self.token.save(update_fields=['used'])
        html = self.client.get(
            f'/pay/{self.invoice.payment_token}/success/').content.decode()
        self.assertNotIn('Set up my account', html)

    def test_setup_page_renders_for_an_account_token(self):
        resp = self.client.get(f'/onboarding/setup/{self.token.token}/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Ernest', resp.content.decode())

    def test_setup_sets_password_and_pin_then_goes_to_intake(self):
        resp = self.client.post(
            f'/onboarding/setup/{self.token.token}/',
            {'password': 'sup3rsecret', 'password_confirm': 'sup3rsecret',
             'pin_1': '1', 'pin_2': '2', 'pin_3': '3', 'pin_4': '4',
             'pin_confirm_1': '1', 'pin_confirm_2': '2',
             'pin_confirm_3': '3', 'pin_confirm_4': '4'})
        self.assertEqual(resp.status_code, 302)

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertTrue(self.user.check_password('sup3rsecret'))

        self.account.refresh_from_db()
        self.assertTrue(self.account.client_pin_set)
        self.assertEqual(self.account.onboarding_status, 'pending_intake')

        self.token.refresh_from_db()
        self.assertTrue(self.token.used)

    def test_setup_rejects_a_bad_pin(self):
        resp = self.client.post(
            f'/onboarding/setup/{self.token.token}/',
            {'password': 'sup3rsecret', 'password_confirm': 'sup3rsecret',
             'pin_1': '1', 'pin_2': '2', 'pin_3': '3', 'pin_4': '',
             'pin_confirm_1': '1', 'pin_confirm_2': '2',
             'pin_confirm_3': '3', 'pin_confirm_4': ''})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('PIN must be exactly 4 digits.', resp.content.decode())
        self.token.refresh_from_db()
        self.assertFalse(self.token.used)
