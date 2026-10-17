"""
The receptionist wizard: V1-V5, and what the answers become.

The assertions that matter most are about the two answers that are
easiest to get subtly wrong:

* **Hours parsing.** "8:00am - 5:00pm" has to become windows that
  ``voice/schedule.py`` can invert. "closed" has to mean the agent
  covers the whole day, because a shop shut on Sunday is precisely when
  a no-heat call is worth the most.
* **The recording disclosure.** The client is told we add it, so it has
  to actually be added — once, and not again when they edit their
  greeting later.
"""

from datetime import time

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from clients.account_models import Account
from onboarding.models import Onboarding, OnboardingResponse
from onboarding.registry import ONBOARDING_QUESTIONS, visible_sections
from onboarding.validators import describe_answer_problems
from voice.materialise import (
    RECORDING_DISCLOSURE, apply_onboarding, parse_hours,
)
from voice.models import VoiceConsentAttestation

User = get_user_model()

_seq = 0

_BASE_ANSWERS = {
    'legal_business_name': 'Johnson Heating & Cooling',
    'business_phone': '(210) 555-0142',
    'number_strategy': 'forward_existing',
    'business_timezone': 'America/Chicago',
    'business_city': 'San Antonio',
    'business_state': 'TX',
    'support_email': 'office@johnsonhvac.com',
    'coverage_mode': 'after_hours_only',
    'hours_mon_fri': '8:00am - 5:00pm',
    'hours_saturday': 'closed',
    'hours_sunday': 'closed',
    'answer_mode': 'ai_first',
    'transfer_number': '(210) 555-0199',
    'after_transfer_fail': 'ai_takes_message',
    'greeting': 'Thanks for calling Johnson Heating and Cooling.',
    'services_offered': 'AC and heating repair. Residential only.',
    'service_area': 'San Antonio and 30 miles out.',
    'emergency_handling': 'No heat in winter goes straight through.',
    'collect_fields': 'name_phone_address_issue',
    'max_call_minutes': '10',
    'recording_enabled': 'yes',
    'consent_recording_disclosure': 'yes',
    'consent_numbers_lawful': 'yes',
    'attestation_typed_name': 'Dana Reyes',
    'attestation_title': 'Owner',
}


def _onboarding_with(**overrides):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'vonb{_seq}', email=f'vonb{_seq}@example.com',
        password='test-pass-123')
    Account.objects.create(user=user, name=f'Onb Co {_seq}',
                           onboarding_status='complete')
    ob = Onboarding.objects.create(
        user=user, product_type='voice_receptionist',
        tier_slug='hvac-voice-receptionist')
    answers = {**_BASE_ANSWERS, **overrides}
    for key, value in answers.items():
        if value is None:
            continue
        OnboardingResponse.objects.create(
            onboarding=ob, question_key=key, value=value)
    return ob


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class RegistryTests(TestCase):

    def test_product_is_registered(self):
        self.assertIn('voice_receptionist', ONBOARDING_QUESTIONS)
        keys = [s['key'] for s in ONBOARDING_QUESTIONS['voice_receptionist']]
        self.assertEqual(keys, ['V1', 'V2', 'V3', 'V4', 'V5'])

    def test_answer_mode_defaults_to_ai_first(self):
        """Spam screening only works in ai_first, so that is the default
        and the help text has to say why."""
        section = ONBOARDING_QUESTIONS['voice_receptionist'][2]
        question = next(q for q in section['questions']
                        if q['key'] == 'answer_mode')
        self.assertEqual(question['default'], 'ai_first')
        self.assertIn('screening only works if it answers first',
                      question['help'].lower())

    def test_timezone_is_required(self):
        """Every after-hours calculation depends on it and GHL has no
        timezone field of its own."""
        section = ONBOARDING_QUESTIONS['voice_receptionist'][0]
        question = next(q for q in section['questions']
                        if q['key'] == 'business_timezone')
        self.assertTrue(question['required'])
        self.assertFalse(question['skip_allowed'])

    def test_consent_questions_cannot_be_skipped(self):
        section = ONBOARDING_QUESTIONS['voice_receptionist'][4]
        for key in ('consent_recording_disclosure',
                    'consent_numbers_lawful', 'attestation_typed_name'):
            question = next(q for q in section['questions']
                            if q['key'] == key)
            self.assertTrue(question['required'], key)
            self.assertFalse(question['skip_allowed'], key)

    def test_all_sections_are_visible(self):
        ob = _onboarding_with()
        self.assertEqual(len(visible_sections(ob)), 5)


