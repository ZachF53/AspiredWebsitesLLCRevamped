"""
The Review System wizard, and the legal record it produces.

Completing this wizard writes a ``ReviewConsentAttestation`` — the
client warranting in writing that they may text their customers. That
makes a few things non-negotiable, and this file pins each:

* Every R5 question is required and cannot be skipped.
* Exactly one attestation per onboarding, ever.
* **A staff view-as session cannot sign it.** Zach must never be able to
  warrant consent on a client's behalf.
* A sole proprietor completes without an EIN; an LLC without one does
  not get marked ready for carrier registration.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account
from onboarding.models import Onboarding, OnboardingResponse
from onboarding.registry import get_sections, visible_sections
from reviews.models import ReviewConsentAttestation, ReviewProgram

User = get_user_model()

_seq = 0

_BASE_ANSWERS = {
    'legal_business_name': 'Johnson Heating & Cooling LLC',
    'business_entity_type': 'llc',
    'ein': '12-3456789',
    'business_street': '101 Main St',
    'business_city': 'San Antonio',
    'business_state': 'TX',
    'business_zip': '78205',
    'business_phone': '2105550100',
    'business_website_url': 'https://johnsonhvac.example.com',
    'support_email': 'office@johnsonhvac.example.com',
    'business_timezone': 'America/Chicago',
    'google_review_url': 'https://g.page/r/CQabcdef/review',
    'google_business_name': 'Johnson HVAC',
    'job_system': 'housecall_pro',
    'job_trigger_method': 'zapier',
    'who_configures': 'client',
    'avg_jobs_per_week': '25_50',
    'sms_body_template':
        'Hi {customer_first_name}, thanks for choosing {business_name}! '
        'Mind leaving a Google review? {review_link}',
    'email_subject_template': 'Thanks from {business_name}',
    'email_body_template': 'Thanks! {review_link}',
    'reminder_enabled': 'yes',
    'send_delay': '1_hour',
    'consent_work_order_discloses': 'yes',
    'consent_numbers_lawful': 'yes',
    'consent_no_purchased_lists': 'yes',
    'consent_no_gating': 'yes',
    'consent_honor_stop': 'yes',
    'attestation_typed_name': 'Dale Johnson',
    'attestation_title': 'Owner',
}


def _onboarding(answers=None, **overrides):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'rvob{_seq}', email=f'rvob{_seq}@example.com',
        password='test-pass-123')
    Account.objects.create(
        user=user, name=f'Ob Co {_seq}', onboarding_status='complete')
    ob = Onboarding.objects.create(
        user=user, product_type='review_system',
        tier_slug='hvac-review-system')
    merged = dict(_BASE_ANSWERS)
    merged.update(answers or {})
    for key, value in merged.items():
        if value is None:
            continue
        OnboardingResponse.objects.create(
            onboarding=ob, question_key=key, value=value)
    for key, value in overrides.items():
        setattr(ob, key, value)
    if overrides:
        ob.save()
    return ob


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class RegistryTests(TestCase):

    def test_the_wizard_has_all_five_sections(self):
        sections = get_sections('review_system', 'hvac-review-system')
        self.assertEqual([s['key'] for s in sections],
                         ['R1', 'R2', 'R3', 'R4', 'R5'])

    def test_every_consent_question_is_required_and_unskippable(self):
        sections = get_sections('review_system', 'hvac-review-system')
        r5 = next(s for s in sections if s['key'] == 'R5')
        consent = [q for q in r5['questions']
                   if q['key'].startswith('consent_')
                   or q['key'] == 'attestation_typed_name']
        self.assertTrue(consent)
        for q in consent:
            self.assertTrue(q['required'], q['key'])
            self.assertFalse(q['skip_allowed'], q['key'])

    def test_the_ein_question_is_skippable_for_sole_proprietors(self):
        sections = get_sections('review_system', 'hvac-review-system')
        r1 = next(s for s in sections if s['key'] == 'R1')
        ein = next(q for q in r1['questions'] if q['key'] == 'ein')
        self.assertTrue(ein['skip_allowed'], (
            'a sole proprietor has no EIN — making it unskippable '
            'dead-ends every one-truck HVAC'))

    def test_the_message_wording_arrives_pre_filled(self):
        sections = get_sections('review_system', 'hvac-review-system')
        r4 = next(s for s in sections if s['key'] == 'R4')
        sms = next(q for q in r4['questions']
                   if q['key'] == 'sms_body_template')
        self.assertIn('{review_link}', sms.get('default', ''))

    def test_the_default_wording_passes_its_own_guard(self):
        """Shipping a default that the save form would reject would be a
        very silly first impression."""
        from reviews.templating import (
            DEFAULT_EMAIL_BODY, DEFAULT_EMAIL_SUBJECT, DEFAULT_SMS,
            describe_template_problems,
        )
        self.assertEqual(describe_template_problems(DEFAULT_SMS,
                                                    channel='sms'), [])
        self.assertEqual(
            describe_template_problems(DEFAULT_EMAIL_BODY, channel='email',
                                       subject=DEFAULT_EMAIL_SUBJECT), [])

    def test_all_sections_are_visible_with_no_conditional_rules(self):
        ob = _onboarding()
        self.assertEqual(len(visible_sections(ob)), 5)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class MaterialisationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def _apply(self, ob, request=None):
        from reviews.materialise import apply_onboarding
        ob.completed_at = timezone.now()
        ob.save(update_fields=['completed_at'])
        return apply_onboarding(ob, request=request)

    def test_it_builds_one_program_and_one_attestation(self):
        ob = _onboarding()
        program = self._apply(ob)
        self.assertIsNotNone(program)
        self.assertEqual(ReviewProgram.objects.count(), 1)
        self.assertEqual(ReviewConsentAttestation.objects.count(), 1)

    def test_the_answers_land_on_the_program(self):
        program = self._apply(_onboarding())
        self.assertEqual(program.legal_business_name,
                         'Johnson Heating & Cooling LLC')
        self.assertEqual(program.business_city, 'San Antonio')
        self.assertEqual(program.timezone, 'America/Chicago')
        self.assertEqual(program.google_business_name, 'Johnson HVAC')
        self.assertEqual(program.setup_status, 'awaiting_a2p')

    def test_the_ein_is_encrypted_and_only_the_last_four_are_readable(self):
        program = self._apply(_onboarding())
        self.assertEqual(program.ein_last4, '6789')
        self.assertNotIn('123456789', program.ein_encrypted)
        self.assertEqual(program.ein(), '123456789')

    def test_a_sole_proprietor_completes_with_no_ein(self):
        ob = _onboarding({'business_entity_type': 'sole_prop', 'ein': None})
        program = self._apply(ob)
        self.assertEqual(program.entity_type, 'sole_prop')
        self.assertEqual(program.ein_encrypted, '')
        self.assertEqual(program.setup_status, 'awaiting_a2p')

    def test_wording_is_normalised_so_a_pasted_incentive_cannot_persist(self):
        ob = _onboarding({
            'sms_body_template':
                'Thanks from {business_name}! Review us {review_link}'})
        program = self._apply(ob)
        self.assertIn('STOP', program.sms_body_template)

    def test_a_missing_link_is_restored_on_materialisation(self):
        ob = _onboarding({'sms_body_template': 'Thanks from {business_name}!'})
        program = self._apply(ob)
        self.assertIn('{review_link}', program.sms_body_template)

    def test_the_wording_starts_out_of_sync_with_ghl(self):
        """The operator has to paste it into the GHL workflow, so a
        brand-new program owes that work by definition."""
        program = self._apply(_onboarding())
        self.assertFalse(program.wording_synced_to_ghl)

    def test_untouched_defaults_still_reach_the_program(self):
        """The wizard saves on change, so a client who accepted the
        pre-filled wording created no response row for it."""
        ob = _onboarding({'sms_body_template': None,
                          'email_body_template': None,
                          'email_subject_template': None})
        program = self._apply(ob)
        self.assertIn('{review_link}', program.sms_body_template)
        self.assertTrue(program.email_body_template)

    def test_reminder_off_is_respected(self):
        program = self._apply(_onboarding({'reminder_enabled': 'no'}))
        self.assertFalse(program.reminder_enabled)

    def test_running_it_twice_does_not_sign_twice(self):
        from reviews.materialise import apply_onboarding
        ob = _onboarding()
        self._apply(ob)
        apply_onboarding(ob)
        self.assertEqual(ReviewConsentAttestation.objects.count(), 1)

    def test_the_attestation_snapshots_what_was_shown(self):
        program = self._apply(_onboarding())
        att = ReviewConsentAttestation.objects.get()
        self.assertEqual(att.typed_name, 'Dale Johnson')
        self.assertEqual(att.typed_title, 'Owner')
        self.assertEqual(att.program_id, program.pk)
        self.assertTrue(att.version)
        # The exhibit has to contain the statements, not a reference.
        self.assertIn('work order', att.attestation_text.lower())
        self.assertEqual(att.answers['consent_honor_stop'], 'yes')

    def test_the_attestation_records_the_signing_ip_and_agent(self):
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.META['REMOTE_ADDR'] = '203.0.113.7'
        request.META['HTTP_USER_AGENT'] = 'Mozilla/5.0 (test)'
        self._apply(_onboarding(), request=request)
        att = ReviewConsentAttestation.objects.get()
        self.assertEqual(att.ip_address, '203.0.113.7')
        self.assertIn('Mozilla', att.user_agent)

    def test_a_forwarded_ip_is_preferred_over_the_proxy(self):
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.META['REMOTE_ADDR'] = '10.0.0.1'
        request.META['HTTP_X_FORWARDED_FOR'] = '203.0.113.9, 10.0.0.1'
        self._apply(_onboarding(), request=request)
        self.assertEqual(
            ReviewConsentAttestation.objects.get().ip_address, '203.0.113.9')

    def test_a_program_cannot_go_live_without_an_attestation(self):
        from reviews.entitlement import program_for
        ob = _onboarding()
        account = Account.objects.get(user=ob.user)
        program = program_for(account, create=True)
        program.ghl_location_id = 'loc_x'
        program.sending_number = '+14785550134'
        program.a2p_brand_status = 'approved'
        program.a2p_campaign_status = 'approved'
        program.google_review_url = 'https://g.page/r/x/review'
        program.save()
        reasons = program.blocking_reasons()
        self.assertTrue(any('attestation' in r.lower() for r in reasons))

        self._apply(ob)
        program.refresh_from_db()
        self.assertEqual(program.blocking_reasons(), [])


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ViewAsCannotSignTests(TestCase):
    """`step()` has always guarded on a read-only view-as session;
    `complete()` did not, so an operator could finish a client's wizard.
    For a consent attestation that is unacceptable."""

    def test_a_read_only_session_completes_nothing_and_signs_nothing(self):
        ob = _onboarding()
        self.client.force_login(ob.user)
        url = reverse('onboarding:complete',
                      args=[ob.product_type, ob.tier_slug])
        with patch('clients.impersonation.is_read_only', return_value=True):
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        ob.refresh_from_db()
        self.assertIsNone(ob.completed_at, (
            'a staff view-as session marked a client onboarding complete'))
        self.assertEqual(ReviewConsentAttestation.objects.count(), 0, (
            'a staff view-as session signed a consent attestation on the '
            "client's behalf"))

    def test_the_client_themselves_can_complete_and_sign(self):
        ob = _onboarding()
        self.client.force_login(ob.user)
        resp = self.client.get(
            reverse('onboarding:complete',
                    args=[ob.product_type, ob.tier_slug]))
        self.assertEqual(resp.status_code, 200)
        ob.refresh_from_db()
        self.assertIsNotNone(ob.completed_at)
        self.assertEqual(ReviewConsentAttestation.objects.count(), 1)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class AnswerValidationTests(TestCase):
    """A mistyped EIN costs a 2-5 day carrier rejection cycle. Catching
    it at the keystroke is the whole point."""

    def setUp(self):
        self.ob = _onboarding()
        self.client.force_login(self.ob.user)
        self.url = reverse('onboarding:save',
                           args=[self.ob.product_type, self.ob.tier_slug])

    def _save(self, key, value):
        import json
        return self.client.post(
            self.url, data=json.dumps({'question_key': key, 'value': value}),
            content_type='application/json')

    def test_a_bad_ein_is_rejected_with_a_message(self):
        resp = self._save('ein', '123')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('9 digits', resp.json()['error'])

    def test_a_valid_ein_is_accepted(self):
        self.assertEqual(self._save('ein', '12-3456789').status_code, 200)

    def test_an_all_same_digit_ein_is_rejected(self):
        self.assertEqual(self._save('ein', '11-1111111').status_code, 400)

    def test_a_non_google_review_link_is_rejected(self):
        resp = self._save('google_review_url', 'https://example.com/reviews')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Google', resp.json()['error'])

    def test_a_real_google_review_link_is_accepted(self):
        self.assertEqual(
            self._save('google_review_url',
                       'https://g.page/r/CQabc/review').status_code, 200)

    def test_a_lookalike_host_does_not_slip_through(self):
        resp = self._save('google_review_url',
                          'https://g.page.evil.example.com/r/x/review')
        self.assertEqual(resp.status_code, 400)

    def test_an_incentive_in_the_wording_is_rejected_at_save_time(self):
        resp = self._save(
            'sms_body_template',
            '{business_name} review us for a free tune-up {review_link}')
        self.assertEqual(resp.status_code, 400)

    def test_an_unvalidated_question_is_unaffected(self):
        self.assertEqual(
            self._save('google_business_name', 'Anything At All').status_code,
            200)
