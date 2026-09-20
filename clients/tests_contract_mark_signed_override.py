"""
clients.services.admin_mark_contract_signed — admin override for a
client who signed outside the portal's real e-sign flow. Must require
a reason, must never fabricate ESIGN/UETA evidence (signed_ip /
signed_user_agent / signed_content_hash stay blank), and must make the
override visible wherever signed_name is shown.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from clients.account_models import Account, Website, WebsiteStageLog
from clients.models import Contract
from clients.services import GuardError, admin_mark_contract_signed

User = get_user_model()

_seq = 0


def _contract(**kwargs):
    global _seq
    _seq += 1
    u = User.objects.create_user(
        username=f'csign{_seq}', email=f'csign{_seq}@example.com',
        password='x')
    account = Account.objects.create(user=u, name=f'Contract Sign Co {_seq}')
    website = Website.objects.create(
        account=account, name=f'Contract Sign Site {_seq}',
        build_platform='custom')
    return Contract.objects.create(
        account=account, website_new=website,
        contract_text='Terms go here.', **kwargs)


class AdminMarkContractSignedTests(TestCase):

    def test_requires_a_reason(self):
        contract = _contract()
        with self.assertRaises(GuardError):
            admin_mark_contract_signed(contract, reason='', set_by='Tester')
        with self.assertRaises(GuardError):
            admin_mark_contract_signed(contract, reason='   ', set_by='Tester')
        contract.refresh_from_db()
        self.assertFalse(contract.signed)

    def test_marks_signed_with_reason(self):
        contract = _contract()
        admin_mark_contract_signed(
            contract, reason='Signed physical copy, scanned to file',
            set_by='Zach Long')
        contract.refresh_from_db()
        self.assertTrue(contract.signed)
        self.assertIsNotNone(contract.signed_at)
        self.assertIn('Admin override:', contract.signed_name)
        self.assertIn('Signed physical copy', contract.signed_name)

    def test_never_fabricates_real_signature_evidence(self):
        contract = _contract()
        admin_mark_contract_signed(
            contract, reason='Signed over email', set_by='Tester')
        contract.refresh_from_db()
        self.assertEqual(contract.signed_ip, None)
        self.assertEqual(contract.signed_user_agent, '')
        self.assertEqual(contract.signed_content_hash, '')

    def test_writes_audit_log_entry(self):
        contract = _contract()
        admin_mark_contract_signed(
            contract, reason='Signed over email', set_by='Zach Long')
        log = WebsiteStageLog.objects.filter(
            website=contract.website_new).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.set_by, 'Zach Long')
        self.assertIn('admin override', log.note)

    def test_idempotent_on_already_signed_contract(self):
        contract = _contract()
        admin_mark_contract_signed(
            contract, reason='Signed over email', set_by='Tester')
        contract.refresh_from_db()
        first_signed_at = contract.signed_at

        # Second call must not raise even with a blank reason — the
        # already-signed contract is returned unchanged before the
        # reason is even checked.
        result = admin_mark_contract_signed(contract, reason='', set_by='Tester')
        self.assertEqual(result.signed_at, first_signed_at)
