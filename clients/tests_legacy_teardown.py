"""
`clients.legacy_teardown` is exempt from the readiness gate. These tests
are the price of that exemption.

The gate allowlists it, so a legacy read added there is invisible to
`check_legacy_removal_readiness` — the same shape of blind spot that let
`request.client_profile` hide twenty-one reads, and `legacy_client_profile`
hide seventeen more. An allowlist entry is a promise about what a module
does; nothing enforced that promise until here.

Two properties, both checkable:

1. It only *removes*. Nothing in it may be a source of truth — no
   queryset a caller consults to decide something, no attribute read to
   pick a branch.
2. It stays small enough to delete whole. If it grows into a place where
   legacy logic accumulates, the exemption stops being honest.
"""

import ast
import pathlib

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from clients.account_models import Account
from clients.legacy_teardown import delete_legacy_mirror
from clients.models import ClientProfile

User = get_user_model()

MODULE = pathlib.Path(__file__).resolve().parent / 'legacy_teardown.py'


class TeardownBehaviourTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(
            username='teardown', email='teardown@example.com',
            password='test-pass-123')
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='Teardown Firm')

    def test_it_deletes_the_profile(self):
        self.assertTrue(delete_legacy_mirror(self.profile))
        self.assertFalse(ClientProfile.objects.filter(
            pk=self.profile.pk).exists())

    def test_none_is_not_an_error(self):
        """A canonical-only account has no mirror. That is the expected
        shape now, not a failure."""
        self.assertFalse(delete_legacy_mirror(None))

    def test_a_failure_is_swallowed_not_raised(self):
        """It runs inside the account-delete transaction. Failing to tidy
        a mirror must not abort the delete the admin asked for."""
        class Exploding:
            pk = 'exploding'

            def delete(self):
                raise RuntimeError('nope')

        with self.assertLogs('clients.legacy_teardown', 'ERROR'):
            self.assertFalse(delete_legacy_mirror(Exploding()))

    def test_deleting_an_account_takes_its_mirror_with_it(self):
        """The orphan this exists to prevent: `legacy_client_profile` is
        SET_NULL, so the profile outlives the account."""
        from clients.legacy_teardown import delete_mirror_for

        account = self.profile.migrated_account
        self.assertIsNotNone(account)

        delete_mirror_for(account)
        account.delete()

        self.assertFalse(Account.objects.filter(pk=account.pk).exists())
        self.assertFalse(ClientProfile.objects.filter(
            pk=self.profile.pk).exists())

    def test_the_mirrors_own_rows_go_with_it(self):
        """The profile owns intake, tickets and scans through its own FKs.
        Leaving it behind leaves those too."""
        from clients.models import SupportTicket
        from clients.legacy_teardown import delete_mirror_for

        SupportTicket.objects.create(
            client=self.profile, subject='Old ticket',
            description='raised before the cutover')

        account = self.profile.migrated_account
        delete_mirror_for(account)
        account.delete()

        self.assertFalse(SupportTicket.objects.filter(
            subject='Old ticket').exists())

    def test_a_canonical_only_account_deletes_cleanly(self):
        """No mirror to remove. This is the shape of every account
        created since the cutover."""
        from clients.legacy_teardown import delete_mirror_for

        user = User.objects.create_user(
            username='canonicaldel', email='cd@example.com', password='x')
        account = Account.objects.create(user=user, name='Canonical Only')

        self.assertFalse(delete_mirror_for(account))
        account.delete()
        self.assertFalse(Account.objects.filter(pk=account.pk).exists())


class TeardownStaysHonestTests(SimpleTestCase):
    """Static properties of the module, so the exemption keeps its
    meaning without anyone having to remember to check."""

    def _tree(self):
        return ast.parse(MODULE.read_text(encoding='utf-8'))

    def test_it_never_returns_a_legacy_row_to_a_caller(self):
        """Rule 1. Returning a ClientProfile would make this a reader
        wearing a teardown label, and the gate would keep trusting it.

        Three shapes hand back a bool and are fine: a bare constant,
        delegating to another function in this module, and a predicate
        like `isinstance(...)`. What must not appear is a return of an
        attribute or subscript, which is how a row gets out.
        """
        tree = self._tree()
        allowed_calls = {
            n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
        } | {'isinstance', 'bool'}

        leaks = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            value = node.value
            if isinstance(value, ast.Constant):
                continue
            if (isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id in allowed_calls):
                continue
            leaks.append(ast.dump(value)[:70])

        self.assertEqual(
            leaks, [],
            'legacy_teardown hands an object back to a caller. It is '
            'allowlisted on the promise that it only removes rows; '
            'returning one makes it a source of truth the readiness gate '
            f'cannot see: {leaks}')

    def test_it_defines_no_querysets(self):
        """`.objects` anywhere means it is selecting, not just deleting."""
        source = MODULE.read_text(encoding='utf-8')
        code = '\n'.join(
            line for line in source.splitlines()
            if not line.strip().startswith('#'))
        # Strip the docstring so prose about the design does not fail this.
        body = code.split('"""')
        code = ''.join(body[2:]) if len(body) > 2 else code

        self.assertNotIn(
            '.objects', code,
            'legacy_teardown builds a queryset. It is meant to remove rows '
            'it was handed, not go looking for them.')

    def test_it_stays_small_enough_to_delete_whole(self):
        functions = [
            n.name for n in ast.walk(self._tree())
            if isinstance(n, ast.FunctionDef)
        ]
        self.assertLessEqual(
            len(functions), 3,
            f'legacy_teardown has grown to {functions}. It is exempt from '
            'the readiness gate because it is a short list of things the '
            'drop deletes outright — if legacy logic is accumulating here, '
            'the exemption is hiding it.')

    def test_it_is_registered_as_allowed(self):
        from clients.legacy_audit import ALLOWED_PREFIXES

        self.assertIn('clients/legacy_teardown.py', ALLOWED_PREFIXES)
