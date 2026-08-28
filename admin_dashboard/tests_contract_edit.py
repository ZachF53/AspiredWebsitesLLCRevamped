"""
Contract section numbering, and the per-client contract editor.

Numbering: Ownership, Revisions, Recurring Services and the Money-Back
guarantee are all conditional, but their numbers were written into the
headings as 4/5/6/7. A build-only agreement therefore rendered
1,2,3,4,5,7,8,9,10 — and the Signatures cross-reference ("see Section 9")
was hardcoded too, so it pointed at the wrong section whenever the
document was shorter.

Worse: those clauses keyed off `build` — the ServiceTier object — rather
than "was a build sold". A custom-priced build has no tier, so T-Bear's
live $750 agreement went out with NO ownership clause (the site stays
ours until final payment), no revision limit and no out-of-scope rate.

Editor: the generated template is a starting point; per-client wording
has to be editable before sending. A SIGNED contract must stay read-only
— signed_content_hash is a SHA-256 of the text as displayed, and editing
after the fact destroys the ESIGN/UETA audit trail.
"""

import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from clients.account_models import Account, Website
from clients.contract_template import generate_combined_contract_text
from clients.models import Contract, ContractService

User = get_user_model()


class _Owner:
    contact_name = 'Ernest Bear'
    name = 'T-Bear Tool Rentals'
    firm_name = ''
    account = None
    user = None


class _Tier:
    def __init__(self, name, price, recurring=False, interval=''):
        self.name = name
        self.price = Decimal(price)
        self.pages_included = 8
        self.practice_areas_included = 3
        self.timeline_weeks = 4
        self.is_recurring = recurring
        self.billing_interval = interval


CUSTOM_BUILD = [{'service_type': 'build', 'tier': None,
                 'price': Decimal('750'), 'name': 'Custom Website Build',
                 'platform': 'wordpress', 'weeks': 4}]


def _numbers(html):
    return [int(n) for n in re.findall(r'<h2>(\d+)\.', html)]


class SectionNumbering(TestCase):

    def _assert_sequential(self, html):
        nums = _numbers(html)
        self.assertTrue(nums, 'no numbered sections rendered')
        self.assertEqual(
            nums, list(range(1, len(nums) + 1)),
            f'section numbers are not sequential: {nums}')

    def test_custom_build_only(self):
        self._assert_sequential(
            generate_combined_contract_text(_Owner(), CUSTOM_BUILD))

    def test_tier_build_only(self):
        self._assert_sequential(generate_combined_contract_text(
            _Owner(), [{'service_type': 'build',
                        'tier': _Tier('Essential Build', '2500')}]))

    def test_maintenance_only(self):
        self._assert_sequential(generate_combined_contract_text(
            _Owner(), [{'service_type': 'maintenance',
                        'tier': _Tier('Essentials', '299', True, 'month')}]))

    def test_all_three_services(self):
        self._assert_sequential(generate_combined_contract_text(
            _Owner(), [
                {'service_type': 'build', 'tier': _Tier('Premium', '4500')},
                {'service_type': 'maintenance',
                 'tier': _Tier('Growth', '599', True, 'month')},
                {'service_type': 'social',
                 'tier': _Tier('Basic', '399', True, 'month')}]))

    def test_esign_cross_reference_matches_its_section(self):
        for services in (CUSTOM_BUILD,
                         [{'service_type': 'maintenance',
                           'tier': _Tier('Essentials', '299', True, 'month')}]):
            html = generate_combined_contract_text(_Owner(), services)
            actual = re.findall(
                r'<h2>(\d+)\. Electronic Signature Consent', html)
            referenced = re.findall(r'Consent in Section (\d+)', html)
            self.assertEqual(actual, referenced,
                             'Signatures points at the wrong section')


