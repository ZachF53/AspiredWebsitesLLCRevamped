"""
`ring_seconds` is a target we ask a carrier for, not a dial we turn.

Calls reach the agent by conditional call-forwarding from the client's
existing line, and the no-answer timer that decides when a call forwards
belongs to their CARRIER. Some let it be set, some fix it near 20-25
seconds, some will not expose it. We control it exactly only when the
number has been ported to us, because then no upstream carrier is in the
path.

The field reads like a setting, which is exactly why this file exists:
the honest framing is one careless copy edit away from becoming a
promise we cannot keep, and the failure is invisible — the client picks
15 seconds, their carrier does 25, and nobody notices until somebody
counts rings during a complaint.

These tests pin the caveat at every surface a client could form the
expectation from: the public FAQ, the intake question, the portal
setting, and the waiting-page instruction.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account, Website
from clients.service_models import MaintenancePlan
from onboarding.models import Onboarding
from onboarding.registry import ONBOARDING_QUESTIONS
from voice.entitlement import program_for

User = get_user_model()

_seq = 0


def _client(answer_mode='ring_you_first', setup_status='awaiting_number',
            number_strategy='forward_existing'):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'vring{_seq}', email=f'vring{_seq}@example.com',
        password='test-pass-123')
    account = Account.objects.create(
        user=user, name=f'Ring Co {_seq}', onboarding_status='complete',
        comp_voice_receptionist=True)
    Website.objects.create(account=account, name=f'Site {_seq}',
                           stage='live', onboarding_status='complete')
    Onboarding.objects.create(
        user=user, product_type='voice_receptionist',
        tier_slug='hvac-voice-receptionist', completed_at=timezone.now())
    program = program_for(account, create=True)
    program.answer_mode = answer_mode
    program.setup_status = setup_status
    program.number_strategy = number_strategy
    program.ring_seconds = 15
    program.business_phone = '(210) 555-0142'
    program.transfer_number = '(210) 555-0199'
    program.answering_number = '(210) 555-0100'
    program.greeting = 'Thanks for calling.'
    program.save()
    return user, account, program


class IntakeIsHonestTests(TestCase):

    def _q(self, key):
        for section in ONBOARDING_QUESTIONS['voice_receptionist']:
            for q in section['questions']:
                if q['key'] == key:
                    return q
        raise AssertionError(f'{key} missing from the wizard')

    def test_ring_seconds_help_does_not_promise_exact_control(self):
        help_text = self._q('ring_seconds')['help'].lower()
        self.assertIn('target', help_text, (
            'the ring timer is set by the carrier, not by us — the help '
            'text must not read as a guarantee'))
        self.assertIn('carrier', help_text)

    def test_ring_seconds_help_names_porting_as_the_exact_option(self):
        self.assertIn('over to us', self._q('ring_seconds')['help'].lower())

    def test_forwarding_option_admits_the_setup_step(self):
        """"Works with the number you already have" is true, but it
        costs the client one call to their phone company. Saying so at
        intake is cheaper than a confused client later."""
        self.assertIn('phone company',
                      self._q('number_strategy')['help'].lower())


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class PublicCopyIsHonestTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()

    def test_service_page_explains_who_owns_the_ring_timer(self):
        body = self.client.get(
            '/services/ai-receptionist/').content.decode('utf-8').lower()
        self.assertIn('belongs to your phone company', body, (
            'a buyer reading "ring me first, 15 seconds" must be told '
            'that number is a request to their carrier'))

    def test_service_page_admits_the_forwarding_setup_step(self):
        body = self.client.get(
            '/services/ai-receptionist/').content.decode('utf-8').lower()
        self.assertIn('one setup step', body)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class PortalCopyIsHonestTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()

    def test_waiting_page_gives_an_instruction_not_a_gesture(self):
        """"Point your number at it" was unactionable, and hid the fact
        that the client has to call their carrier at all."""
        user, _, _ = _client()
        self.client.force_login(user)
        body = self.client.get(
            reverse('voice:hub')).content.decode('utf-8').lower()
        self.assertIn('call your carrier', body)
        self.assertIn('(210) 555-0100', body)

    def test_ring_first_asks_for_conditional_forwarding(self):
        user, _, _ = _client(answer_mode='ring_you_first')
        self.client.force_login(user)
        body = self.client.get(
            reverse('voice:hub')).content.decode('utf-8').lower()
        self.assertIn('conditional forwarding', body)
        self.assertIn("when i don't answer", body.replace('&rsquo;', "'"))

    def test_ai_first_warns_the_handset_stops_ringing(self):
        """Unconditional forwarding means their own phone goes quiet for
        business calls. An owner who is not told that thinks it broke."""
        user, _, _ = _client(answer_mode='ai_first')
        self.client.force_login(user)
        body = self.client.get(
            reverse('voice:hub')).content.decode('utf-8').lower()
        self.assertIn('unconditional forwarding', body)
        self.assertIn('stop ringing', body)

    def test_manage_page_caveats_the_ring_target(self):
        user, _, program = _client(
            answer_mode='ring_you_first', setup_status='live')
        self.client.force_login(user)
        body = self.client.get(
            reverse('voice:manage')).content.decode('utf-8').lower()
        self.assertIn('target, not a dial', body)

    def test_a_ported_number_is_described_as_exact(self):
        """Porting is the one case where we really do control it, and
        the copy should stop hedging."""
        user, _, _ = _client(answer_mode='ring_you_first',
                             setup_status='live',
                             number_strategy='port_existing')
        self.client.force_login(user)
        body = self.client.get(
            reverse('voice:manage')).content.decode('utf-8').lower()
        self.assertIn('this is exact', body)
        self.assertNotIn('target, not a dial', body)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class OperatorCheatSheetTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        cache.clear()
        global _seq
        _seq += 1
        self.staff = User.objects.create_user(
            username=f'vringadm{_seq}', email=f'vringadm{_seq}@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)

    def test_detail_page_carries_the_carrier_codes(self):
        """The client gets a plain instruction; the operator needs the
        codes to read out when the client would rather self-serve."""
        _, _, program = _client()
        self.client.force_login(self.staff)
        body = self.client.get(reverse(
            'admin_dashboard:voice_program_detail',
            kwargs={'program_id': program.id})).content.decode('utf-8')
        self.assertIn('*61*NUMBER#', body)      # GSM, on no answer
        self.assertIn('*21*NUMBER#', body)      # GSM, all calls
        self.assertIn('*72', body)              # Verizon, all calls
        self.assertIn('Forwarding cheat-sheet', body)

    def test_detail_page_states_which_forwarding_this_client_needs(self):
        _, _, program = _client(answer_mode='ai_first')
        self.client.force_login(self.staff)
        body = self.client.get(reverse(
            'admin_dashboard:voice_program_detail',
            kwargs={'program_id': program.id})).content.decode('utf-8')
        self.assertIn('unconditional', body)