class HoursParsingTests(TestCase):

    def test_standard_range(self):
        self.assertEqual(parse_hours('8:00am - 5:00pm'),
                         (time(8, 0), time(17, 0)))

    def test_closed_is_a_real_answer_not_a_failure(self):
        for word in ('closed', 'Closed', 'none', '', '  '):
            self.assertIsNone(parse_hours(word), word)

    def test_bare_hours_assume_afternoon_close(self):
        """"8 - 5" is never 8am to 5am."""
        self.assertEqual(parse_hours('8 - 5'), (time(8, 0), time(17, 0)))

    def test_various_separators_and_spacing(self):
        for text in ('8:00am-5:00pm', '8:00 am to 5:00 pm',
                     '8:00am – 5:00pm'):
            self.assertEqual(parse_hours(text),
                             (time(8, 0), time(17, 0)), text)

    def test_noon_and_midnight_edges(self):
        self.assertEqual(parse_hours('12:00am - 12:00pm'),
                         (time(0, 0), time(12, 0)))

    def test_garbage_returns_none_rather_than_raising(self):
        self.assertIsNone(parse_hours('whenever we feel like it'))


class ValidatorTests(TestCase):

    def test_transfer_number_must_be_dialable(self):
        """A typo here is silent: the agent tries to transfer and fails
        every time, and nobody finds out for weeks."""
        self.assertTrue(describe_answer_problems('transfer_number', '555'))
        self.assertTrue(describe_answer_problems(
            'transfer_number', '(110) 555-0142'))
        self.assertEqual(describe_answer_problems(
            'transfer_number', '(210) 555-0142'), [])
        self.assertEqual(describe_answer_problems(
            'transfer_number', '+1 210 555 0142'), [])

    def test_hours_must_be_parseable_or_closed(self):
        self.assertEqual(describe_answer_problems(
            'hours_mon_fri', '8:00am - 5:00pm'), [])
        self.assertEqual(describe_answer_problems(
            'hours_saturday', 'closed'), [])
        self.assertTrue(describe_answer_problems(
            'hours_mon_fri', 'mornings mostly'))

    def test_ambiguous_hours_are_rejected(self):
        self.assertTrue(describe_answer_problems('hours_mon_fri', '8 - 5:00'))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class MaterialisationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_answers_land_on_the_program(self):
        program = apply_onboarding(_onboarding_with())
        self.assertEqual(program.legal_business_name,
                         'Johnson Heating & Cooling')
        self.assertEqual(program.timezone, 'America/Chicago')
        self.assertEqual(program.coverage_mode, 'after_hours_only')
        self.assertEqual(program.answer_mode, 'ai_first')
        self.assertEqual(program.transfer_number, '(210) 555-0199')
        self.assertEqual(program.max_call_minutes, 10)

    def test_weekday_hours_become_five_windows(self):
        program = apply_onboarding(_onboarding_with())
        windows = list(program.hours.all())
        self.assertEqual(len(windows), 5, (
            'Mon-Fri 8-5 with weekends closed should be five windows'))
        self.assertEqual({w.day_of_week for w in windows}, {0, 1, 2, 3, 4})
        self.assertEqual(windows[0].start_time, time(8, 0))
        self.assertEqual(windows[0].end_time, time(17, 0))

    def test_saturday_hours_are_stored_when_given(self):
        program = apply_onboarding(
            _onboarding_with(hours_saturday='9:00am - 1:00pm'))
        saturday = program.hours.filter(day_of_week=5).first()
        self.assertIsNotNone(saturday)
        self.assertEqual(saturday.end_time, time(13, 0))

    def test_recording_disclosure_is_added_to_the_greeting(self):
        """The client is told we handle this, so it has to happen."""
        program = apply_onboarding(_onboarding_with())
        self.assertIn(RECORDING_DISCLOSURE, program.greeting)

    def test_disclosure_is_not_added_twice(self):
        """A client who edits their greeting in the portal and keeps the
        line must not end up with two."""
        program = apply_onboarding(_onboarding_with(
            greeting='Thanks for calling. This call may be recorded.'))
        self.assertEqual(program.greeting.lower().count('may be recorded'), 1)

    def test_no_disclosure_when_recording_is_off(self):
        program = apply_onboarding(
            _onboarding_with(recording_enabled='no'))
        self.assertFalse(program.recording_enabled)
        self.assertNotIn('recorded', program.greeting.lower())

    def test_collect_fields_expands(self):
        program = apply_onboarding(_onboarding_with())
        self.assertEqual(program.collect_fields,
                         ['name', 'phone', 'address', 'issue'])

    def test_program_waits_on_us_not_the_client(self):
        """After the wizard the operator still has to build the GHL agent,
        so the status must say we are the blocker."""
        program = apply_onboarding(_onboarding_with())
        self.assertEqual(program.setup_status, 'awaiting_number')
        self.assertFalse(program.config_synced_to_ghl)

    def test_attestation_is_written_once(self):
        ob = _onboarding_with()
        apply_onboarding(ob)
        apply_onboarding(ob)
        attestations = VoiceConsentAttestation.objects.filter(onboarding=ob)
        self.assertEqual(attestations.count(), 1, (
            're-running materialisation signed the recording disclosure '
            'a second time'))
        att = attestations.first()
        self.assertEqual(att.typed_name, 'Dana Reyes')
        self.assertIn('may be recorded', att.attestation_text.lower())

    def test_attestation_snapshots_the_wording_shown(self):
        att = VoiceConsentAttestation.objects.filter(
            onboarding=_materialised()).first()
        self.assertTrue(att.attestation_text.strip())
        self.assertIn('[YES]', att.attestation_text)

    def test_rerun_updates_config_without_duplicating_hours(self):
        ob = _onboarding_with()
        apply_onboarding(ob)
        OnboardingResponse.objects.filter(
            onboarding=ob, question_key='hours_mon_fri').update(
                value='7:00am - 6:00pm')
        program = apply_onboarding(ob)
        self.assertEqual(program.hours.count(), 5)
        self.assertEqual(program.hours.first().start_time, time(7, 0))

    def test_defaults_fill_in_for_answers_never_touched(self):
        """The wizard saves on change, so an accepted pre-fill has no
        response row — the program must still get the value."""
        ob = _onboarding_with(emergency_handling=None,
                              after_transfer_fail=None)
        program = apply_onboarding(ob)
        self.assertTrue(program.emergency_handling, (
            'the pre-filled emergency instructions were lost'))
        self.assertEqual(program.after_transfer_fail, 'ai_takes_message')

    def test_missing_account_does_not_raise(self):
        global _seq
        _seq += 1
        user = User.objects.create_user(
            username=f'vorphan{_seq}', email=f'vorphan{_seq}@example.com',
            password='test-pass-123')
        ob = Onboarding.objects.create(
            user=user, product_type='voice_receptionist',
            tier_slug='hvac-voice-receptionist')
        self.assertIsNone(apply_onboarding(ob))


def _materialised():
    ob = _onboarding_with()
    apply_onboarding(ob)
    return ob
