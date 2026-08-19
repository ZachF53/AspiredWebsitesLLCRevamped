"""
Every portal page must render for a client who has no legacy row.

The admin equivalent of this test
(`admin_dashboard.tests_canonical_only_render`) found eleven broken pages
on its first run — ten hard 500s and one that returned 200 with the
client's name silently blank. The portal has had no such coverage at all,
and it is the surface the *client* sees: an admin page that 500s is an
internal annoyance, a portal page that 500s is a paying customer locked
out of their own project.

The staging render sweep cannot find these either. Staging's rows predate
the cutover and still carry a legacy FK, so every lookup resolves there
and the pages look fine. Only a fixture with no legacy row anywhere
exposes it — which is also the shape of every client created since the
cutover, so anything failing here is failing in production now.

Two failure modes, asserted separately:

- a 500 (NoReverseMatch, AttributeError on a null relation, FieldError)
- a 200 whose content is missing, because Django resolves a missing
  attribute in a template to the empty string. Status alone cannot see
  that, so pages that list client-owned rows also assert the row is
  actually on the page.

The content check is deliberately *not* "does the site name appear".
The portal's site bar only renders for multi-website accounts, so a
single-site client legitimately never sees their site name on most
pages — asserting on it would have failed a dozen correct pages and
sent me rewriting them.
"""

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account, Website

User = get_user_model()

SITE_NAME = 'Canonical Only Dental'

#: url name -> a string the page must contain, for pages that list rows
#: the client owns. Proves the queryset resolved, not just that the
#: template rendered.
MUST_CONTAIN = {
    'clients:support': 'Front page typo',
    'clients:revisions': 'Swap the hero photo',
    'clients:files': 'Brand guide',
    'clients:portal_changelog': 'Launched the new gallery',
    'clients:portal_suggestions': 'Add online booking',
    'clients:portal_seo': 'dentist san antonio',
    'clients:dashboard': SITE_NAME,
}


