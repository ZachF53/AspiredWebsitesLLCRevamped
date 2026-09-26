"""
The Aspired side of the Moonieful bridge, made complete.

  * the file endpoint streams large bodies (the 2.5 MB Django cap used to
    reject every real brand file before the size check ever ran), holds
    them to the signed Content-Length, replaces rather than duplicates,
    and logs every attempt;
  * the inbound audit log never keeps the client's password hash, and
    records the handler's real outcome;
  * document metadata (direction, description, filename, category,
    deleted/hidden) is kept and is last-writer-wins;
  * a synced website is intake-complete, so the portal never sends
    Miki's client to Aspired's own intake form;
  * the optional extension keys land in Website.moonieful_extra.
"""

import json
import os
import shutil
import tempfile
import time

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account, Website
from clients.models import ClientDocument, IntakeResponse
from sync.models import SyncLog
from sync.security import sign
from sync.tests_handlers import _bundle

TEST_SECRET = 'test-sync-secret-do-not-use-in-prod'
DOC_ID = '55555555-5555-5555-5555-555555555555'

User = get_user_model()


class _TempPrivateMedia:
    """Point PRIVATE_MEDIA_ROOT at a throwaway directory per test class."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix='aspired-private-')
        cls._override = override_settings(PRIVATE_MEDIA_ROOT=cls._tmp)
        cls._override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._override.disable()
        shutil.rmtree(cls._tmp, ignore_errors=True)


@override_settings(MOONIEFUL_SYNC_SECRET=TEST_SECRET, SYNC_MAX_FILE_SIZE=8 * 1024 * 1024)
class SyncFileStreamingTests(_TempPrivateMedia, TestCase):

    def setUp(self):
        user = User.objects.create_user(
            username='streamer', email='stream@example.com', password='x')
        self.account = Account.objects.filter(user=user).first() or \
            Account.objects.create(user=user, name='Stream Co')
        self.site = Website.objects.create(account=self.account, name='Stream Site')
        self.document = ClientDocument.objects.create(
            website_new=self.site, direction='to_client', label='Brand guide',
            moonieful_document_id=DOC_ID)
        self.url = reverse('sync:file', args=[DOC_ID])

    def _post(self, body, *, filename='brand-guide.pdf', signed_len=None, **extra):
        ts = int(time.time())
        n = len(body) if signed_len is None else signed_len
        sig = sign(ts, f'http://testserver{self.url}\n{n}'.encode())
        return self.client.post(
            self.url, data=body, content_type='application/octet-stream',
            HTTP_X_SYNC_SIGNATURE=sig, HTTP_X_SYNC_TIMESTAMP=str(ts),
            HTTP_X_SYNC_FILENAME=filename, **extra)

    def test_a_file_over_the_old_2_5_mb_cap_is_accepted(self):
        body = os.urandom(3 * 1024 * 1024)
        r = self._post(body, filename='logo-master.psd')
        self.assertEqual(r.status_code, 200, r.content)
        self.document.refresh_from_db()
        with self.document.file.open('rb') as fh:
            self.assertEqual(fh.read(), body)
        self.assertEqual(self.document.original_filename, 'logo-master.psd')

    def test_the_file_lands_in_private_storage_not_media_root(self):
        from django.conf import settings
        r = self._post(b'%PDF-1.4 bytes')
        self.assertEqual(r.status_code, 200)
        self.document.refresh_from_db()
        path = self.document.file.path
        self.assertTrue(path.startswith(os.path.abspath(self._tmp)))
        self.assertFalse(path.startswith(os.path.abspath(str(settings.MEDIA_ROOT))))

    def test_over_the_limit_is_413(self):
        r = self._post(b'x' * (8 * 1024 * 1024 + 1))
        self.assertEqual(r.status_code, 413)
        self.document.refresh_from_db()
        self.assertFalse(self.document.file)

    def test_a_signature_over_a_different_length_is_rejected(self):
        r = self._post(b'real bytes', signed_len=4)
        self.assertEqual(r.status_code, 403)

    def test_a_resend_replaces_the_file_without_leaving_an_orphan(self):
        self._post(b'first version')
        self.document.refresh_from_db()
        first_path = self.document.file.path
        self._post(b'second version')
        self.document.refresh_from_db()
        with self.document.file.open('rb') as fh:
            self.assertEqual(fh.read(), b'second version')
        docs_dir = os.path.dirname(self.document.file.path)
        self.assertEqual(os.listdir(docs_dir), [os.path.basename(self.document.file.path)])
        self.assertEqual(os.path.dirname(first_path), docs_dir)

    def test_every_attempt_is_logged_against_the_account(self):
        self._post(b'%PDF ok')
        self._post(b'nope', filename='payload.zip')
        logs = SyncLog.objects.filter(event_type='file_received').order_by('created_at')
        self.assertEqual([l.status for l in logs], ['processed', 'failed'])
        self.assertTrue(all(l.account_new_id == self.account.id for l in logs))
        self.assertIn('.zip', logs[1].error_message)

    def test_an_unknown_ref_is_404_and_logged(self):
        self.url = reverse('sync:file', args=['99999999-9999-9999-9999-999999999999'])
        r = self._post(b'bytes')
        self.assertEqual(r.status_code, 404)
        self.assertTrue(SyncLog.objects.filter(
            event_type='file_received', status='failed').exists())


@override_settings(MOONIEFUL_SYNC_SECRET=TEST_SECRET)
class SyncInboundAuditTests(TestCase):

    def _post(self, bundle):
        body = json.dumps(bundle).encode()
        ts = int(time.time())
        return self.client.post(
            reverse('sync:inbound'), data=body, content_type='application/json',
            HTTP_X_SYNC_SIGNATURE=sign(ts, body), HTTP_X_SYNC_TIMESTAMP=str(ts))

    def test_the_password_hash_never_reaches_the_log(self):
        r = self._post(_bundle())
        self.assertEqual(r.status_code, 200, r.content)
        log = SyncLog.objects.get(event_type='client_created')
        self.assertEqual(log.status, 'processed')
        self.assertEqual(
            log.payload_received['client']['account']['password_hash'], '[redacted]')
        self.assertNotIn('fakehash', json.dumps(log.payload_received))
        # ...but the handler still got it: the new login carries the hash.
        self.assertEqual(
            User.objects.get(email='moon@example.com').password,
            'pbkdf2_sha256$fake$fakehash')

    def test_the_log_is_linked_to_the_account(self):
        self._post(_bundle())
        log = SyncLog.objects.get(event_type='client_created')
        self.assertEqual(
            log.account_new, Account.objects.get(moonieful_client_id=_bundle()['client']['moonieful_client_id']))

    def test_a_failed_handler_is_logged_failed_and_rolled_back(self):
        from unittest.mock import patch

        with patch('sync.handlers._upsert_documents', side_effect=RuntimeError('boom')):
            r = self._post(_bundle())
        self.assertEqual(r.status_code, 400)
        log = SyncLog.objects.get(event_type='client_created')
        self.assertEqual(log.status, 'failed')
        self.assertIn('boom', log.error_message)
        # Atomic: no half-synced client left behind.
        self.assertFalse(Account.objects.filter(synced_from_moonieful=True).exists())

    def test_a_failed_event_can_be_retried(self):
        from unittest.mock import patch

        with patch('sync.handlers._upsert_documents', side_effect=RuntimeError('boom')):
            self._post(_bundle())
        r = self._post(_bundle())
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b'duplicate', r.content)

    def test_a_wrong_schema_version_is_logged(self):
        r = self._post(_bundle(schema_version=2))
        self.assertEqual(r.status_code, 400)
        log = SyncLog.objects.get(status='failed')
        self.assertIn('schema_version', log.error_message)
        self.assertEqual(
            log.payload_received['client']['account']['password_hash'], '[redacted]')


class RedactMigrationTests(TestCase):
    """The data migration scrubs rows written before redaction existed."""

    def test_old_rows_are_scrubbed(self):
        from django.apps import apps
        from importlib import import_module

        log = SyncLog.objects.create(
            source_site='moonieful', event_type='client_created',
            payload_received=_bundle(), status='processed')
        mod = import_module('sync.migrations.0005_redact_synclog_password_hash')
        mod.forwards(apps, None)
        log.refresh_from_db()
        self.assertEqual(
            log.payload_received['client']['account']['password_hash'], '[redacted]')


class DocumentMetadataTests(TestCase):

    def _doc(self, **over):
        doc = {
            'id': '22222222-2222-2222-2222-222222222222',
            'label': 'Signed logo files', 'description': 'All lockups',
            'direction': 'from_client', 'filename': 'logos.ai',
            'file_ref': '22222222-2222-2222-2222-222222222222',
            'created_at': '2026-01-01T00:00:00+00:00',
            'updated_at': '2026-01-05T00:00:00+00:00',
        }
        doc.update(over)
        return doc

    def test_every_field_is_kept(self):
        from sync.handlers import handle_client_created

        handle_client_created(_bundle(documents=[self._doc(category='asset')]))
        doc = ClientDocument.objects.get(moonieful_document_id=self._doc()['id'])
        self.assertEqual(doc.direction, 'from_client')
        self.assertEqual(doc.description, 'All lockups')
        self.assertEqual(doc.original_filename, 'logos.ai')
        self.assertEqual(doc.category, 'asset')
        self.assertEqual(doc.filename, 'logos.ai')

    def test_an_unknown_direction_falls_back_to_to_client(self):
        from sync.handlers import handle_client_created

        handle_client_created(_bundle(documents=[self._doc(direction='sideways')]))
        self.assertEqual(ClientDocument.objects.get().direction, 'to_client')

    def test_a_newer_bundle_updates_and_an_older_one_does_not(self):
        from sync.handlers import handle_client_created, handle_document_added

        handle_client_created(_bundle(documents=[self._doc()]))
        handle_document_added(_bundle(event_type='document_added', documents=[
            self._doc(label='Renamed', updated_at='2026-01-09T00:00:00+00:00')]))
        self.assertEqual(ClientDocument.objects.get().label, 'Renamed')

        handle_document_added(_bundle(event_type='document_added', documents=[
            self._doc(label='Stale', updated_at='2026-01-02T00:00:00+00:00')]))
        self.assertEqual(ClientDocument.objects.get().label, 'Renamed')
        self.assertEqual(ClientDocument.objects.count(), 1)

    def test_deleted_and_hidden_flags_are_kept(self):
        from sync.handlers import handle_client_created

        handle_client_created(_bundle(documents=[
            self._doc(deleted_at='2026-01-05T00:00:00+00:00'),
            self._doc(id='33333333-3333-3333-3333-333333333333',
                      file_ref='33333333-3333-3333-3333-333333333333',
                      visible_to_client=False),
        ]))
        deleted = ClientDocument.objects.get(moonieful_document_id=self._doc()['id'])
        hidden = ClientDocument.objects.get(
            moonieful_document_id='33333333-3333-3333-3333-333333333333')
        self.assertTrue(deleted.moonieful_deleted)
        self.assertFalse(deleted.client_can_see)
        self.assertFalse(hidden.moonieful_visible_to_client)
        self.assertFalse(hidden.client_can_see)

    def test_client_updated_upserts_documents_and_stage_history(self):
        from sync.handlers import handle_client_created, handle_client_updated

        account = handle_client_created(_bundle())
        bundle = _bundle(event_type='client_updated', documents=[self._doc()],
                         stage_history=[{'id': 's2', 'stage_name': 'site',
                                         'note': 'Handing off soon'}])
        bundle['client']['updated_at'] = timezone.now().isoformat()
        bundle['client']['moonieful_package'] = 'Brand Plus'
        handle_client_updated(bundle)

        site = account.websites.get(moonieful_referred=True)
        self.assertEqual(site.moonieful_stage_history[0]['stage_name'], 'site')
        self.assertEqual(site.moonieful_package, 'Brand Plus')
        self.assertTrue(ClientDocument.objects.filter(website_new=site).exists())

    def test_intake_files_are_from_the_client(self):
        from sync.handlers import handle_client_created

        bundle = _bundle()
        bundle['intake'][0]['answers'][0]['file_ref'] = (
            '77777777-7777-7777-7777-777777777777')
        handle_client_created(bundle)
        doc = ClientDocument.objects.get(
            moonieful_document_id='77777777-7777-7777-7777-777777777777')
        self.assertEqual(doc.direction, 'from_client')
        self.assertEqual(doc.category, 'intake')


class OnboardingStateTests(TestCase):

    def test_a_synced_site_is_intake_complete(self):
        from sync.handlers import handle_client_created

        account = handle_client_created(_bundle())
        site = account.websites.get(moonieful_referred=True)
        self.assertEqual(site.onboarding_status, 'intake_complete')
        intake = IntakeResponse.objects.get(website_new=site)
        self.assertTrue(intake.completed)
        self.assertEqual(intake.moonieful_intake_raw[0]['form_title'], 'Brand Intake')

    def test_the_portal_does_not_send_them_to_our_intake_form(self):
        from sync.handlers import handle_client_created

        account = handle_client_created(_bundle())
        self.client.force_login(account.user)
        r = self.client.get(reverse('clients:dashboard'), follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(reverse('clients:intake'),
                         [url for url, _ in r.redirect_chain])

    def test_backfill_migration_fixes_old_synced_sites(self):
        from django.apps import apps
        from importlib import import_module

        user = User.objects.create_user(username='old', email='old@example.com')
        account = Account.objects.filter(user=user).first() or \
            Account.objects.create(user=user, name='Old')
        site = Website.objects.create(account=account, name='Old Site',
                                      moonieful_referred=True)
        self.assertEqual(site.onboarding_status, 'pending_intake')
        IntakeResponse.objects.create(website_new=site)
        mod = import_module('clients.migrations.0068_moonieful_sites_intake_complete')
        mod.forwards(apps, None)
        site.refresh_from_db()
        self.assertEqual(site.onboarding_status, 'intake_complete')
        self.assertTrue(IntakeResponse.objects.get(website_new=site).completed)


class ExtensionKeyTests(TestCase):

    def test_extra_sections_are_stored_and_overwritten_per_key(self):
        from sync.handlers import handle_client_created, handle_stage_changed

        tasks = [{'id': 't1', 'title': 'Send logo files', 'status': 'open'}]
        account = handle_client_created(_bundle(
            tasks=tasks, invoices=[{'id': 'i1', 'label': 'Deposit', 'amount_cents': 50000}],
            not_a_known_key='ignored'))
        site = account.websites.get(moonieful_referred=True)
        self.assertEqual(site.moonieful_extra['tasks'], tasks)
        self.assertNotIn('not_a_known_key', site.moonieful_extra)

        # A later bundle with only `tasks` replaces tasks, leaves invoices.
        handle_stage_changed(_bundle(event_type='stage_changed', tasks=[]))
        site.refresh_from_db()
        self.assertEqual(site.moonieful_extra['tasks'], [])
        self.assertEqual(site.moonieful_extra['invoices'][0]['label'], 'Deposit')

    def test_extra_files_become_documents(self):
        from sync.handlers import handle_client_created

        account = handle_client_created(_bundle(extra_files=[
            {'ref': '44444444-4444-4444-4444-444444444444', 'label': 'Homepage screenshot',
             'category': 'screenshot', 'direction': 'to_client', 'filename': 'home.png'}]))
        doc = ClientDocument.objects.get(
            moonieful_document_id='44444444-4444-4444-4444-444444444444')
        self.assertEqual(doc.category, 'screenshot')
        self.assertEqual(doc.website_new.account_id, account.id)
