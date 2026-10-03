"""
The impersonation audit log must stay append-only, and the entry point
must stay a POST.

Behaviour of the session itself is covered in
clients/tests_impersonation.py. This module protects the two properties
that live on the admin_dashboard side and would be easy to undo while
tidying:

  - ImpersonationSessionAdmin blocks add, change AND delete. The delete
    override is a deliberate departure from the other ModelAdmins in
    this app, which leave deletion enabled — so it reads like an
    oversight and invites "fixing". It is not an oversight: the client
    is never told a view-as session happened, which makes this table the
    only record. If whoever can open a client's portal can also erase
    the trace, the log documents nothing.

  - The "View as client" control is a form POST, not an <a href>. As a
    link it would open a session and write an audit row from any
    prefetch or stray navigation.
"""

from django.contrib.admin.sites import site as admin_site
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from admin_dashboard.models import ImpersonationSession
from clients.account_models import Account, Website

User = get_user_model()


class AuditLogIsAppendOnlyTests(TestCase):

    # Looked up per-test rather than cached on the class: Django
    # deep-copies setUpTestData attributes between tests, and a
    # ModelAdmin holds an unpicklable thread lock.
    @property
    def model_admin(self):
        return admin_site._registry[ImpersonationSession]

    def test_rows_cannot_be_added_through_the_admin(self):
        self.assertFalse(self.model_admin.has_add_permission(None))

    def test_rows_cannot_be_edited_through_the_admin(self):
        self.assertFalse(self.model_admin.has_change_permission(None))

    def test_rows_cannot_be_deleted_through_the_admin(self):
        """The deliberate departure — see the module docstring."""
        self.assertFalse(self.model_admin.has_delete_permission(None))

    def test_every_field_is_readonly(self):
        """A field left writable would be editable via a crafted POST
        even with has_change_permission False on some Django versions."""
        readonly = self.model_admin.readonly_fields
        for field in ('operator', 'account', 'website', 'ended_at',
                      'end_reason', 'ip_address', 'user_agent',
                      'blocked_attempts'):
            with self.subTest(field=field):
                self.assertIn(field, readonly)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ViewAsEntryPointTests(TestCase):

    def setUp(self):
        self.staff = User.objects.create_user(
            username='entrystaff', email='entrystaff@example.com',
            password='test-pass-123', is_staff=True, is_superuser=True)
        staff_account = Account.objects.filter(user=self.staff).first()
        if staff_account is not None:
            staff_account.websites.all().delete()

        self.client_user = User.objects.create_user(
            username='entryclient', email='entryclient@example.com',
            password='test-pass-123')
        self.target = Account.objects.filter(user=self.client_user).first()
        if self.target is None:
            self.target = Account.objects.create(
                user=self.client_user, name='Entry Co')
        self.target.name = 'Entry Co'
        self.target.onboarding_status = 'complete'
        self.target.save()
        self.target.websites.all().delete()
        Website.objects.create(
            account=self.target, name='Entry Site',
            onboarding_status='complete')

    def test_the_account_page_offers_view_as_as_a_post_form(self):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse('admin_dashboard:v2_account_detail',
                    args=[self.target.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'View as client')
        self.assertContains(
            resp,
            reverse('admin_dashboard:v2_account_view_as',
                    args=[self.target.id]))

    def test_the_entry_point_refuses_get(self):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse('admin_dashboard:v2_account_view_as',
                    args=[self.target.id]))
        self.assertEqual(resp.status_code, 405)
        self.assertFalse(ImpersonationSession.objects.exists())

    def test_an_anonymous_post_cannot_start_a_session(self):
        resp = self.client.post(
            reverse('admin_dashboard:v2_account_view_as',
                    args=[self.target.id]))
        self.assertNotEqual(resp.status_code, 200)
        self.assertFalse(ImpersonationSession.objects.exists())
