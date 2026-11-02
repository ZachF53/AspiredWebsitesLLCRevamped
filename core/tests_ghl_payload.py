"""
Reading GHL webhook bodies in both shapes, and the duration bug.

The duration tests are the ones that matter most. GHL's post-call
workflow sends ``{{voice_ai.duration}}`` as an undocumented STRING. The
previous parse was ``int(float(raw))``, so "1:35" raised, was caught,
and the call was metered as **zero seconds** -- no error, no log, and the
300-minute cap never tripping. If GHL sends m:ss, every call billed
nothing. That is a silent revenue loss, which is why every plausible
format is pinned here.
"""
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from core.ghl_payload import (
    RAW_LOG_LIMIT, as_bool, coerce_duration_seconds, first, is_stop_keyword,
    log_raw_body,
)


class DurationCoercionTests(SimpleTestCase):

    def test_plain_seconds(self):
        for raw, want in (('95', 95), (95, 95), ('95.4', 95), ('95.6', 96),
                          (95.4, 95)):
            got, ok = coerce_duration_seconds(raw)
            self.assertEqual((got, ok), (want, True), f'{raw!r}')

    def test_minutes_colon_seconds(self):
        """The format that used to bill zero."""
        for raw, want in (('1:35', 95), ('0:47', 47), ('10:00', 600),
                          ('0:00', 0)):
            got, ok = coerce_duration_seconds(raw)
            self.assertEqual((got, ok), (want, True), f'{raw!r}')

    def test_hours_colon_minutes_colon_seconds(self):
        got, ok = coerce_duration_seconds('1:02:03')
        self.assertEqual((got, ok), (3723, True))

    def test_iso_and_compact_forms(self):
        for raw, want in (('PT1M35S', 95), ('1m35s', 95), ('2h3m4s', 7384),
                          ('45s', 45), ('PT2M', 120)):
            got, ok = coerce_duration_seconds(raw)
            self.assertEqual((got, ok), (want, True), f'{raw!r}')

    def test_empty_is_zero_and_recognised(self):
        """Nothing sent is a legitimate zero, not a parse failure."""
        for raw in ('', None, '   '):
            self.assertEqual(coerce_duration_seconds(raw), (0, True))

    def test_junk_is_zero_but_flagged_unrecognised(self):
        """So the caller can log the raw value instead of billing 0 quietly."""
        for raw in ('about a minute', 'N/A', '1:75', '1:2:3:4', True):
            got, ok = coerce_duration_seconds(raw)
            self.assertEqual(got, 0, f'{raw!r}')
            self.assertFalse(ok, f'{raw!r} should be flagged unrecognised')

    def test_negative_never_returned(self):
        self.assertEqual(coerce_duration_seconds('-30')[0], 0)


