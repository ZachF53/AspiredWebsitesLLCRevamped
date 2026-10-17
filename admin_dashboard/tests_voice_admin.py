"""
The operator surface, and the comp flow for both products.

Two things here are regression tests rather than feature tests:

* **The comp UI gate.** The comp block used to be wrapped in
  ``{% if account.legacy_client_profile %}``. The commit that moved every
  field read from ``profile.*`` to ``account.*`` left the gate behind, so
  an Account with no legacy profile rendered no comp UI at all and the
  whole feature was invisible. That is the exact shape of the bug this
  file pins, for both products at once.
* **Comping the receptionist consumes a cohort slot.** It is the only
  comp in the system with a pricing side effect, so it has to actually
  move the ladder.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account
from voice.entitlement import program_for, voice_entitlement
from voice.models import VoiceCallRecord, VoiceCohort, VoiceProgram

User = get_user_model()

_seq = 0


def _staff():
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f'vadmin{_seq}', email=f'vadmin{_seq}@example.com',
        password='test-pass-123', is_staff=True, is_superuser=True)


def _account(**kwargs):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'vacct{_seq}', email=f'vacct{_seq}@example.com',
        password='test-pass-123')
    defaults = dict(user=user, name=f'Admin Co {_seq}',
                    onboarding_status='complete')
    defaults.update(kwargs)
    return Account.objects.create(**defaults)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class QueueTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()
        self.client.force_login(_staff())

    def test_queue_renders_empty(self):
        response = self.client.get(reverse('admin_dashboard:voice_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'AI Voice Receptionist')

    def test_queue_shows_the_cohort_position(self):
        response = self.client.get(reverse('admin_dashboard:voice_list'))
        self.assertContains(response, 'Cohort 1')
        self.assertContains(response, '0 of 10 slots used')

    def test_waiting_on_us_sorts_above_live(self):
        """It is a work list, not a report."""
        live = program_for(_account(), create=True)
        live.setup_status = 'live'
        live.legal_business_name = 'Zzz Live Co'
        live.save()
        waiting = program_for(_account(), create=True)
        waiting.setup_status = 'awaiting_number'
        waiting.legal_business_name = 'Aaa Waiting Co'
        waiting.save()

        programs = self.client.get(
            reverse('admin_dashboard:voice_list')).context['programs']
        self.assertEqual(programs[0].pk, waiting.pk)

    def test_unattributed_calls_are_surfaced(self):
        """Minutes nobody is being billed for. Invisible unless flagged."""
        VoiceCallRecord.objects.create(
            program=None, ghl_call_id='orphan-1',
            started_at=timezone.now(), duration_seconds=300)
        response = self.client.get(reverse('admin_dashboard:voice_list'))
        self.assertContains(response, 'location we don')

    def test_unbilled_overage_is_surfaced(self):
        from datetime import timedelta
        from voice.models import VoiceUsagePeriod
        program = program_for(_account(), create=True)
        now = timezone.now()
        VoiceUsagePeriod.objects.create(
            program=program,
            period_start=now - timedelta(days=40),
            period_end=now - timedelta(days=10),
            overage_minutes=20, overage_amount=Decimal('5.00'))
        response = self.client.get(reverse('admin_dashboard:voice_list'))
        self.assertContains(response, 'never invoiced')

    def test_queue_requires_staff(self):
        self.client.logout()
        response = self.client.get(reverse('admin_dashboard:voice_list'))
        self.assertNotEqual(response.status_code, 200)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class DetailTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()
        self.client.force_login(_staff())
        self.program = program_for(_account(), create=True)

    def _url(self, name='voice_program_detail'):
        return reverse(f'admin_dashboard:{name}',
                       kwargs={'program_id': self.program.id})

    def test_detail_renders(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)

    def test_blocking_reasons_are_listed(self):
        response = self.client.get(self._url())
        self.assertContains(response, 'No GHL location id')

    def test_mark_live_is_refused_while_blocked(self):
        self.client.post(self._url('voice_mark_live'))
        self.program.refresh_from_db()
        self.assertNotEqual(self.program.setup_status, 'live')

    def test_mark_live_works_once_everything_is_present(self):
        from voice.models import VoiceConsentAttestation
        self.program.ghl_location_id = 'loc-1'
        self.program.ghl_agent_id = 'agent-1'
        self.program.ghl_token_encrypted = 'x'
        self.program.answering_number = '(210) 555-0100'
        self.program.greeting = 'Thanks for calling.'
        self.program.coverage_mode = 'all_hours'
        self.program.save()
        VoiceConsentAttestation.objects.create(
            program=self.program, version='v1', attestation_text='ok',
            typed_name='Dana', agreed_at=timezone.now())

        self.client.post(self._url('voice_mark_live'))
        self.program.refresh_from_db()
        self.assertEqual(self.program.setup_status, 'live')

    def test_recording_without_an_attestation_blocks_go_live(self):
        """The disclosure record is what keeps the client lawful where
        both parties must consent, so it gates activation."""
        self.program.ghl_location_id = 'loc-1'
        self.program.ghl_agent_id = 'agent-1'
        self.program.ghl_token_encrypted = 'x'
        self.program.answering_number = '(210) 555-0100'
        self.program.greeting = 'Thanks for calling.'
        self.program.coverage_mode = 'all_hours'
        self.program.recording_enabled = True
        self.program.save()

        reasons = self.program.blocking_reasons()
        self.assertTrue(any('recording' in r.lower() for r in reasons))

    def test_token_is_write_only(self):
        self.client.post(self._url('voice_save_credentials'),
                         {'ghl_token': 'super-secret-token'})
        self.program.refresh_from_db()
        self.assertTrue(self.program.ghl_token_encrypted)
        body = self.client.get(self._url()).content.decode('utf-8')
        self.assertNotIn('super-secret-token', body)

    def test_blank_token_submit_keeps_the_existing_one(self):
        self.client.post(self._url('voice_save_credentials'),
                         {'ghl_token': 'first-token'})
        self.program.refresh_from_db()
        stored = self.program.ghl_token_encrypted
        self.client.post(self._url('voice_save_credentials'),
                         {'ghl_token': '', 'ghl_location_id': 'loc-9'})
        self.program.refresh_from_db()
        self.assertEqual(self.program.ghl_token_encrypted, stored)
        self.assertEqual(self.program.ghl_location_id, 'loc-9')

    def test_computed_after_hours_windows_are_shown_not_business_hours(self):
        """An operator must not have to invert the schedule by hand."""
        from datetime import time
        from voice.models import BusinessHoursWindow
        self.program.coverage_mode = 'after_hours_only'
        self.program.save()
        for day in range(5):
            BusinessHoursWindow.objects.create(
                program=self.program, day_of_week=day,
                start_time=time(8, 0), end_time=time(17, 0))

        working = self.client.get(self._url()).context['working_hours']
        monday = working['days']['monday']
        self.assertEqual(
            [(w['start'], w['end']) for w in monday],
            [('00:00', '08:00'), ('17:00', '23:59')])


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class CompTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()
        self.client.force_login(_staff())
        self.account = _account()

    def _comp_url(self):
        return reverse('admin_dashboard:account_set_comp_tier',
                       kwargs={'account_id': self.account.id})

    def test_comp_ui_renders_without_a_legacy_client_profile(self):
        """REGRESSION: the comp block was gated on
        `account.legacy_client_profile`, so for an Account-native client
        the entire comp feature rendered as nothing at all."""
        self.assertIsNone(
            getattr(self.account, 'legacy_client_profile', None))
        response = self.client.get(reverse(
            'admin_dashboard:account_detail',
            kwargs={'account_id': self.account.id}))
        body = response.content.decode('utf-8')
        self.assertIn('Comp products', body, (
            'the comp card is invisible for an account with no legacy '
            'profile — comping is impossible for this client'))
        self.assertIn('comp_review_system', body)
        self.assertIn('comp_voice_receptionist', body)

    def test_comping_the_receptionist_grants_access(self):
        self.client.post(self._comp_url(), {
            'bucket': 'voice', 'comp_voice_receptionist': '1'})
        self.account.refresh_from_db()
        self.assertTrue(self.account.comp_voice_receptionist)
        self.assertTrue(voice_entitlement(self.account).has_access)

    def test_comping_creates_the_program_so_the_portal_works(self):
        self.client.post(self._comp_url(), {
            'bucket': 'voice', 'comp_voice_receptionist': '1'})
        self.assertTrue(
            VoiceProgram.objects.filter(account=self.account).exists())

    def test_comping_consumes_a_cohort_slot(self):
        """The only comp in the system with a pricing side effect."""
        self.client.post(self._comp_url(), {
            'bucket': 'voice', 'comp_voice_receptionist': '1'})
        cache.clear()
        cohort = VoiceCohort.objects.get(sequence=1)
        self.assertEqual(cohort.filled, 1)
        self.assertEqual(cohort.slots_remaining, 9)

    def test_unchecking_clears_the_comp_but_keeps_history(self):
        self.client.post(self._comp_url(), {
            'bucket': 'voice', 'comp_voice_receptionist': '1'})
        self.client.post(self._comp_url(), {'bucket': 'voice'})
        self.account.refresh_from_db()
        self.assertFalse(self.account.comp_voice_receptionist)
        self.assertTrue(
            VoiceProgram.objects.filter(account=self.account).exists(),
            'clearing the comp destroyed their setup and call history')

    def test_comping_does_not_invent_a_subscription(self):
        """A $0 MaintenancePlan row was explicitly rejected: billing
        surfaces would read it as a subscription."""
        self.client.post(self._comp_url(), {
            'bucket': 'voice', 'comp_voice_receptionist': '1'})
        self.assertFalse(
            self.account.maintenance_plans.exists())

    def test_the_two_product_comps_are_independent(self):
        self.client.post(self._comp_url(), {
            'bucket': 'voice', 'comp_voice_receptionist': '1'})
        self.account.refresh_from_db()
        self.assertTrue(self.account.comp_voice_receptionist)
        self.assertFalse(self.account.comp_review_system)

        self.client.post(self._comp_url(), {
            'bucket': 'reviews', 'comp_review_system': '1'})
        self.account.refresh_from_db()
        self.assertTrue(self.account.comp_review_system)
        self.assertTrue(self.account.comp_voice_receptionist, (
            'comping reviews cleared the receptionist comp'))

    def test_comp_does_not_leak_into_plan_identity(self):
        self.client.post(self._comp_url(), {
            'bucket': 'voice', 'comp_voice_receptionist': '1'})
        self.account.refresh_from_db()
        self.assertEqual(self.account.comp_maintenance_package, '')
        self.assertEqual(
            voice_entitlement(self.account).current_plan_slug, '')

    def test_ten_comps_escalate_the_ladder(self):
        """The owner's own account plus a free client are real slots. Ten
        of them move the price for the next paying customer."""
        for _ in range(10):
            account = _account()
            self.client.post(
                reverse('admin_dashboard:account_set_comp_tier',
                        kwargs={'account_id': account.id}),
                {'bucket': 'voice', 'comp_voice_receptionist': '1'})
        cache.clear()
        self.assertEqual(VoiceCohort.objects.count(), 2)
        self.assertEqual(
            VoiceCohort.objects.get(sequence=2).price, Decimal('147.00'))
