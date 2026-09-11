"""
Phase 3.3 — sync HMAC + handoff-token tests.

Covers:
  - sync_inbound: bad signature → 403, stale timestamp → 403, valid
    HMAC+ts passes through to payload validation, event_id dedup
  - sync_file: raw-body (non-multipart) upload, HMAC over url+size
  - the outbound stage_changed / maintenance_activated envelope shape
  - generate_handoff_token / validate_handoff_token: round-trip,
    expired rejected, tampered (sig + payload) rejected

HMAC scheme is a locked cross-repo contract with Moonieful — see
docs/sync_contract.md and sync/security.py. Signing just the raw body
(no version/timestamp binding) is the exact bug this bridge shipped with
for months; see test_old_body_only_signature_is_rejected below.
"""

import base64
import hashlib
import hmac
import json
import time

from django.test import TestCase, override_settings
from django.urls import reverse

from sync.security import sign
from sync.token_utils import (
    TOKEN_TTL_SECONDS,
    generate_handoff_token,
    validate_handoff_token,
)


TEST_SECRET = 'test-sync-secret-do-not-use-in-prod'


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
        sig = sign(stale_ts, body)
        r = self._post(body, sig=sig, ts=stale_ts)
        self.assertEqual(r.status_code, 403)

    def test_missing_timestamp_returns_403(self):
        body = b'{}'
        sig = sign(int(time.time()), body)
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
        ts = int(time.time())
        sig = sign(ts, body)
        r = self._post(body, sig=sig, ts=ts)
        self.assertEqual(r.status_code, 400)
        self.assertIn(b'schema_version', r.content)

    def test_unknown_event_type_returns_400(self):
        body = json.dumps({
            'schema_version': 1,
            'event_type': 'fake_event_does_not_exist',
        }).encode()
        ts = int(time.time())
        sig = sign(ts, body)
        r = self._post(body, sig=sig, ts=ts)
        self.assertEqual(r.status_code, 400)

    def test_old_body_only_signature_is_rejected(self):
        """Regression test for the exact contract bug that shipped for
        months: this endpoint used to verify HMAC over the raw body alone,
        with no version prefix and no timestamp binding — which could
        never match what Moonieful actually signs, AND independently left
        a replay hole (a captured body+signature pair could be replayed
        forever by attaching a fresh timestamp, since the timestamp
        wasn't part of what was signed)."""
        body = b'{}'
        ts = int(time.time())
        old_style_sig = hmac.new(
            TEST_SECRET.encode(), body, hashlib.sha256).hexdigest()
        r = self._post(body, sig=old_style_sig, ts=ts)
        self.assertEqual(r.status_code, 403)

    def test_duplicate_event_id_is_acknowledged_not_reapplied(self):
        """A retried delivery (e.g. a response lost to a network blip)
        must be acknowledged without re-running the handler — otherwise
        project_complete resends the handoff email and duplicates the
        stage-log row on every retry of an already-successful delivery."""
        from django.core import mail

        from clients.account_models import Account, Website

        User = __import__('django.contrib.auth', fromlist=['get_user_model']).get_user_model()
        user = User.objects.create_user(
            username='dupe', email='dupe@example.com', password='x')
        account = Account.objects.create(
            user=user, name='Dupe Co',
            moonieful_client_id='44444444-4444-4444-4444-444444444444',
            synced_from_moonieful=True)
        Website.objects.create(
            account=account, name='Dupe Site', stage='review',
            moonieful_referred=True)

        body = json.dumps({
            'schema_version': 1,
            'event_type': 'project_complete',
            'event_id': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
            'client': {'moonieful_client_id': str(account.moonieful_client_id)},
        }).encode()
        ts = int(time.time())
        sig = sign(ts, body)

        mail.outbox = []
        r1 = self._post(body, sig=sig, ts=ts)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)

        r2 = self._post(body, sig=sig, ts=ts)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(json.loads(r2.content)['duplicate'])
        # Handler did not run again — still exactly one handoff email.
        self.assertEqual(len(mail.outbox), 1)


