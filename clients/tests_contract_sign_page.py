"""
The contract signing page is a standalone document.

Signing is a focused transaction, like paying an invoice. The page used
to extend base.html, so it carried the full marketing nav and footer —
every one of which is an exit from a page the client is meant to finish.
It now mirrors billing/pay_invoice.html.

The agreement's own h1/h2/h3 also inherited the marketing display scale,
which left about three legible lines visible in the scroll box.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from clients.account_models import Account, Website
from clients.models import Contract

User = get_user_model()


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ContractSignPageIsStandalone(TestCase):

    def setUp(self):
        owner = User.objects.create_user(
            username='signowner', email='signowner@example.com',
            password='test-pass-123')
        self.account = Account.objects.filter(user=owner).first() or (
            Account.objects.create(user=owner, name='Sign Co'))
        self.account.websites.all().delete()
        self.website = Website.objects.create(
            account=self.account, name='Sign Co Site')
        self.contract = Contract.objects.create(
            account=self.account, website_new=self.website,
            build_price=Decimal('750'), deposit_amount=Decimal('375'),
            timeline_weeks=4,
            contract_text='<h1>Services Agreement</h1><h2>1. Parties</h2>'
                          '<h3>2.1 Website Development</h3><p>Body.</p>')

    def _get(self):
        return self.client.get(f'/portal/contract/{self.contract.contract_token}/')

    def test_page_renders(self):
        self.assertEqual(self._get().status_code, 200)

    def test_no_site_navigation(self):
        html = self._get().content.decode()
        self.assertNotIn('<nav class="nav"', html)
        self.assertNotIn('nav-menu', html)

    def test_no_site_footer(self):
        html = self._get().content.decode()
        self.assertNotIn('site-footer', html)

    def test_uses_the_payment_page_shell(self):
        html = self._get().content.decode()
        self.assertIn('pay-body', html)
        self.assertIn('pay-card--wide', html)

    def test_agreement_text_is_scoped_for_document_sizing(self):
        """Without contract-doc the agreement renders at headline scale."""
        html = self._get().content.decode()
        self.assertIn('contract-doc', html)

    def test_signing_controls_are_present(self):
        html = self._get().content.decode()
        self.assertIn('name="signed_name"', html)
        self.assertIn('name="agree"', html)
        self.assertIn('contract-sign-btn', html)
        self.assertIn('csrfmiddlewaretoken', html)

    def test_still_noindex(self):
        html = self._get().content.decode()
        self.assertIn('noindex', html)

    def test_signed_contract_shows_confirmation_not_the_form(self):
        from django.utils import timezone
        self.contract.signed = True
        self.contract.signed_at = timezone.now()
        self.contract.signed_name = 'Ernest'
        self.contract.save()
        html = self._get().content.decode()
        self.assertIn('Ernest', html)
        self.assertNotIn('contract-sign-btn', html)