class BuildClausesSurviveWithoutATier(TestCase):
    """A custom-priced build has no ServiceTier — the protective clauses
    must still be in the agreement."""

    def setUp(self):
        self.html = generate_combined_contract_text(_Owner(), CUSTOM_BUILD)

    def test_ownership_clause_present(self):
        self.assertIn('Ownership', self.html)
        self.assertIn('until the final build payment has cleared', self.html)

    def test_revision_limit_present(self):
        self.assertIn('Revisions', self.html)
        self.assertIn('two (2) major revisions', self.html)

    def test_out_of_scope_rate_present(self):
        self.assertIn('per hour', self.html)

    def test_money_back_guarantee_present(self):
        self.assertIn('Money-Back', self.html)

    def test_maintenance_only_has_no_build_clauses(self):
        html = generate_combined_contract_text(
            _Owner(), [{'service_type': 'maintenance',
                        'tier': _Tier('Essentials', '299', True, 'month')}])
        self.assertNotIn('Ownership', html)
        self.assertNotIn('Money-Back', html)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ContractEditor(TestCase):

    def setUp(self):
        staff = User.objects.create_user(
            username='ceStaff', email='cestaff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)
        self.client.force_login(staff)

        u = User.objects.create_user(
            username='ceowner', email='ceowner@example.com', password='x')
        self.account = Account.objects.filter(user=u).first() or (
            Account.objects.create(user=u, name='Edit Co'))
        self.account.websites.all().delete()
        self.website = Website.objects.create(
            account=self.account, name='Edit Site', build_platform='wordpress')
        self.contract = Contract.objects.create(
            account=self.account, website_new=self.website,
            build_price=Decimal('750'), deposit_amount=Decimal('375'),
            timeline_weeks=4,
            contract_text='<div class="contract-doc"><h1>Services Agreement</h1></div>')
        ContractService.objects.create(
            contract=self.contract, service_type='build', tier_slug='custom',
            tier_name='Custom Website Build', price=Decimal('750'),
            deposit_amount=Decimal('375'), is_recurring=False,
            billing_interval='')

    def _url(self):
        return f'/admin-dashboard/contracts/{self.contract.id}/edit/'

    def test_page_renders_with_the_text_prefilled(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertIn('Services Agreement', resp.content.decode())

    def test_saving_replaces_the_text(self):
        new = ('<div class="contract-doc"><h1>Services Agreement</h1>'
               '<h2>1. Parties</h2><p>Bespoke clause for this client.</p></div>')
        self.client.post(self._url(), {'contract_text': new})
        self.contract.refresh_from_db()
        self.assertIn('Bespoke clause for this client.',
                      self.contract.contract_text)

    def test_empty_text_is_refused(self):
        before = self.contract.contract_text
        self.client.post(self._url(), {'contract_text': '   '})
        self.contract.refresh_from_db()
        self.assertEqual(self.contract.contract_text, before)

    def test_regenerate_rebuilds_from_the_service_lines(self):
        self.client.post(self._url(), {'action': 'regenerate',
                                       'contract_text': 'ignored'})
        self.contract.refresh_from_db()
        text = self.contract.contract_text
        self.assertIn('Custom Website Build', text)
        self.assertIn('$750', text)
        self.assertIn('Ownership', text)
        self.assertEqual(_numbers(text), list(range(1, len(_numbers(text)) + 1)))

    def test_regenerate_keeps_the_wordpress_wording(self):
        self.client.post(self._url(), {'action': 'regenerate'})
        self.contract.refresh_from_db()
        self.assertIn('WordPress', self.contract.contract_text)
        self.assertNotIn('hand-coded', self.contract.contract_text)

    def test_signed_contract_cannot_be_edited(self):
        self.contract.signed = True
        self.contract.signed_at = timezone.now()
        self.contract.signed_name = 'Ernest Bear'
        self.contract.save()
        before = self.contract.contract_text

        self.client.post(self._url(), {'contract_text': '<p>tampered</p>'})

        self.contract.refresh_from_db()
        self.assertEqual(self.contract.contract_text, before,
                         'a signed contract was edited — audit trail broken')

    def test_signed_contract_renders_read_only(self):
        self.contract.signed = True
        self.contract.signed_at = timezone.now()
        self.contract.signed_name = 'Ernest Bear'
        self.contract.save()
        html = self.client.get(self._url()).content.decode()
        self.assertIn('read only', html.lower())
        self.assertNotIn('name="contract_text"', html)
