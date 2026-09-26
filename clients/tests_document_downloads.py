"""
Client documents are private.

They used to live under MEDIA_ROOT, which Nginx serves at /media/ with no
auth — any client's contract or brand files were readable by anyone
holding the URL. They now live in PRIVATE_MEDIA_ROOT and only leave
through auth-checked, forced-download views.
"""

import os
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from clients.account_models import Account, Website
from clients.models import ClientDocument

User = get_user_model()


class _TempRoots:

    @classmethod
    def setUpClass(cls):
        cls._private = tempfile.mkdtemp(prefix='aspired-private-')
        cls._public = tempfile.mkdtemp(prefix='aspired-media-')
        cls._override = override_settings(
            PRIVATE_MEDIA_ROOT=cls._private, MEDIA_ROOT=cls._public)
        cls._override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._override.disable()
        shutil.rmtree(cls._private, ignore_errors=True)
        shutil.rmtree(cls._public, ignore_errors=True)


def _client(username):
    user = User.objects.create_user(
        username=username, email=f'{username}@example.com', password='pw-123456')
    account = Account.objects.filter(user=user).first() or \
        Account.objects.create(user=user, name=f'{username} Co')
    account.onboarding_status = 'complete'
    account.save(update_fields=['onboarding_status'])
    account.websites.all().delete()
    site = Website.objects.create(
        account=account, name=f'{username} Site', onboarding_status='complete')
    return user, account, site


def _doc(site, name='brand.svg', body=b'<svg onload="alert(1)"/>', **fields):
    doc = ClientDocument(website_new=site, direction='to_client', label='Brand', **fields)
    doc.file.save(name, ContentFile(body), save=True)
    return doc


class PortalDocumentDownloadTests(_TempRoots, TestCase):

    def setUp(self):
        self.user, self.account, self.site = _client('owner')
        self.doc = _doc(self.site)
        self.client.force_login(self.user)

    def test_owner_gets_a_forced_sandboxed_download(self):
        r = self.client.get(reverse('clients:portal_document_download', args=[self.doc.id]))
        self.assertEqual(r.status_code, 200)
        self.assertIn('attachment', r['Content-Disposition'])
        self.assertEqual(r['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox', r['Content-Security-Policy'])
        self.assertEqual(r['Content-Type'], 'application/octet-stream')
        self.assertEqual(b''.join(r.streaming_content), b'<svg onload="alert(1)"/>')

    def test_another_clients_document_is_404(self):
        _, _, other_site = _client('stranger')
        other = _doc(other_site, name='secret.pdf', body=b'%PDF secret')
        r = self.client.get(reverse('clients:portal_document_download', args=[other.id]))
        self.assertEqual(r.status_code, 404)

    def test_anonymous_is_sent_to_login(self):
        self.client.logout()
        r = self.client.get(reverse('clients:portal_document_download', args=[self.doc.id]))
        self.assertEqual(r.status_code, 302)

    def test_hidden_and_deleted_moonieful_files_are_not_served_or_listed(self):
        hidden = _doc(self.site, name='hidden.pdf', body=b'x',
                      moonieful_visible_to_client=False)
        deleted = _doc(self.site, name='deleted.pdf', body=b'x', moonieful_deleted=True)
        for d in (hidden, deleted):
            r = self.client.get(reverse('clients:portal_document_download', args=[d.id]))
            self.assertEqual(r.status_code, 404)
        page = self.client.get(reverse('clients:files'))
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        self.assertIn(reverse('clients:portal_document_download', args=[self.doc.id]), body)
        self.assertNotIn(str(hidden.id), body)
        self.assertNotIn(str(deleted.id), body)

    def test_files_page_never_links_to_media(self):
        page = self.client.get(reverse('clients:files'))
        self.assertNotIn('/media/', page.content.decode())

    def test_the_file_is_not_under_media_root(self):
        self.assertTrue(os.path.abspath(self.doc.file.path).startswith(
            os.path.abspath(self._private)))
        with self.assertRaises(ValueError):
            self.doc.file.url  # noqa: B018 — private storage has no URL


class MoveDocumentsPrivateTests(_TempRoots, TestCase):

    def setUp(self):
        _, _, self.site = _client('mover')
        self.doc = ClientDocument.objects.create(
            website_new=self.site, direction='to_client', label='Old',
            file=f'portal/clients/website/{self.site.id}/docs/old.pdf')
        self.public_path = os.path.join(self._public, self.doc.file.name)
        os.makedirs(os.path.dirname(self.public_path), exist_ok=True)
        with open(self.public_path, 'wb') as fh:
            fh.write(b'%PDF old')

    def test_dry_run_moves_nothing(self):
        call_command('move_documents_private', '--dry-run', stdout=open(os.devnull, 'w'))
        self.assertTrue(os.path.exists(self.public_path))
        self.assertFalse(os.path.exists(self.doc.file.path))

    def test_moves_and_is_idempotent(self):
        devnull = open(os.devnull, 'w')
        call_command('move_documents_private', stdout=devnull)
        self.assertFalse(os.path.exists(self.public_path))
        with self.doc.file.open('rb') as fh:
            self.assertEqual(fh.read(), b'%PDF old')
        call_command('move_documents_private', stdout=devnull)
        with self.doc.file.open('rb') as fh:
            self.assertEqual(fh.read(), b'%PDF old')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class DjangoAdminDocumentPageTests(_TempRoots, TestCase):
    """The default file widget calls .url, which private storage refuses."""

    def test_change_page_renders_with_a_download_link(self):
        staff = User.objects.create_user(
            username='boss', email='boss@example.com', password='pw',
            is_staff=True, is_superuser=True)
        _, _, site = _client('adminview')
        doc = _doc(site, name='x.pdf', body=b'%PDF')
        self.client.force_login(staff)
        r = self.client.get(reverse('admin:clients_clientdocument_change', args=[doc.id]))
        self.assertEqual(r.status_code, 200)
        self.assertIn(
            reverse('admin_dashboard:v2_website_document_download', args=[site.id, doc.id]),
            r.content.decode())