@override_settings(MOONIEFUL_SYNC_SECRET=TEST_SECRET)
class SyncFileTests(TestCase):
    """/api/sync/file/<id>/ — Moonieful streams the raw file body
    (application/octet-stream), never multipart form data."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        from clients.account_models import Account, Website
        from clients.models import ClientDocument

        User = get_user_model()
        user = User.objects.create_user(
            username='fileowner', email='file@example.com', password='x')
        account = Account.objects.create(user=user, name='File Co')
        site = Website.objects.create(account=account, name='File Site')
        self.document = ClientDocument.objects.create(
            website_new=site, direction='to_client', label='Brand guide',
            moonieful_document_id='55555555-5555-5555-5555-555555555555')

    def test_raw_body_upload_is_saved(self):
        body = b'%PDF-1.4 fake pdf bytes'
        url = reverse('sync:file', args=[self.document.moonieful_document_id])
        ts = int(time.time())
        # Signed content is f'{url}\n{size}' per docs/sync_contract.md, not
        # the file bytes — the test client's request path is the URL used.
        signed_over = f'http://testserver{url}\n{len(body)}'.encode()
        sig = sign(ts, signed_over)

        r = self.client.post(
            url, data=body, content_type='application/octet-stream',
            HTTP_X_SYNC_SIGNATURE=sig, HTTP_X_SYNC_TIMESTAMP=str(ts),
            HTTP_X_SYNC_FILENAME='brand-guide.pdf',
        )
        self.assertEqual(r.status_code, 200)
        self.document.refresh_from_db()
        self.assertTrue(self.document.file.name.endswith('.pdf'))

    def test_disallowed_extension_rejected(self):
        body = b'#!/bin/sh\necho pwned'
        url = reverse('sync:file', args=[self.document.moonieful_document_id])
        ts = int(time.time())
        signed_over = f'http://testserver{url}\n{len(body)}'.encode()
        sig = sign(ts, signed_over)

        r = self.client.post(
            url, data=body, content_type='application/octet-stream',
            HTTP_X_SYNC_SIGNATURE=sig, HTTP_X_SYNC_TIMESTAMP=str(ts),
            HTTP_X_SYNC_FILENAME='evil.sh',
        )
        self.assertEqual(r.status_code, 400)


class OutboundSyncSignalTests(TestCase):
    """sync/signals.py — Aspired → Moonieful envelope shape."""

    def _account(self, moonieful_client_id='66666666-6666-6666-6666-666666666666'):
        from django.contrib.auth import get_user_model

        from clients.account_models import Account

        User = get_user_model()
        user = User.objects.create_user(
            username=f'out{moonieful_client_id[:8]}',
            email=f'{moonieful_client_id[:8]}@example.com', password='x')
        return Account.objects.create(
            user=user, name='Outbound Co',
            moonieful_client_id=moonieful_client_id,
            synced_from_moonieful=True)

    def test_stage_change_queues_the_contract_shaped_envelope(self):
        from clients.account_models import Website
        from sync.models import SyncJob

        account = self._account()
        site = Website.objects.create(account=account, name='Site', stage='intake')
        site.stage = 'review'
        site.save()

        job = SyncJob.objects.get(event_type='stage_changed')
        self.assertEqual(job.payload_snapshot['moonieful_client_id'],
                          str(account.moonieful_client_id))
        self.assertEqual(job.payload_snapshot['data']['aspired_project_stage'],
                          'review')
        self.assertIsNotNone(job.event_id)

    def test_maintenance_activated_queues_the_contract_shaped_envelope(self):
        from clients.account_models import Website
        from sync.models import SyncJob

        account = self._account()
        site = Website.objects.create(
            account=account, name='Site', maintenance_active=False)
        site.maintenance_active = True
        site.save()

        job = SyncJob.objects.get(event_type='maintenance_activated')
        self.assertEqual(job.payload_snapshot['moonieful_client_id'],
                          str(account.moonieful_client_id))
        self.assertTrue(job.payload_snapshot['data']['maintenance_active'])

    def test_a_direct_aspired_client_never_queues_a_job(self):
        """Most Aspired clients are direct, not Moonieful referrals. Queuing
        a job for them can only ever fail on Moonieful's end — the signal
        must skip accounts with no moonieful_client_id."""
        from django.contrib.auth import get_user_model

        from clients.account_models import Account, Website
        from sync.models import SyncJob

        User = get_user_model()
        user = User.objects.create_user(
            username='direct', email='direct@example.com', password='x')
        account = Account.objects.create(user=user, name='Direct Law Firm')
        site = Website.objects.create(account=account, name='Site', stage='intake')
        site.stage = 'review'
        site.save()

        self.assertEqual(SyncJob.objects.count(), 0)

    def test_inbound_originated_change_is_not_echoed_back(self):
        from clients.account_models import Website
        from sync.models import SyncJob

        account = self._account()
        site = Website.objects.create(account=account, name='Site', stage='intake')
        site.stage = 'live'
        site._from_sync = True
        site.save()

        self.assertEqual(SyncJob.objects.count(), 0)


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
