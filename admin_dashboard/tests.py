"""Tests for the admin-dashboard client edit form + quick-edit endpoint."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from clients.models import ClientProfile

User = get_user_model()


class ClientProfileEditFormTests(TestCase):
    """Regression coverage for the package-dropdown + live-url
    auto-prepend fixes."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='admin-form', email='af@example.com',
            password='x', is_staff=True, is_superuser=True)
        self.client_profile = ClientProfile.objects.create(
            user=self.user, firm_name='Edit Co')

    def _base_post(self, **overrides):
        """Minimum-valid POST data for ClientProfileEditForm."""
        data = {
            'firm_name': 'Edit Co',
            'contact_name': '',
            'business_type': '',
            'status': 'active',
            'package': '',
            'city': '', 'state': '', 'phone': '',
            'do_droplet_ip': '',
            'do_droplet_created_at': '',
            'live_url': '',
            'maintenance_active': '',
            'auto_send_scan_reports': '',
            'onboarding_complete': '',
            'is_tester': '',
            'internal_notes': '',
        }
        data.update(overrides)
        return data

    # ── package = dropdown ──

    def test_package_is_a_choice_field_with_canonical_choices(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(instance=self.client_profile)
        choices = dict(form.fields['package'].choices)
        # Blank option always present
        self.assertIn('', choices)
        # All canonical package codes are options
        for code, _label in ClientProfile.PACKAGE_CHOICES:
            self.assertIn(code, choices)

    def test_package_dropdown_renders_select_html(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(instance=self.client_profile)
        rendered = str(form['package'])
        self.assertIn('<select', rendered)
        self.assertNotIn('<input', rendered)

    def test_package_invalid_value_rejected(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(package='garbage-not-a-real-code'),
            instance=self.client_profile)
        self.assertFalse(form.is_valid())
        self.assertIn('package', form.errors)

    def test_package_valid_choice_saves(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(package='essential_build'),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.package, 'essential_build')

    def test_package_blank_is_allowed(self):
        from admin_dashboard.forms import ClientProfileEditForm
        self.client_profile.package = 'essential_build'
        self.client_profile.save()
        form = ClientProfileEditForm(
            self._base_post(package=''),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.package, '')

    # ── live_url = tolerant CharField + auto-https ──

    def test_live_url_empty_stays_empty(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(live_url=''),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['live_url'], '')

    def test_live_url_naked_domain_gets_https_prepended(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(live_url='clientdomain.com'),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data['live_url'], 'https://clientdomain.com')

    def test_live_url_existing_https_unchanged(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(live_url='https://already.com'),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data['live_url'], 'https://already.com')

    def test_live_url_http_kept_as_is(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(live_url='http://legacy.com'),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data['live_url'], 'http://legacy.com')

    def test_live_url_whitespace_stripped(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(live_url='   spaced.com   '),
            instance=self.client_profile)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data['live_url'], 'https://spaced.com')

    def test_live_url_garbage_rejected(self):
        from admin_dashboard.forms import ClientProfileEditForm
        form = ClientProfileEditForm(
            self._base_post(live_url='not a domain at all'),
            instance=self.client_profile)
        self.assertFalse(form.is_valid())
        self.assertIn('live_url', form.errors)


class ClientQuickEditLiveUrlTests(TestCase):
    """The inline HTMX quick-edit for live_url should accept naked
    domains and auto-prepend https://."""

    def setUp(self):
        from django.test import Client as DjangoTestClient
        self.user = User.objects.create_user(
            username='qe-admin', email='qe@example.com',
            password='x', is_staff=True, is_superuser=True)
        self.profile = ClientProfile.objects.create(
            user=self.user, firm_name='QE Co', stage='live')
        # 2026-05-25 refactor: quick-edit live_url writes to
        # client.website (canonical). No project row required.
        self.tc = DjangoTestClient()
        self.tc.force_login(self.user)

    def _post_url(self, value):
        return self.tc.post(
            reverse('admin_dashboard:client_quick_edit_field',
                    args=[self.profile.id]),
            {'field': 'live_url', 'value': value})

    def test_naked_domain_gets_https_prepended(self):
        resp = self._post_url('clientdomain.com')
        self.assertEqual(resp.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(
            self.profile.website, 'https://clientdomain.com')

    def test_https_value_kept_as_is(self):
        self._post_url('https://kept.com')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.website, 'https://kept.com')

    def test_empty_value_clears_url(self):
        self.profile.website = 'https://old.com'
        self.profile.save()
        self._post_url('')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.website, '')

    def test_quick_edit_field_meta_uses_text_input(self):
        """Browser-side blocker: type=url rejects 'clientdomain.com'.
        Our quick-edit must use type=text so submission isn't blocked
        before the server can normalise."""
        from admin_dashboard.forms import CLIENT_QUICK_EDIT_FIELDS
        self.assertEqual(
            CLIENT_QUICK_EDIT_FIELDS['live_url']['type'], 'text')