class PortalCanonicalOnlyTests(TestCase):
    """No ClientProfile, no Project — anywhere in this fixture."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='portalowner', email='portalowner@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(
            user=cls.user, name=SITE_NAME, contact_name='Sam Ortiz',
            phone='210-555-0199', onboarding_status='complete',
            stripe_customer_id='cus_portal_canonical')
        cls.website = Website.objects.create(
            account=cls.account, name=SITE_NAME,
            url='https://canonical-dental.example.com',
            package='essential_build', stage='live',
            payment_status='fully_paid',
            onboarding_status='complete',
            launch_date=datetime.date.today() - datetime.timedelta(days=40),
            maintenance_active=True)

        # The premise. If a signal quietly manufactures a legacy row the
        # fixture stops testing what it claims to.
        assert cls.account.legacy_client_profile_id is None
        assert cls.website.legacy_project_id is None

        cls._build_rows()

    @classmethod
    def _build_rows(cls):
        from clients.models import (
            AnnualReport,
            ClientDocument,
            IntakeResponse,
            IntelligenceSuggestion,
            RevisionRequest,
            SiteChangelogEntry,
            SupportTicket,
        )
        from reporting.models import MonthlyReport, TrackedKeyword

        month = datetime.date.today().replace(day=1)

        IntakeResponse.objects.create(
            website_new=cls.website, completed=True,
            completed_at=timezone.now(), about_copy='We fix teeth.')
        SupportTicket.objects.create(
            account_new=cls.account, website_new=cls.website,
            subject='Front page typo', description='Missing an apostrophe.')
        RevisionRequest.objects.create(
            website_new=cls.website, description='Swap the hero photo.')
        # No file attached. `sync.handlers.handle_document_added`
        # registers the row and the file follows separately, so this is a
        # real state and it used to 500 the entire Files page.
        ClientDocument.objects.create(
            website_new=cls.website,
            label='Brand guide', direction='to_client')
        SiteChangelogEntry.objects.create(
            website_new=cls.website, title='Launched the new gallery',
            is_client_visible=True)
        MonthlyReport.objects.create(
            website_new=cls.website, report_month=month, status='sent')
        AnnualReport.objects.create(
            website_new=cls.website, report_year=month.year - 1,
            status='sent')
        IntelligenceSuggestion.objects.create(
            website_new=cls.website, title='Add online booking',
            description='No way to book right now.',
            status='sent_to_client', sent_to_client_at=timezone.now())
        TrackedKeyword.objects.create(
            website_new=cls.website, keyword='dentist san antonio')

    def setUp(self):
        self.client.force_login(self.user)

    def _renders(self, name):
        url = reverse(name)
        response = self.client.get(url, follow=True)
        self.assertEqual(
            response.status_code, 200,
            f'{name} ({url}) did not render for a client with no legacy row')
        needle = MUST_CONTAIN.get(name)
        if needle:
            self.assertContains(
                response, needle,
                msg_prefix=(
                    f'{name} returned 200 but the client-owned row is '
                    'missing from it — the silent form, where the queryset '
                    'scoped on the legacy FK matched nothing and the page '
                    'rendered empty.'))

    # ── the pages a client actually lives in ────────────────────────

    def test_dashboard(self):
        self._renders('clients:dashboard')

    def test_project(self):
        self._renders('clients:project')

    def test_files(self):
        self._renders('clients:files')

    def test_revisions(self):
        self._renders('clients:revisions')

    def test_revision_new(self):
        self._renders('clients:revision_new')

    def test_support(self):
        self._renders('clients:support')

    def test_support_new(self):
        self._renders('clients:support_new')

    def test_invoices(self):
        self._renders('clients:invoices')

    def test_settings(self):
        self._renders('clients:settings')

    def test_changelog(self):
        self._renders('clients:portal_changelog')

    def test_reports(self):
        self._renders('clients:portal_reports')

    def test_seo(self):
        self._renders('clients:portal_seo')

    def test_security(self):
        self._renders('clients:portal_security')

    def test_referral(self):
        self._renders('clients:portal_referral')

    def test_suggestions(self):
        self._renders('clients:portal_suggestions')

    def test_recordings(self):
        self._renders('clients:portal_recordings')

    def test_credentials(self):
        self._renders('clients:credentials')

    def test_social_channels(self):
        self._renders('clients:social_channels')

    def test_maintenance(self):
        self._renders('clients:portal_maintenance')

    def test_social_plans(self):
        self._renders('clients:portal_social_plans')

    def test_subscriptions(self):
        """500'd for any comped client: `Account` was read for the comp
        labels and never imported."""
        self._renders('clients:portal_subscriptions')

    def test_domains(self):
        self._renders('domains:portal_domains')


class CompedClientPortalTests(TestCase):
    """A comped account — the branch that was never rendered in a test.

    `portal_subscriptions` builds comp rows from `Account.BUILD_COMP_CHOICES`
    and never imported `Account`, so the page raised NameError. The branch
    only runs when a comp package is set, which is why a full suite and a
    staging sweep both missed it — and why both comped accounts on
    production hit it.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username='compedowner', email='comped@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(
            user=cls.user, name='Comped Co', onboarding_status='complete',
            comp_build_package='premium_build',
            comp_maintenance_package='maintenance_dominant',
            comp_notes='Partner arrangement.')
        cls.website = Website.objects.create(
            account=cls.account, name='Comped Co', stage='live',
            onboarding_status='complete', maintenance_active=True)

    def setUp(self):
        self.client.force_login(self.user)

    def test_subscriptions_renders_the_comped_rows(self):
        response = self.client.get(
            reverse('clients:portal_subscriptions'), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Comped')

    def test_the_comped_maintenance_tier_is_named(self):
        response = self.client.get(
            reverse('clients:portal_subscriptions'), follow=True)
        self.assertContains(response, 'Dominant')
