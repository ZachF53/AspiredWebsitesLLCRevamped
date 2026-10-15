"""
Review entitlement — the branch matrix every other surface depends on.

Entitlement is not one row, which is the whole reason
``reviews.entitlement`` exists. These tests pin each of the four ways a
client can have access, and the two states that look like access but
aren't.

The one that matters most commercially: **a Full Plan client must never
be offered the $97 add-on.** They already pay for it, and selling it
twice is the bug this module was written to make impossible.
"""

from django.core.management import call_command
from django.test import TestCase, override_settings

from clients.account_models import Account, Website
from clients.service_models import MaintenancePlan
from django.contrib.auth import get_user_model
from django.utils import timezone

from reviews.entitlement import (
    has_review_system, nav_state, program_for, review_entitlement,
)
from reviews.models import ReviewProgram

User = get_user_model()

_seq = 0


def _account(**kwargs):
    """A fresh Account. Emails must be unique across the whole run."""
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'rvent{_seq}', email=f'rvent{_seq}@example.com',
        password='test-pass-123')
    defaults = dict(user=user, name=f'Ent Co {_seq}',
                    onboarding_status='complete')
    defaults.update(kwargs)
    return Account.objects.create(**defaults)


def _plan(account, slug, status='active'):
    return MaintenancePlan.objects.create(
        account=account, tier_slug=slug, status=status,
        started_at=timezone.now(),
        stripe_subscription_id=f'sub_{slug}_{account.pk}'[:80])


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class EntitlementMatrixTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_full_plan_financed_has_access_and_is_never_upsold(self):
        account = _account()
        _plan(account, 'hvac-full-plan')
        ent = review_entitlement(account)
        self.assertTrue(ent.has_access)
        self.assertEqual(ent.source, 'full_plan')
        self.assertFalse(ent.can_buy_standalone, (
            'a Full Plan client was offered the $97 add-on they already '
            'pay for'))
        self.assertFalse(ent.can_upgrade_full_plan)

    def test_full_plan_paid_in_full_has_access(self):
        account = _account()
        _plan(account, 'hvac-plan-paid-in-full')
        ent = review_entitlement(account)
        self.assertTrue(ent.has_access)
        self.assertEqual(ent.source, 'full_plan')

    def test_website_package_alone_grants_access(self):
        """A contract-signed client provisioned by plan_billing may carry
        the entitlement only on Website.package."""
        account = _account()
        Website.objects.create(
            account=account, name='Pkg Co', package='hvac_full_plan',
            onboarding_status='complete')
        ent = review_entitlement(account)
        self.assertTrue(ent.has_access)
        self.assertEqual(ent.source, 'full_plan')

    def test_operator_comp_grants_access(self):
        account = _account(comp_maintenance_package='hvac_plan_paid_in_full')
        ent = review_entitlement(account)
        self.assertTrue(ent.has_access)
        self.assertEqual(ent.source, 'comp')

    def test_standalone_purchase_has_access(self):
        account = _account()
        _plan(account, 'hvac-review-system')
        ent = review_entitlement(account)
        self.assertTrue(ent.has_access)
        self.assertEqual(ent.source, 'standalone')
        self.assertFalse(ent.can_buy_standalone)

    def test_cancelled_standalone_keeps_access_until_period_end(self):
        """'cancelled' means "cancels at period end" in this codebase —
        they have paid for the rest of the month and keep the service,
        the same rule cancel_maintenance_subscription encodes."""
        account = _account()
        _plan(account, 'hvac-review-system', status='cancelled')
        self.assertTrue(review_entitlement(account).has_access)

    def test_ended_standalone_has_no_access(self):
        account = _account()
        _plan(account, 'hvac-review-system', status='ended')
        ent = review_entitlement(account)
        self.assertFalse(ent.has_access)
        self.assertTrue(ent.can_buy_standalone)

    def test_hosting_only_is_flagged_and_can_do_both(self):
        account = _account()
        _plan(account, 'hvac-hosting-security')
        ent = review_entitlement(account)
        self.assertFalse(ent.has_access)
        self.assertTrue(ent.is_hosting_only)
        self.assertTrue(ent.can_buy_standalone)
        self.assertTrue(ent.can_upgrade_full_plan)

    def test_legacy_maintenance_client_gets_no_upgrade_button(self):
        """Moving a pre-HVAC plan to the Full Plan is a conversation, not
        a self-serve button."""
        account = _account()
        _plan(account, 'maintenance-growth')
        ent = review_entitlement(account)
        self.assertFalse(ent.has_access)
        self.assertTrue(ent.can_buy_standalone)
        self.assertFalse(ent.can_upgrade_full_plan)

    def test_no_plan_at_all(self):
        account = _account()
        ent = review_entitlement(account)
        self.assertFalse(ent.has_access)
        self.assertEqual(ent.source, '')
        self.assertTrue(ent.can_buy_standalone)

    def test_none_account_is_safe(self):
        ent = review_entitlement(None)
        self.assertFalse(ent.has_access)
        self.assertEqual(nav_state(None)['badge'], '')

    def test_bundled_beats_standalone_when_a_client_has_both(self):
        """Someone who bought standalone and later took a Full Plan must
        read as full_plan, or the portal would keep showing the add-on."""
        account = _account()
        _plan(account, 'hvac-review-system')
        _plan(account, 'hvac-full-plan')
        ent = review_entitlement(account)
        self.assertEqual(ent.source, 'full_plan')
        self.assertFalse(ent.can_buy_standalone)

    def test_has_review_system_shorthand_agrees(self):
        account = _account()
        _plan(account, 'hvac-full-plan')
        self.assertTrue(has_review_system(account))
        self.assertFalse(has_review_system(_account()))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ProgramResolutionTests(TestCase):

    def test_program_for_creates_once_and_returns_the_same_row(self):
        account = _account()
        first = program_for(account, create=True)
        second = program_for(account, create=True)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            ReviewProgram.objects.filter(account=account).count(), 1)

    def test_program_for_does_not_create_without_the_flag(self):
        account = _account()
        self.assertIsNone(program_for(account))

    def test_new_program_gets_a_public_key(self):
        program = program_for(_account(), create=True)
        self.assertTrue(program.public_key)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class NavBadgeTests(TestCase):
    """The badge is how a client learns we are waiting on them without
    having to open the Review System tab."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_entitled_but_no_onboarding_shows_a_bang(self):
        account = _account()
        _plan(account, 'hvac-full-plan')
        self.assertEqual(nav_state(account)['badge'], '!')

    def test_awaiting_a2p_shows_setup(self):
        account = _account()
        _plan(account, 'hvac-full-plan')
        program = program_for(account, create=True)
        program.setup_status = 'awaiting_a2p'
        program.save(update_fields=['setup_status'])
        self._complete_onboarding(account)
        self.assertEqual(nav_state(account)['badge'], 'Setup')

    def test_paused_shows_paused(self):
        account = _account()
        _plan(account, 'hvac-full-plan')
        program = program_for(account, create=True)
        program.setup_status = 'live'
        program.sending_paused = True
        program.save(update_fields=['setup_status', 'sending_paused'])
        self._complete_onboarding(account)
        self.assertEqual(nav_state(account)['badge'], 'Paused')

    def test_live_and_running_shows_nothing(self):
        account = _account()
        _plan(account, 'hvac-full-plan')
        program = program_for(account, create=True)
        program.setup_status = 'live'
        program.save(update_fields=['setup_status'])
        self._complete_onboarding(account)
        self.assertEqual(nav_state(account)['badge'], '')

    def test_no_access_shows_nothing(self):
        self.assertEqual(nav_state(_account())['badge'], '')

    def _complete_onboarding(self, account):
        from onboarding.models import Onboarding
        Onboarding.objects.create(
            user=account.user, product_type='review_system',
            tier_slug='hvac-review-system', completed_at=timezone.now())
