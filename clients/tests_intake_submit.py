"""
Intake photo upload, and intake submission.

Two crashes at the end of onboarding, both from writing legacy-shaped
fields onto canonical models:

  - intake_photo_path built the upload path from
    `intake.project.client_id`. `project` is the legacy FK and is null on
    every intake created post-refactor, so FileField.save() raised
    AttributeError. htmx swallows an error response, so selecting photos
    just appeared to do nothing.
  - _on_intake_submitted set `onboarding_complete` on the Website, which
    is an Account column. update_fields then raised ValueError — AFTER
    the intake was marked complete, so answers saved but the client got
    a crash instead of a confirmation, and provisioning, the file copy,
    the changelog entry and the confirmation email never ran.
"""

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from clients.account_models import Account, Website
from clients.models import IntakePhoto, IntakeResponse

User = get_user_model()

PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 128


class IntakePhotoPath(TestCase):

    def setUp(self):
        u = User.objects.create_user(
            username='photouser', email='photo@example.com', password='x')
        self.account = Account.objects.filter(user=u).first() or (
            Account.objects.create(user=u, name='Photo Co'))
        self.account.websites.all().delete()
        self.website = Website.objects.create(
            account=self.account, name='Photo Site')
        self.intake = IntakeResponse.objects.create(website_new=self.website)

    def test_intake_has_no_legacy_project(self):
        self.assertIsNone(self.intake.project)

    def test_saving_a_photo_does_not_raise(self):
        photo = IntakePhoto.objects.create(
            intake=self.intake,
            file=SimpleUploadedFile('a.png', PNG, content_type='image/png'))
        self.assertTrue(photo.file.name)

    def test_path_is_keyed_on_the_website(self):
        from clients.models import intake_photo_path
        path = intake_photo_path(
            IntakePhoto(intake=self.intake), 'shot.png')
        self.assertEqual(
            path, f'portal/intake/photos/{self.website.pk}/shot.png')

    def test_orphan_intake_still_gets_a_path(self):
        from clients.models import intake_photo_path
        orphan = IntakeResponse.objects.create()
        path = intake_photo_path(IntakePhoto(intake=orphan), 'x.png')
        self.assertIn(str(orphan.pk), path)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class IntakeSubmitCompletesOnboarding(TestCase):

    def test_website_has_no_onboarding_complete_column(self):
        """Guards the premise — it lives on Account."""
        web_fields = {f.name for f in Website._meta.fields}
        acct_fields = {f.name for f in Account._meta.fields}
        self.assertNotIn('onboarding_complete', web_fields)
        self.assertIn('onboarding_complete', acct_fields)

    def test_intake_files_are_copied_to_the_files_page(self):
        """`profile.user` on a Website killed this inside a best-effort
        except, so logos and photos silently never appeared."""
        from clients.models import ClientDocument
        from clients.views import _copy_intake_files_to_documents

        u = User.objects.create_user(
            username='copyuser', email='copy@example.com', password='x')
        account = Account.objects.filter(user=u).first() or (
            Account.objects.create(user=u, name='Copy Co'))
        account.websites.all().delete()
        website = Website.objects.create(account=account, name='Copy Site')
        intake = IntakeResponse.objects.create(
            website_new=website,
            logo=SimpleUploadedFile('logo.png', PNG, content_type='image/png'))
        IntakePhoto.objects.create(
            intake=intake,
            file=SimpleUploadedFile('p1.png', PNG, content_type='image/png'))

        _copy_intake_files_to_documents(website, website)

        docs = ClientDocument.objects.filter(website_new=website)
        self.assertEqual(docs.count(), 2)
        self.assertEqual(
            {d.uploaded_by for d in docs}, {u},
            'uploader should be the account owner')

    def test_on_intake_submitted_marks_both_records(self):
        from clients.views import _on_intake_submitted

        u = User.objects.create_user(
            username='submituser', email='submit@example.com', password='x')
        account = Account.objects.filter(user=u).first() or (
            Account.objects.create(user=u, name='Submit Co'))
        account.websites.all().delete()
        website = Website.objects.create(
            account=account, name='Submit Site',
            onboarding_status='pending_intake', build_platform='wordpress')
        IntakeResponse.objects.create(website_new=website)

        # Must not raise — this is what 500'd the submit.
        _on_intake_submitted(website, website)

        website.refresh_from_db()
        account.refresh_from_db()
        self.assertEqual(website.onboarding_status, 'onboarding_complete')
        self.assertIsNotNone(website.needs_admin_review_at)
        self.assertTrue(account.onboarding_complete)
        self.assertEqual(account.onboarding_status, 'onboarding_complete')
