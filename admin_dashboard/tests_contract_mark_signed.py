"""v2 admin — "Mark signed (override)" button on the Onboarding tab's
contract step."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.account_models import Account, Website
from clients.models import Contract

User = get_user_model()

_seq = 0


def _staff():
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f'csign_staff{_seq}', email=f'csign_staff{_seq}@example.com',
        password='test-pass-123', is_staff=True, is_superuser=True)


def _website_with_unsigned_contract():
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'csign_client{_seq}', email=f'csign_client{_seq}@example.com',
        password='x')
    account = Account.objects.create(user=u, name=f'CSign Co {_seq}')
    website = Website.objects.create(
        account=account, name=f'CSign Site {_seq}', build_platform='custom')
    Contract.objects.create(
        account=account, website_new=website, contract_text='Terms.')
    return website


class ContractMarkSignedViewTests(TestCase):

    def setUp(self):
        self.client.force_login(_staff())

    def test_post_with_reason_marks_signed(self):
        website = _website_with_unsigned_contract()
        url = reverse('admin_dashboard:v2_contract_mark_signed',
                       args=[website.id])
        r = self.client.post(url, {'reason': 'Signed physical copy'},
                              follow=True)
        self.assertEqual(r.status_code, 200)
        contract = website.contracts.first()
        self.assertTrue(contract.signed)
        self.assertContains(r, 'admin override')

    def test_post_without_reason_shows_error_and_leaves_unsigned(self):
        website = _website_with_unsigned_contract()
        url = reverse('admin_dashboard:v2_contract_mark_signed',
                       args=[website.id])
        r = self.client.post(url, {'reason': ''}, follow=True)
        contract = website.contracts.first()
        self.assertFalse(contract.signed)
        self.assertContains(r, 'A reason is required')

    def test_no_unsigned_contract_shows_error(self):
        u = User.objects.create_user(
            username='csign_none', email='csign_none@example.com',
            password='x')
        account = Account.objects.create(user=u, name='No Contract Co')
        website = Website.objects.create(
            account=account, name='No Contract Site', build_platform='custom')
        url = reverse('admin_dashboard:v2_contract_mark_signed',
                       args=[website.id])
        r = self.client.post(url, {'reason': 'x'}, follow=True)
        self.assertContains(r, 'No unsigned contract exists')

    def test_blocked_for_live_subscription_website(self):
        website = _website_with_unsigned_contract()
        website.stripe_hosting_subscription_id = 'sub_csign_live'
        website.save(update_fields=['stripe_hosting_subscription_id'])
        url = reverse('admin_dashboard:v2_contract_mark_signed',
                       args=[website.id])
        r = self.client.post(url, {'reason': 'x'}, follow=True)
        contract = website.contracts.first()
        self.assertFalse(contract.signed)
        self.assertContains(r, 'live Stripe subscription')

    def test_onboarding_tab_shows_mark_signed_form_when_unsigned_contract_exists(self):
        website = _website_with_unsigned_contract()
        url = (reverse('admin_dashboard:v2_website_detail', args=[website.id])
               + '?tab=onboarding')
        r = self.client.get(url)
        self.assertContains(r, 'Mark signed (override)')
