"""
The v2 dashboard shows what Miki sees.

A Moonieful-referred website gets a Moonieful tab (sync summary, her
stages, every optional section, inbound + outbound sync activity), its
intake answers render on the Intake tab, and every file downloads through
the auth-checked view rather than a public /media/ URL.
"""

import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from clients.models import ClientDocument
from sync.handlers import handle_client_created
from sync.models import SyncLog
from sync.tests_handlers import _bundle

User = get_user_model()

INTAKE_FILE_REF = '77777777-7777-7777-7777-777777777777'


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class V2MooniefulTests(TestCase):

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

    def setUp(self):
        self.staff = User.objects.create_user(
            username='v2moon', email='v2moon@example.com', password='pw',
            is_staff=True, is_superuser=True)
        bundle = _bundle(
            documents=[{
                'id': '22222222-2222-2222-2222-222222222222', 'label': 'Brand guide',
                'description': 'Final PDF', 'direction': 'to_client',
                'filename': 'guide.pdf', 'file_ref': '22222222-2222-2222-2222-222222222222',
                'category': 'deliverable', 'updated_at': '2026-01-03T00:00:00+00:00'}],
            tasks=[{'id': 't1', 'title': 'Approve moodboard <b>now</b>', 'status': 'open'}],
            completion={'training_delivered': True, 'notes': 'All wrapped'},
        )
        bundle['intake'][0]['answers'].append({
            'question_text': 'Upload your logo', 'question_type': 'file',
            'value_text': '', 'file_ref': INTAKE_FILE_REF})
        self.account = handle_client_created(bundle)
        self.site = self.account.websites.get(moonieful_referred=True)
        SyncLog.objects.create(
            source_site='moonieful', event_type='client_created', status='processed',
            account_new=self.account)
        self.doc = ClientDocument.objects.get(
            moonieful_document_id='22222222-2222-2222-2222-222222222222')
        self.doc.file.save('guide.pdf', ContentFile(b'%PDF guide'), save=True)
        logo = ClientDocument.objects.get(moonieful_document_id=INTAKE_FILE_REF)
        logo.file.save('logo.png', ContentFile(b'\x89PNG'), save=True)
        self.client.force_login(self.staff)

    def _tab(self, tab, extra=''):
        return self.client.get(
            f'/admin-dashboard/v2/websites/{self.site.id}/?tab={tab}{extra}')

    def test_moonieful_tab_is_in_the_nav_only_for_referred_sites(self):
        self.assertIn(b'?tab=moonieful', self._tab('overview').content)

    def test_moonieful_tab_renders_everything_received(self):
        r = self._tab('moonieful')
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('Sync summary', body)
        self.assertIn('Brand + Site', body)          # package sold
        self.assertIn('brand', body)                  # stage history
        # Task content rendered, autoescaped.
        self.assertIn('Approve moodboard &lt;b&gt;now&lt;/b&gt;', body)
        self.assertNotIn('<b>now</b>', body)
        self.assertIn('All wrapped', body)            # completion (kv)
        self.assertIn('Not shared by Moonieful yet', body)   # e.g. meetings
        self.assertIn('client_created', body)         # sync activity

    def test_intake_tab_shows_moonieful_answers_and_file_link(self):
        r = self._tab('intake')
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('Moonieful intake — Brand Intake', body)
        self.assertIn('sage, cream', body)
        logo = ClientDocument.objects.get(moonieful_document_id=INTAKE_FILE_REF)
        self.assertIn(f'/documents/{logo.id}/download/', body)
        self.assertNotIn('Intake has not been submitted yet', body)

    def test_files_tab_shows_metadata_and_links_through_the_view(self):
        r = self._tab('documents')
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn('Final PDF', body)
        self.assertIn('Deliverable', body)
        self.assertIn(f'/documents/{self.doc.id}/download/', body)
        self.assertNotIn('/media/portal/', body)

    def test_admin_download_is_forced_and_sandboxed(self):
        r = self.client.get(
            f'/admin-dashboard/v2/websites/{self.site.id}/documents/{self.doc.id}/download/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r['Content-Disposition'])
        # The admin-dashboard CSP must not replace the sandbox policy.
        self.assertIn('sandbox', r['Content-Security-Policy'])
        self.assertEqual(b''.join(r.streaming_content), b'%PDF guide')

    def test_download_scoped_to_the_website_in_the_url(self):
        other = User.objects.create_user(username='o', email='o@example.com')
        from clients.account_models import Account, Website
        acct = Account.objects.filter(user=other).first() or \
            Account.objects.create(user=other, name='Other')
        other_site = Website.objects.create(account=acct, name='Other Site')
        r = self.client.get(
            f'/admin-dashboard/v2/websites/{other_site.id}/documents/{self.doc.id}/download/')
        self.assertEqual(r.status_code, 404)

    def test_non_staff_cannot_download(self):
        self.client.force_login(self.account.user)
        r = self.client.get(
            f'/admin-dashboard/v2/websites/{self.site.id}/documents/{self.doc.id}/download/')
        self.assertEqual(r.status_code, 302)

    def test_monitoring_tab_renders_the_stage_log(self):
        from clients.account_models import WebsiteStageLog
        WebsiteStageLog.objects.create(
            website=self.site, from_stage='review', to_stage='live',
            note='Project handed off from Moonieful.', set_by='sync')
        r = self._tab('monitoring')
        self.assertIn(b'Project handed off from Moonieful.', r.content)

    def test_overview_shows_revisions(self):
        r = self._tab('overview')
        self.assertIn(b'Revisions used:', r.content)

    def test_account_detail_has_the_sync_card(self):
        r = self.client.get(f'/admin-dashboard/v2/accounts/{self.account.id}/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Moonieful sync', r.content)
        self.assertIn(str(self.account.moonieful_client_id).encode(), r.content)