class PayloadReadingTests(SimpleTestCase):
    """Custom raw-JSON body and standard Webhook action, same reader."""

    CUSTOM = {
        'locationId': 'loc-1', 'duration': '95', 'trialCall': True,
        'event_type': 'sent', 'phone': '+15550001111',
    }
    STANDARD = {
        'contact_id': 'c-1', 'full_name': 'Dale Gribble',
        'phone': '+15550001111', 'email': 'd@example.com',
        'location': {'id': 'loc-1'},
        'message': {'body': 'STOP'},
        'customData': {'event_type': 'reply', 'trialCall': 'false',
                       'duration': '1:35'},
    }

    def test_custom_body_flat_keys(self):
        self.assertEqual(first(self.CUSTOM, 'locationId', 'location.id'),
                         'loc-1')
        self.assertEqual(first(self.CUSTOM, 'duration'), '95')

    def test_standard_body_nested_location(self):
        self.assertEqual(first(self.STANDARD, 'locationId', 'location.id'),
                         'loc-1')

    def test_standard_body_nested_message_body(self):
        self.assertEqual(first(self.STANDARD, 'body', 'message.body'), 'STOP')

    def test_custom_data_wins_over_top_level(self):
        """Custom Data is the only place an operator can put a deliberate
        value on a standard action, so it must take precedence."""
        payload = {'event_type': 'sent',
                   'customData': {'event_type': 'review_left'}}
        self.assertEqual(first(payload, 'event_type'), 'review_left')

    def test_empty_string_counts_as_absent(self):
        """GHL renders an unmatched merge field as '', which must not
        overwrite a good value from a later alias."""
        payload = {'name': '', 'full_name': 'Real Name'}
        self.assertEqual(first(payload, 'name', 'full_name'), 'Real Name')

    def test_missing_everything_is_none(self):
        self.assertIsNone(first({}, 'nope', 'also.nope'))
        self.assertIsNone(first(None, 'nope'))

    def test_non_dict_custom_data_is_ignored(self):
        payload = {'event_type': 'sent', 'customData': 'not-a-dict'}
        self.assertEqual(first(payload, 'event_type'), 'sent')

    def test_a_container_never_shadows_a_scalar_alias(self):
        """Caught in review: the standard action nests `message`, so
        first(p, 'body', 'message', 'message.body') returned the whole
        {'body': 'STOP'} dict because 'message' came first and was
        non-empty. str() of that is not a stop keyword, so the opt-out
        went undetected. Alias order must not decide correctness."""
        payload = {'message': {'body': 'STOP'}}
        self.assertEqual(first(payload, 'body', 'message', 'message.body'),
                         'STOP')
        self.assertEqual(first(payload, 'message'), None)

    def test_lists_are_skipped_too(self):
        payload = {'duration': [], 'callDuration': '95'}
        self.assertEqual(first(payload, 'duration', 'callDuration'), '95')

    def test_as_bool_accepts_ghl_string_forms(self):
        for raw in (True, 'true', 'True', '1', 'yes', 'on'):
            self.assertTrue(as_bool(raw), f'{raw!r}')
        for raw in (False, 'false', '0', 'no', '', None, 'maybe'):
            self.assertFalse(as_bool(raw), f'{raw!r}')


class StopKeywordTests(SimpleTestCase):

    def test_bare_keywords_are_opt_outs(self):
        for raw in ('STOP', 'stop', ' Stop ', 'STOPALL', 'unsubscribe',
                    'CANCEL', 'end', 'quit', 'stop.', 'STOP!', 'opt out'):
            self.assertTrue(is_stop_keyword(raw), f'{raw!r}')

    def test_a_sentence_containing_stop_is_a_customer_talking(self):
        """The distinction that matters: whole-body match only."""
        for raw in ('stop by tomorrow with the quote',
                    'can you stop the service call',
                    'please cancel my 2pm appointment',
                    'end of the week works'):
            self.assertFalse(is_stop_keyword(raw), f'{raw!r}')

    def test_empty_is_not_an_opt_out(self):
        for raw in ('', None, '   '):
            self.assertFalse(is_stop_keyword(raw))


class RawBodyLoggingTests(TestCase):
    """The first N bodies get logged so the live shape can be confirmed
    once, then it goes quiet -- transcripts are customer content."""

    def setUp(self):
        cache.clear()

    def test_logs_up_to_the_limit_then_stops(self):
        with self.assertLogs('core.ghl_payload', level='INFO') as logs:
            for i in range(RAW_LOG_LIMIT + 5):
                log_raw_body('voice', f'{{"n":{i}}}'.encode())
        self.assertEqual(len(logs.output), RAW_LOG_LIMIT)

    def test_counters_are_per_endpoint(self):
        for i in range(RAW_LOG_LIMIT):
            log_raw_body('voice', b'{}')
        with self.assertLogs('core.ghl_payload', level='INFO') as logs:
            log_raw_body('reviews', b'{"still":"logging"}')
        self.assertEqual(len(logs.output), 1)

    def test_undecodable_bytes_do_not_raise(self):
        with self.assertLogs('core.ghl_payload', level='INFO'):
            log_raw_body('voice', b'\xff\xfe not utf8')
