"""
The portal surfaces — the hub's state machine and the client controls.

The hub is one URL serving five states, so most of this is "does the
right page come back". The assertions that protect money and data:

* **Pause must never touch billing.** Pausing is not cancelling.
  Conflating them would stop a subscription because someone wanted a
  quiet week. Asserted by patching Stripe and requiring zero calls.
* **Pause must actually stop the agent.** It is only real because the
  agent polls /api/voice/status/. A flag nothing reads would be a lie
  told to a paying client.
* **Cross-tenant scoping.** Client A must never see client B's calls or
  transcripts, which here include recorded conversations.
* **The recording disclosure survives a client edit.** They can rewrite
  their greeting; they must not be able to delete the line that keeps
  them lawful in two-party-consent states.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account, Website
from clients.service_models import MaintenancePlan
from onboarding.models import Onboarding
from voice.entitlement import program_for
from voice.models import VoiceCallRecord

User = get_user_model()

_seq = 0


def _client_account(*, entitled=False, comped=False, onboarded=False,
                    setup_status='', paused=False):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'vportal{_seq}', email=f'vportal{_seq}@example.com',
        password='test-pass-123')
    account = Account.objects.create(
        user=user, name=f'Voice Portal Co {_seq}',
        onboarding_status='complete',
        stripe_customer_id=f'cus_vp{_seq}',
        comp_voice_receptionist=comped)
    Website.objects.create(
        account=account, name=f'Site {_seq}', stage='live',
        onboarding_status='complete')
    if entitled:
        MaintenancePlan.objects.create(
            account=account, tier_slug='hvac-voice-receptionist',
            status='active', started_at=timezone.now(),
            stripe_subscription_id=f'sub_vp{_seq}')
    if onboarded:
        Onboarding.objects.create(
            user=user, product_type='voice_receptionist',
            tier_slug='hvac-voice-receptionist',
            completed_at=timezone.now())
    if setup_status or paused:
        program = program_for(account, create=True)
        if setup_status:
            program.setup_status = setup_status
        program.paused = paused
        program.greeting = 'Thanks for calling.'
        program.transfer_number = '(210) 555-0199'
        program.save()
    return user, account


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class HubDispatchTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()

    def test_no_access_gets_the_sales_page(self):
        user, _ = _client_account()
        self.client.force_login(user)
        response = self.client.get(reverse('voice:hub'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Never miss another after-hours call')

    def test_entitled_but_not_onboarded_gets_setup(self):
        user, _ = _client_account(entitled=True)
        self.client.force_login(user)
        response = self.client.get(reverse('voice:hub'))
        self.assertContains(response, 'Finish setting up your receptionist')

    def test_onboarded_but_no_number_gets_the_waiting_page(self):
        user, _ = _client_account(
            entitled=True, onboarded=True, setup_status='awaiting_number')
        self.client.force_login(user)
        response = self.client.get(reverse('voice:hub'))
        self.assertContains(response, 'building your receptionist')

    def test_live_redirects_to_manage(self):
        user, _ = _client_account(
            entitled=True, onboarded=True, setup_status='live')
        self.client.force_login(user)
        response = self.client.get(reverse('voice:hub'))
        self.assertRedirects(response, reverse('voice:manage'))

    def test_suspended_gets_the_suspended_page_with_no_resume(self):
        user, _ = _client_account(
            entitled=True, onboarded=True, setup_status='suspended')
        self.client.force_login(user)
        response = self.client.get(reverse('voice:hub'))
        self.assertContains(response, 'switched off')
        self.assertNotContains(response, reverse('voice:resume'))

    def test_comped_client_gets_the_same_setup_flow(self):
        user, _ = _client_account(comped=True)
        self.client.force_login(user)
        response = self.client.get(reverse('voice:hub'))
        self.assertContains(response, 'Finish setting up your receptionist')

    def test_anonymous_is_redirected(self):
        response = self.client.get(reverse('voice:hub'))
        self.assertEqual(response.status_code, 302)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ManagePageTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()
        self.user, self.account = _client_account(
            entitled=True, onboarded=True, setup_status='live')
        self.program = program_for(self.account)
        self.client.force_login(self.user)

    def _call(self, seconds=120, **kwargs):
        global _seq
        _seq += 1
        return VoiceCallRecord.objects.create(
            program=self.program, ghl_call_id=f'mp-{_seq}',
            started_at=timezone.now(), duration_seconds=seconds, **kwargs)

    def test_manage_renders(self):
        response = self.client.get(reverse('voice:manage'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Your AI receptionist')

    def test_minutes_gauge_shows_usage(self):
        self._call(seconds=600)
        from voice.usage import period_for, recompute
        recompute(period_for(self.program))
        response = self.client.get(reverse('voice:manage'))
        self.assertContains(response, 'of 300 minutes')

    def test_over_allowance_states_the_amount_owed(self):
        self._call(seconds=340 * 60)
        from voice.usage import period_for, recompute
        recompute(period_for(self.program))
        response = self.client.get(reverse('voice:manage'))
        self.assertContains(response, '$10.00')
        self.assertContains(response, 'still answering every call')

    def test_handled_without_you_is_the_headline_stat(self):
        """Call volume alone says nothing — the owner's own phone could
        have taken those. Calls it finished alone is the value metric."""
        self._call()
        self._call(transferred=True)
        response = self.client.get(reverse('voice:manage'))
        self.assertContains(response, 'handled without you')

    def test_trial_calls_are_excluded_from_the_client_view(self):
        self._call(trial_call=True, seconds=9000)
        response = self.client.get(reverse('voice:manage'))
        self.assertContains(response, '0 of 300 minutes')

    def test_screening_note_reflects_the_answer_mode(self):
        self.program.answer_mode = 'ring_you_first'
        self.program.save(update_fields=['answer_mode'])
        response = self.client.get(reverse('voice:manage'))
        self.assertContains(response, 'your phone rings first, so screening is off')

    def test_manage_bounces_when_not_live(self):
        self.program.setup_status = 'awaiting_number'
        self.program.save(update_fields=['setup_status'])
        response = self.client.get(reverse('voice:manage'))
        self.assertRedirects(response, reverse('voice:hub'))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class PauseTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()
        self.user, self.account = _client_account(
            entitled=True, onboarded=True, setup_status='live')
        self.program = program_for(self.account)
        self.client.force_login(self.user)

    def test_pause_never_touches_billing(self):
        """Pausing is not cancelling. Conflating them would stop a
        subscription because someone wanted a quiet week."""
        with patch('stripe.Subscription.modify') as modify, \
                patch('stripe.Subscription.delete') as delete:
            self.client.post(reverse('voice:pause'))
        modify.assert_not_called()
        delete.assert_not_called()
        self.assertTrue(MaintenancePlan.objects.filter(
            account=self.account, status='active').exists())

    def test_pause_actually_stops_the_agent_answering(self):
        """Only real because the agent polls the status endpoint. A flag
        nothing reads would be a lie told to a paying client."""
        self.client.post(reverse('voice:pause'))
        self.program.refresh_from_db()
        status = self.client.get(reverse(
            'voice_program_status', args=[self.program.public_key]))
        self.assertFalse(status.json()['answer'])
        self.assertTrue(status.json()['paused'])

    def test_resume_puts_it_back(self):
        self.client.post(reverse('voice:pause'))
        self.client.post(reverse('voice:resume'))
        status = self.client.get(reverse(
            'voice_program_status', args=[self.program.public_key]))
        self.assertTrue(status.json()['answer'])

    def test_pause_requires_post(self):
        self.assertEqual(
            self.client.get(reverse('voice:pause')).status_code, 405)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class SettingsTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()
        self.user, self.account = _client_account(
            entitled=True, onboarded=True, setup_status='live')
        self.program = program_for(self.account)
        self.client.force_login(self.user)

    def test_coverage_and_answer_mode_are_client_editable(self):
        self.client.post(reverse('voice:settings_save'), {
            'coverage_mode': 'all_hours',
            'answer_mode': 'ai_first',
            'after_transfer_fail': 'voicemail',
        })
        self.program.refresh_from_db()
        self.assertEqual(self.program.coverage_mode, 'all_hours')
        self.assertEqual(self.program.after_transfer_fail, 'voicemail')

    def test_a_change_flags_the_config_as_unsynced(self):
        """The operator owes GHL a push; without this flag nobody knows."""
        self.client.post(reverse('voice:settings_save'),
                         {'coverage_mode': 'all_hours'})
        self.program.refresh_from_db()
        self.assertFalse(self.program.config_synced_to_ghl)

    def test_switching_to_ring_first_warns_about_losing_screening(self):
        response = self.client.post(reverse('voice:settings_save'), {
            'answer_mode': 'ring_you_first'}, follow=True)
        body = response.content.decode('utf-8')
        self.assertIn('can no longer screen', body)

    def test_a_bad_transfer_number_is_rejected(self):
        self.client.post(reverse('voice:settings_save'),
                         {'transfer_number': '555'})
        self.program.refresh_from_db()
        self.assertNotEqual(self.program.transfer_number, '555')

    def test_garbage_choices_are_ignored_not_stored(self):
        self.client.post(reverse('voice:settings_save'),
                         {'coverage_mode': 'whenever', 'ring_seconds': '9999'})
        self.program.refresh_from_db()
        self.assertIn(self.program.coverage_mode,
                      dict(self.program.COVERAGE_CHOICES))
        self.assertLessEqual(self.program.ring_seconds, 60)

    def test_client_cannot_edit_the_ghl_credentials(self):
        """Those are ours. A POST naming them must be ignored."""
        self.client.post(reverse('voice:settings_save'), {
            'coverage_mode': 'all_hours',
            'ghl_location_id': 'attacker-location',
            'ghl_agent_id': 'attacker-agent',
            'answering_number': '(999) 999-9999',
        })
        self.program.refresh_from_db()
        self.assertNotEqual(self.program.ghl_location_id, 'attacker-location')
        self.assertNotEqual(self.program.ghl_agent_id, 'attacker-agent')
        self.assertNotEqual(self.program.answering_number, '(999) 999-9999')

    def test_client_cannot_reprice_themselves(self):
        self.client.post(reverse('voice:settings_save'), {
            'coverage_mode': 'all_hours', 'locked_price': '1.00'})
        self.program.refresh_from_db()
        self.assertNotEqual(str(self.program.locked_price), '1.00')

    def test_greeting_edit_keeps_the_recording_disclosure(self):
        """They can rewrite the greeting; they must not be able to delete
        the line that keeps them lawful where both parties must consent."""
        self.client.post(reverse('voice:knowledge_save'),
                         {'greeting': 'Johnson HVAC, how can I help?'})
        self.program.refresh_from_db()
        self.assertIn('may be recorded', self.program.greeting.lower())

    def test_disclosure_is_not_duplicated_on_re_save(self):
        for _ in range(3):
            self.client.post(reverse('voice:knowledge_save'),
                             {'greeting': 'Johnson HVAC. This call may be recorded.'})
        self.program.refresh_from_db()
        self.assertEqual(
            self.program.greeting.lower().count('may be recorded'), 1)

    def test_faqs_are_client_editable(self):
        self.client.post(reverse('voice:knowledge_save'),
                         {'faq_text': 'Do you charge for a quote? No.'})
        self.program.refresh_from_db()
        self.assertIn('charge for a quote', self.program.faq_text)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class TenantScopingTests(TestCase):
    """These pages carry recorded phone conversations. A leak here is
    worse than a leak of review stats."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()

    def test_one_client_cannot_see_anothers_calls(self):
        user_a, account_a = _client_account(
            entitled=True, onboarded=True, setup_status='live')
        _, account_b = _client_account(
            entitled=True, onboarded=True, setup_status='live')
        VoiceCallRecord.objects.create(
            program=program_for(account_b), ghl_call_id='secret-call',
            started_at=timezone.now(), duration_seconds=90,
            summary='Client B private call summary',
            transcript='Client B transcript')

        self.client.force_login(user_a)
        for url in (reverse('voice:manage'), reverse('voice:calls')):
            body = self.client.get(url).content.decode('utf-8')
            self.assertNotIn('Client B private call summary', body, url)
            self.assertNotIn('Client B transcript', body, url)

    def test_status_endpoint_needs_the_right_public_key(self):
        import uuid
        response = self.client.get(reverse(
            'voice_program_status', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class SignupTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()

    def test_signup_confirm_screen_states_the_terms(self):
        user, _ = _client_account()
        self.client.force_login(user)
        response = self.client.get(reverse('voice:signup'))
        self.assertContains(response, '300 talk minutes')
        self.assertContains(response, '25 cents a minute')

    def test_signup_charges_the_existing_customer_not_a_new_one(self):
        """A redirect to /billing/checkout/ would create a second Stripe
        customer and a second Django user for someone already logged in."""
        user, account = _client_account()
        self.client.force_login(user)
        with patch('billing.stripe_helpers.create_addon_subscription') as sub, \
                patch('stripe.Customer.create') as cust:
            self.client.post(reverse('voice:signup'))
        sub.assert_called_once()
        cust.assert_not_called()

    def test_already_entitled_client_is_not_sold_it_twice(self):
        user, _ = _client_account(entitled=True)
        self.client.force_login(user)
        with patch('billing.stripe_helpers.create_addon_subscription') as sub:
            response = self.client.post(reverse('voice:signup'))
        sub.assert_not_called()
        self.assertRedirects(response, reverse('voice:hub'))
