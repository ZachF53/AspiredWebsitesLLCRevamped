"""
Receptionist entitlement — two grant paths, and the absence of a third.

``reviews/entitlement.py`` resolves four paths because the Review System
is bundled into the Full Plan. This resolves two, and the most important
assertion in this file is a negative one: **a Full Plan does not grant
the receptionist.** If that ever starts passing by accident, the $145
plan has silently acquired a per-minute cost centre.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from clients.account_models import Account
from clients.contract_options import (
    FULL_PLAN_SLUG, PLAN_PAID_IN_FULL_SLUG, REVIEW_SYSTEM_SLUG,
    VOICE_RECEPTIONIST_SLUG,
)
from clients.service_models import MaintenancePlan
from voice.entitlement import (
    has_voice_receptionist, nav_state, program_for, voice_entitlement,
)
from voice.models import VoiceProgram, VoiceUsagePeriod

User = get_user_model()

_seq = 0


def _account(**kwargs):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'vent{_seq}', email=f'vent{_seq}@example.com',
        password='test-pass-123')
    defaults = dict(user=user, name=f'Voice Co {_seq}',
                    onboarding_status='complete')
    defaults.update(kwargs)
    return Account.objects.create(**defaults)


def _plan(account, slug, status='active'):
    return MaintenancePlan.objects.create(
        account=account, tier_slug=slug, status=status,
        started_at=timezone.now(),
        stripe_subscription_id=f'sub_{slug}_{account.pk}'[:80])


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class GrantPathTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_standalone_purchase_grants_access(self):
        account = _account()
        _plan(account, VOICE_RECEPTIONIST_SLUG)
        ent = voice_entitlement(account)
        self.assertTrue(ent.has_access)
        self.assertEqual(ent.source, 'standalone')
        self.assertFalse(ent.can_buy_standalone)

    def test_comp_grants_access(self):
        ent = voice_entitlement(_account(comp_voice_receptionist=True))
        self.assertTrue(ent.has_access)
        self.assertEqual(ent.source, 'comp')

    def test_full_plan_does_NOT_grant_access(self):
        """The commercial rule. A Full Plan client must still be sold the
        receptionist, because the plan price does not cover its per-call
        vendor cost."""
        for slug in (FULL_PLAN_SLUG, PLAN_PAID_IN_FULL_SLUG):
            account = _account()
            _plan(account, slug)
            ent = voice_entitlement(account)
            self.assertFalse(ent.has_access, (
                f'{slug} granted the receptionist for free'))
            self.assertTrue(ent.can_buy_standalone, (
                f'a {slug} client cannot be sold the receptionist'))

    def test_review_system_comp_does_not_leak_across_products(self):
        """The two comp flags are independent. Comping reviews must not
        hand over the receptionist."""
        ent = voice_entitlement(_account(comp_review_system=True))
        self.assertFalse(ent.has_access)

    def test_no_plan_no_comp_is_no_access(self):
        ent = voice_entitlement(_account())
        self.assertFalse(ent.has_access)
        self.assertTrue(ent.can_buy_standalone)
        self.assertEqual(ent.source, '')

    def test_none_account_is_safe(self):
        self.assertFalse(voice_entitlement(None).has_access)
        self.assertEqual(nav_state(None)['badge'], '')

    def test_has_voice_receptionist_shorthand(self):
        self.assertTrue(has_voice_receptionist(
            _account(comp_voice_receptionist=True)))
        self.assertFalse(has_voice_receptionist(_account()))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ServedStatusTests(TestCase):
    """'cancelled' means cancels-at-period-end in this codebase and still
    serves. 'ended' and 'paused' do not."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_cancelled_still_serves(self):
        account = _account()
        _plan(account, VOICE_RECEPTIONIST_SLUG, status='cancelled')
        self.assertTrue(voice_entitlement(account).has_access)

    def test_ended_does_not_serve(self):
        account = _account()
        _plan(account, VOICE_RECEPTIONIST_SLUG, status='ended')
        self.assertFalse(voice_entitlement(account).has_access)

    def test_paused_does_not_serve(self):
        account = _account()
        _plan(account, VOICE_RECEPTIONIST_SLUG, status='paused')
        self.assertFalse(voice_entitlement(account).has_access)

    def test_awaiting_payment_serves(self):
        account = _account()
        _plan(account, VOICE_RECEPTIONIST_SLUG, status='awaiting_payment')
        self.assertTrue(voice_entitlement(account).has_access)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class PlanIdentityTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_receptionist_is_never_the_accounts_plan_identity(self):
        """An addon must not answer 'which plan are they on' — the portal
        would render 'Your plan: AI Voice Receptionist' and suppress the
        actual plan cards."""
        account = _account()
        _plan(account, VOICE_RECEPTIONIST_SLUG)
        self.assertEqual(voice_entitlement(account).current_plan_slug, '')

    def test_real_plan_still_shows_as_plan_identity(self):
        account = _account()
        _plan(account, PLAN_PAID_IN_FULL_SLUG)
        _plan(account, VOICE_RECEPTIONIST_SLUG)
        ent = voice_entitlement(account)
        self.assertEqual(ent.current_plan_slug, PLAN_PAID_IN_FULL_SLUG)
        self.assertTrue(ent.has_access)

    def test_comp_does_not_invent_a_plan_row(self):
        account = _account(comp_voice_receptionist=True)
        voice_entitlement(account)
        self.assertFalse(
            MaintenancePlan.objects.filter(account=account).exists(), (
                'comping the receptionist created a MaintenancePlan row, '
                'which billing surfaces read as a subscription'))

    def test_comp_does_not_touch_comp_maintenance_package(self):
        account = _account(comp_voice_receptionist=True)
        voice_entitlement(account)
        account.refresh_from_db()
        self.assertEqual(account.comp_maintenance_package, '')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ProgramAndNavTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_program_for_create_assigns_a_cohort(self):
        """Without a cohort the program is invisible to the slots
        counter, so an 11th account could buy at the opening price."""
        program = program_for(_account(), create=True)
        self.assertIsNotNone(program.cohort)
        self.assertEqual(program.locked_price, Decimal('97.00'))

    def test_program_for_is_idempotent(self):
        account = _account()
        first = program_for(account, create=True)
        second = program_for(account, create=True)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(VoiceProgram.objects.filter(
            account=account).count(), 1)

    def test_nav_badge_flags_missing_setup(self):
        account = _account(comp_voice_receptionist=True)
        program_for(account, create=True)
        self.assertEqual(nav_state(account)['badge'], '!')

    def test_nav_badge_over_outranks_setup_states(self):
        """A client burning paid overage needs to see that on every page,
        ahead of a setup nudge."""
        account = _account(comp_voice_receptionist=True)
        program = program_for(account, create=True)
        program.setup_status = 'live'
        program.save(update_fields=['setup_status'])
        now = timezone.now()
        VoiceUsagePeriod.objects.create(
            program=program,
            period_start=now - timezone.timedelta(days=5),
            period_end=now + timezone.timedelta(days=25),
            included_minutes=300, billable_seconds=320 * 60)
        self.assertEqual(nav_state(account)['badge'], 'Over')

    def test_no_access_means_no_badge(self):
        state = nav_state(_account())
        self.assertFalse(state['has'])
        self.assertEqual(state['badge'], '')
