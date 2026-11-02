"""
The voice webhook against what GHL's post-call workflow actually sends.

GHL exposes no "Voice AI call ended" trigger. Post-call workflows attach
per agent and expose only four merge fields -- duration, summary,
transcript, formatted_transcript. There is no call id, no agent id, no
start time and no executedCallActions, so the body in GHL_HANDOFF.md
synthesises an id from contact id + a `right_now` timestamp.

Two consequences this file pins:

1. The id is SYNTHETIC. GHL re-renders merge fields on retry, so a
   redelivery arrives with a different id, sails past the primary
   dedupe, and bills the same call twice. Hence the secondary match.

2. There is no start time, so the webhook lands at the call's END.
   started_at is reconstructed as (now - duration) rather than stamped
   at receipt, because after-hours coverage is computed from it.
"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from voice.models import VoiceCallRecord

SECRET = 'test-voice-secret'


def _url():
    return reverse('voice_ghl_events', args=[SECRET])


def _body(**over):
    """The real demo-agent shape from GHL_HANDOFF.md."""
    payload = {
        'id': 'ghlwf-contact123-2026112073015',
        'locationId': 'elhJFAz0ZPa2EmAqthti',
        'duration': '95',
        'trialCall': True,
        'agentId': '6ae8880de13405ba691430c8',
        'agentName': 'Demo - Sample Trades Receptionist',
        'toNumber': '+12107961342',
        'fromNumber': '+15125551234',
        'contactId': 'contact123',
        'source': 'ghl_workflow_post_call',
    }
    payload.update(over)
    return payload


@override_settings(VOICE_GHL_EVENTS_SECRET=SECRET,
                   ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class SyntheticIdDedupeTests(TestCase):

    def _post(self, payload):
        return self.client.post(_url(), data=payload,
                                content_type='application/json')

    def test_a_call_is_stored(self):
        resp = self._post(_body())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(VoiceCallRecord.objects.count(), 1)
        call = VoiceCallRecord.objects.get()
        self.assertEqual(call.duration_seconds, 95)
        self.assertTrue(call.trial_call)

    def test_same_id_is_the_primary_dedupe(self):
        self._post(_body())
        resp = self._post(_body())
        self.assertEqual(resp.json()['status'], 'duplicate')
        self.assertEqual(VoiceCallRecord.objects.count(), 1)

    def test_retry_with_a_regenerated_id_is_still_caught(self):
        """THE double-bill case. Same call, new synthetic id."""
        self._post(_body())
        resp = self._post(_body(id='ghlwf-contact123-2026112073017'))
        self.assertEqual(resp.json()['status'], 'duplicate')
        self.assertEqual(resp.json().get('matched'), 'secondary')
        self.assertEqual(VoiceCallRecord.objects.count(), 1)

    def test_a_different_caller_is_not_a_duplicate(self):
        self._post(_body())
        self._post(_body(id='other', fromNumber='+15125559999'))
        self.assertEqual(VoiceCallRecord.objects.count(), 2)

    def test_a_different_duration_is_not_a_duplicate(self):
        self._post(_body())
        self._post(_body(id='other', duration='96'))
        self.assertEqual(VoiceCallRecord.objects.count(), 2)

    def test_a_different_agent_is_not_a_duplicate(self):
        """Demo line and Hank share a location; the agent separates them."""
        self._post(_body())
        self._post(_body(id='other', agentId='6ae47a367d63c81953acac20'))
        self.assertEqual(VoiceCallRecord.objects.count(), 2)

    def test_the_same_caller_outside_the_window_is_a_real_second_call(self):
        self._post(_body())
        old = VoiceCallRecord.objects.get()
        old.started_at = timezone.now() - timedelta(minutes=30)
        old.save(update_fields=['started_at'])
        self._post(_body(id='later'))
        self.assertEqual(VoiceCallRecord.objects.count(), 2)

    def test_a_zero_duration_call_skips_the_secondary_match(self):
        """Two separate 0-second calls are plausible (both hung up), so
        duration-based matching must not collapse them."""
        self._post(_body(id='a', duration='0'))
        self._post(_body(id='b', duration='0'))
        self.assertEqual(VoiceCallRecord.objects.count(), 2)


@override_settings(VOICE_GHL_EVENTS_SECRET=SECRET,
                   ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class MissingStartTimeTests(TestCase):

    def _post(self, payload):
        return self.client.post(_url(), data=payload,
                                content_type='application/json')

    def test_started_at_is_reconstructed_from_duration(self):
        """The webhook arrives at the call's END. A 95-second call that
        just ended started ~95 seconds ago, not now -- and after-hours
        coverage is computed from started_at."""
        before = timezone.now()
        self._post(_body(duration='95'))
        call = VoiceCallRecord.objects.get()
        delta = (before - call.started_at).total_seconds()
        self.assertGreater(delta, 90)
        self.assertLess(delta, 105)

    def test_an_explicit_created_at_still_wins(self):
        when = timezone.now() - timedelta(hours=3)
        self._post(_body(createdAt=when.isoformat()))
        call = VoiceCallRecord.objects.get()
        self.assertAlmostEqual(
            (call.started_at - when).total_seconds(), 0, delta=2)

    def test_unknown_duration_falls_back_to_now(self):
        self._post(_body(duration=''))
        call = VoiceCallRecord.objects.get()
        self.assertLess(
            abs((timezone.now() - call.started_at).total_seconds()), 10)


@override_settings(VOICE_GHL_EVENTS_SECRET=SECRET,
                   ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class DurationAndShapeTests(TestCase):

    def _post(self, payload):
        return self.client.post(_url(), data=payload,
                                content_type='application/json')

    def test_m_ss_duration_is_not_metered_as_zero(self):
        """The silent revenue bug."""
        self._post(_body(duration='1:35'))
        self.assertEqual(VoiceCallRecord.objects.get().duration_seconds, 95)

    def test_unparseable_duration_is_logged_loudly(self):
        with self.assertLogs('voice.ghl_webhook', level='ERROR') as logs:
            self._post(_body(duration='about a minute'))
        self.assertIn('about a minute', logs.output[0])
        self.assertEqual(VoiceCallRecord.objects.get().duration_seconds, 0)

    def test_standard_webhook_action_shape_is_accepted(self):
        """Nested location, values under customData."""
        self._post({
            'contact_id': 'contact123',
            'full_name': 'Dale Gribble',
            'phone': '+15125551234',
            'location': {'id': 'elhJFAz0ZPa2EmAqthti'},
            'customData': {
                'id': 'std-1', 'duration': '1:35', 'trialCall': 'true',
                'agentId': 'agent-x', 'fromNumber': '+15125551234',
            },
        })
        call = VoiceCallRecord.objects.get()
        self.assertEqual(call.ghl_call_id, 'std-1')
        self.assertEqual(call.duration_seconds, 95)
        self.assertEqual(call.ghl_location_id, 'elhJFAz0ZPa2EmAqthti')
        self.assertTrue(call.trial_call)

    def test_no_id_is_refused_rather_than_risked(self):
        resp = self._post(_body(id=''))
        self.assertEqual(resp.json()['status'], 'ignored')
        self.assertEqual(VoiceCallRecord.objects.count(), 0)

    def test_wrong_secret_writes_nothing(self):
        resp = self.client.post(reverse('voice_ghl_events', args=['nope']),
                                data=_body(), content_type='application/json')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(VoiceCallRecord.objects.count(), 0)

    @override_settings(VOICE_GHL_EVENTS_SECRET='')
    def test_unset_secret_fails_closed(self):
        resp = self._post(_body())
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(VoiceCallRecord.objects.count(), 0)
