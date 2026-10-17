"""
Minutes and money. Every assertion here is a billing dispute prevented.

The four failure modes this file exists to catch:

1. **Double-billing.** GHL delivers at least once. A redelivered call
   that accrues twice bills the client for minutes they never used.
2. **Billing our own test calls.** The operator makes trial calls while
   building an agent. ``trialCall`` must never reach the invoice.
3. **Per-call rounding.** Rounding each call up instead of the period
   adds roughly half a minute per call — systematically in our favour
   and indefensible in a dispute.
4. **Warning spam.** A busy afternoon runs the threshold check a dozen
   times; the client must get one email, not twelve.
"""

import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from clients.account_models import Account
from voice.entitlement import program_for
from voice.models import VoiceCallRecord, VoiceUsagePeriod
from voice import usage as usage_mod

User = get_user_model()

SECRET = 'test-voice-webhook-secret'
URL = f'/api/voice/ghl/events/{SECRET}/'

_seq = 0


def _account(**kwargs):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'vmet{_seq}', email=f'vmet{_seq}@example.com',
        password='test-pass-123')
    defaults = dict(user=user, name=f'Met Co {_seq}',
                    contact_name='Dana Reyes',
                    onboarding_status='complete')
    defaults.update(kwargs)
    return Account.objects.create(**defaults)


def _program(**kwargs):
    program = program_for(_account(), create=True)
    program.ghl_location_id = f'loc-{program.pk.hex[:10]}'
    program.setup_status = 'live'
    program.support_email = 'owner@example.com'
    for key, value in kwargs.items():
        setattr(program, key, value)
    program.save()
    return program


def _payload(program, *, call_id, duration, trial=False, actions=None,
             started=None):
    return {
        'id': call_id,
        'locationId': program.ghl_location_id,
        'agentId': 'agent-1',
        'fromNumber': '+12102010375',
        'createdAt': (started or timezone.now()).isoformat(),
        'duration': duration,
        'trialCall': trial,
        'summary': 'Caller asked about a no-heat call.',
        'transcript': 'Hello...',
        'extractedData': {'name': 'Dana'},
        'executedCallActions': actions or [],
    }


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
                   VOICE_GHL_EVENTS_SECRET=SECRET)
class WebhookAuthTests(TestCase):

    def test_wrong_secret_is_forbidden(self):
        response = Client().post(
            '/api/voice/ghl/events/nope/', data='{}',
            content_type='application/json')
        self.assertEqual(response.status_code, 403)

    @override_settings(VOICE_GHL_EVENTS_SECRET='')
    def test_unset_secret_fails_closed(self):
        """An unset secret must not mean 'accept anything' — here that
        would mean accepting fabricated billable minutes."""
        response = Client().post(
            URL, data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_payload_with_no_call_id_is_refused(self):
        """Without an id we cannot dedupe, so at-least-once delivery
        would double-bill. Refuse rather than risk it."""
        program = _program()
        body = _payload(program, call_id='x', duration=60)
        body.pop('id')
        response = Client().post(URL, data=json.dumps(body),
                                 content_type='application/json')
        self.assertEqual(response.json()['status'], 'ignored')
        self.assertEqual(VoiceCallRecord.objects.count(), 0)

    def test_malformed_json_does_not_500(self):
        response = Client().post(URL, data='{not json',
                                 content_type='application/json')
        self.assertEqual(response.status_code, 400)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
                   VOICE_GHL_EVENTS_SECRET=SECRET)
class AccrualTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def _post(self, body):
        return Client().post(URL, data=json.dumps(body),
                             content_type='application/json')

    def test_a_call_accrues_billable_seconds(self):
        program = _program()
        self._post(_payload(program, call_id='c1', duration=180))
        period = VoiceUsagePeriod.objects.get(program=program)
        self.assertEqual(period.billable_seconds, 180)
        self.assertEqual(period.minutes_used, 3)

    def test_redelivery_does_not_double_count(self):
        program = _program()
        body = _payload(program, call_id='dup-1', duration=600)
        self.assertEqual(self._post(body).json()['status'], 'ok')
        self.assertEqual(self._post(body).json()['status'], 'duplicate')
        self.assertEqual(VoiceCallRecord.objects.count(), 1)
        period = VoiceUsagePeriod.objects.get(program=program)
        self.assertEqual(period.billable_seconds, 600, (
            'a redelivered call was counted twice — the client is being '
            'billed for minutes they did not use'))

    def test_trial_calls_are_stored_but_never_billed(self):
        program = _program()
        self._post(_payload(program, call_id='t1', duration=900, trial=True))
        self.assertEqual(VoiceCallRecord.objects.count(), 1)
        period = VoiceUsagePeriod.objects.filter(program=program).first()
        self.assertTrue(period is None or period.billable_seconds == 0, (
            'an operator test call was billed to the client'))

    def test_transfer_is_detected_from_executed_actions(self):
        """There is no outcome field on the payload — CALL_TRANSFER in
        executedCallActions is the only evidence a human took over."""
        program = _program()
        self._post(_payload(
            program, call_id='x1', duration=120,
            actions=[{'actionType': 'CALL_TRANSFER', 'name': 'to owner'}]))
        self.assertTrue(VoiceCallRecord.objects.get().transferred)

    def test_booking_is_detected(self):
        program = _program()
        self._post(_payload(
            program, call_id='b1', duration=200,
            actions=[{'type': 'APPOINTMENT_BOOKING'}]))
        self.assertTrue(VoiceCallRecord.objects.get().booked_appointment)

    def test_unknown_location_is_stored_unattributed(self):
        """Still a 200: retrying will not teach us the location id, and
        the operator needs to see the call to fix the config."""
        program = _program()
        body = _payload(program, call_id='u1', duration=60)
        body['locationId'] = 'not-a-known-location'
        response = self._post(body)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['attributed'])
        call = VoiceCallRecord.objects.get()
        self.assertIsNone(call.program_id)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class RoundingTests(TestCase):
    """Rounding happens once per period, never per call."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def _period_with(self, seconds_list):
        program = _program()
        now = timezone.now()
        for index, seconds in enumerate(seconds_list):
            VoiceCallRecord.objects.create(
                program=program, ghl_call_id=f'r{program.pk.hex[:6]}-{index}',
                started_at=now, duration_seconds=seconds)
        period = usage_mod.period_for(program, now)
        return usage_mod.recompute(period)

    def test_forty_short_calls_do_not_inflate_by_rounding(self):
        """40 calls of 30s is exactly 20 minutes. Rounding each call up
        would report 40 — double, and all of it in our favour."""
        period = self._period_with([30] * 40)
        self.assertEqual(period.billable_seconds, 1200)
        self.assertEqual(period.minutes_used, 20)

    def test_partial_minute_rounds_up_once_at_the_period_level(self):
        period = self._period_with([61])
        self.assertEqual(period.minutes_used, 2)

    def test_overage_is_minutes_beyond_the_allowance(self):
        period = self._period_with([301 * 60])
        self.assertEqual(period.included_minutes, 300)
        self.assertEqual(period.overage_minutes, 1)
        self.assertEqual(period.overage_amount, Decimal('0.25'))

    def test_exactly_at_the_allowance_owes_nothing(self):
        period = self._period_with([300 * 60])
        self.assertEqual(period.overage_minutes, 0)
        self.assertEqual(period.overage_amount, Decimal('0.00'))
        self.assertFalse(period.is_over)

    def test_forty_minutes_over_costs_ten_dollars(self):
        period = self._period_with([340 * 60])
        self.assertEqual(period.overage_minutes, 40)
        self.assertEqual(period.overage_amount, Decimal('10.00'))

    def test_recompute_is_a_full_recount_not_an_increment(self):
        """The nightly sweep inserts calls the webhook missed, so the
        recount has to pick up rows added after the fact."""
        period = self._period_with([60])
        self.assertEqual(period.minutes_used, 1)
        VoiceCallRecord.objects.create(
            program=period.program, ghl_call_id='late-1',
            started_at=period.period_start + timezone.timedelta(hours=1),
            duration_seconds=120)
        usage_mod.recompute(period)
        self.assertEqual(period.minutes_used, 3)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ThresholdWarningTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        mail.outbox = []

    def _period(self, minutes):
        program = _program()
        now = timezone.now()
        VoiceCallRecord.objects.create(
            program=program, ghl_call_id=f'w{program.pk.hex[:8]}',
            started_at=now, duration_seconds=minutes * 60)
        period = usage_mod.period_for(program, now)
        return usage_mod.recompute(period)

    def test_eighty_percent_warns_once(self):
        period = self._period(240)
        usage_mod.check_thresholds(period)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('80%', mail.outbox[0].subject)

        usage_mod.check_thresholds(period)
        usage_mod.check_thresholds(period)
        self.assertEqual(len(mail.outbox), 1, (
            'the 80% warning fired more than once — a busy afternoon '
            'would send the client a dozen emails'))

    def test_hundred_percent_warns_once_and_states_the_rate(self):
        period = self._period(305)
        usage_mod.check_thresholds(period)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn('25 cents', body)
        self.assertIn('Nothing stops', body, (
            'the over-allowance email must say the receptionist keeps '
            'answering — the owner chose bill-through over a hard cap'))

    def test_below_eighty_percent_is_silent(self):
        period = self._period(100)
        usage_mod.check_thresholds(period)
        self.assertEqual(len(mail.outbox), 0)

    def test_crossing_both_thresholds_at_once_sends_only_the_higher(self):
        """A single long call can jump 0% to 130%. Sending both emails
        in the same second would read as broken."""
        period = self._period(400)
        usage_mod.check_thresholds(period)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('used all', mail.outbox[0].subject)

    def test_a_mail_failure_does_not_re_fire_the_warning(self):
        """The timestamp is stamped before sending on purpose: a broken
        mail backend must not mean an email per subsequent call."""
        period = self._period(250)
        with override_settings(
                EMAIL_BACKEND='django.core.mail.backends.dummy.EmailBackend'):
            usage_mod.check_thresholds(period)
        period.refresh_from_db()
        self.assertIsNotNone(period.warned_80_at)
        usage_mod.check_thresholds(period)
        self.assertEqual(len(mail.outbox), 0)
