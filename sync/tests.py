"""
Phase 3.3 — sync HMAC + handoff-token tests.

Covers:
  - sync_inbound: bad signature → 403, stale timestamp → 403, valid
    HMAC+ts passes through to payload validation
  - generate_handoff_token / validate_handoff_token: round-trip,
    expired rejected, tampered (sig + payload) rejected
"""

import base64
import hashlib
import hmac
import json
import time

from django.test import TestCase, override_settings
from django.urls import reverse

from sync.token_utils import (
    TOKEN_TTL_SECONDS,
    generate_handoff_token,
    validate_handoff_token,
)


TEST_SECRET = 'test-sync-secret-do-not-use-in-prod'


def _sign(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@override_settings(MOONIEFUL_SYNC_SECRET=TEST_SECRET)
class SyncInboundSignatureTests(TestCase):
    """The HMAC signature path on /api/sync/inbound/."""

    def _post(self, body, *, sig, ts):
        return self.client.post(
            reverse('sync:inbound'),
            data=body, content_type='application/json',
            HTTP_X_SYNC_SIGNATURE=sig,
            HTTP_X_SYNC_TIMESTAMP=str(ts),
        )

    def test_bad_signature_returns_403(self):
        body = b'{}'
        r = self._post(body, sig='deadbeef', ts=int(time.time()))
        self.assertEqual(r.status_code, 403)
        self.assertIn(b'invalid signature', r.content)

    def test_stale_timestamp_returns_403(self):
        body = b'{}'
        stale_ts = int(time.time()) - 3600  # 1 hour old
        sig = _sign(TEST_SECRET, body)
        r = self._post(body, sig=sig, ts=stale_ts)
        self.assertEqual(r.status_code, 403)
        self.assertIn(b'stale', r.content)

    def test_missing_timestamp_returns_403(self):
        body = b'{}'
        sig = _sign(TEST_SECRET, body)
        r = self.client.post(
            reverse('sync:inbound'),
            data=body, content_type='application/json',
            HTTP_X_SYNC_SIGNATURE=sig,
            # No HTTP_X_SYNC_TIMESTAMP header — should be rejected.
        )
        self.assertEqual(r.status_code, 403)

    def test_fresh_signature_passes_to_payload_validation(self):
        """Valid HMAC + fresh timestamp → signature check passes.
        With an empty JSON body the schema_version check rejects at 400,
        which proves the signature path itself accepted the request."""
        body = b'{}'
        sig = _sign(TEST_SECRET, body)
        r = self._post(body, sig=sig, ts=int(time.time()))
        self.assertEqual(r.status_code, 400)
        self.assertIn(b'schema_version', r.content)

    def test_unknown_event_type_returns_400(self):
        body = json.dumps({
            'schema_version': 1,
            'event_type': 'fake_event_does_not_exist',
        }).encode()
        sig = _sign(TEST_SECRET, body)
        r = self._post(body, sig=sig, ts=int(time.time()))
        self.assertEqual(r.status_code, 400)


@override_settings(MOONIEFUL_SYNC_SECRET=TEST_SECRET)
class HandoffTokenTests(TestCase):
    """Round-trip + expiry + tamper-resistance for the maintenance
    handoff link signed token."""

    def test_generate_and_validate_roundtrip(self):
        client_id = 'abc-123'
        token = generate_handoff_token(client_id)
        self.assertEqual(validate_handoff_token(token), client_id)

    def test_expired_token_rejected(self):
        """Token whose expiry is in the past returns None."""
        expired_payload = f'expired-client:{int(time.time()) - 1}'
        sig = hmac.new(
            TEST_SECRET.encode(), expired_payload.encode(),
            hashlib.sha256).hexdigest()
        token = base64.urlsafe_b64encode(
            f'{expired_payload}:{sig}'.encode()).decode()
        self.assertIsNone(validate_handoff_token(token))

    def test_tampered_signature_rejected(self):
        """Same payload, modified signature."""
        client_id = 'tampered-client'
        token = generate_handoff_token(client_id)
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = decoded.rsplit(':', 1)
        bad_sig = sig[:-1] + ('0' if sig[-1] != '0' else '1')
        bad_token = base64.urlsafe_b64encode(
            f'{payload}:{bad_sig}'.encode()).decode()
        self.assertIsNone(validate_handoff_token(bad_token))

    def test_tampered_payload_rejected(self):
        """Client id swapped after signing → validation fails."""
        token = generate_handoff_token('original-id')
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        client_id, expiry, sig = decoded.rsplit(':', 2)
        bad_payload = f'swapped-id:{expiry}:{sig}'
        bad_token = base64.urlsafe_b64encode(bad_payload.encode()).decode()
        self.assertIsNone(validate_handoff_token(bad_token))

    def test_garbage_token_rejected(self):
        self.assertIsNone(validate_handoff_token('not-a-real-token'))
        self.assertIsNone(validate_handoff_token(''))

    def test_token_ttl_constant(self):
        """48 hours per CLAUDE.md handoff flow spec."""
        self.assertEqual(TOKEN_TTL_SECONDS, 48 * 3600)
