"""
Every admin page must render for a client that has no legacy row at all.

This is the post-drop world, built directly: an Account and a Website with
`legacy_client_profile` unset, and one row of each dependent model
carrying only its canonical FK. It is also the shape of every client
created since the cutover, so these pages are already wrong in production
for new clients -- not only after the drop.

The render sweep on staging could not catch this. Staging's rows were
written before the cutover and still carry their legacy FK, so
`{{ r.client.firm_name }}` resolves there and the page looks fine. The
data hides the bug; only data without a legacy row exposes it.

Two distinct failures are asserted, because they present completely
differently:

- ``{% url '...' r.client.id %}`` reverses with an empty argument and
  raises NoReverseMatch. That is a 500 on the whole page, and the test
  client re-raises it, so `assertEqual(200)` catches it.

- ``{{ r.client.firm_name }}`` resolves to the empty string. No
  exception, no log line, HTTP 200, and a blank cell where the client's
  name should be. Status alone cannot see it, so each page is also
  asserted to contain the site's name.
"""

import datetime
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account, Website

User = get_user_model()

SITE_NAME = 'Canonical Only Chiropractic'


class CanonicalOnlyAdminRenderTests(TestCase):
    """No ClientProfile, no Project — anywhere in this fixture."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username='rendersweep', email='rendersweep@example.com',
            password='test-pass-123')

        owner = User.objects.create_user(
            username='canonicalowner', email='owner@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(
            user=owner, name=SITE_NAME, contact_name='Dana Reyes',
            phone='210-555-0100', stripe_customer_id='cus_render_sweep')
        cls.website = Website.objects.create(
            account=cls.account, name=SITE_NAME,
            url='https://canonical-only.example.com',
            package='essential_build', stage='live',
            payment_status='fully_paid', do_droplet_ip='203.0.113.7')

        # Guard the premise. If a signal quietly manufactures a legacy row
        # the whole fixture stops testing what it claims to.
        assert cls.account.legacy_client_profile_id is None
        assert cls.website.legacy_project_id is None

        cls._build_dependent_rows()

    @classmethod
    def _build_dependent_rows(cls):
        from admin_dashboard.models import AIAssistantLog
        from clients.models import (
            AnnualReport,
            CompetitorGapReport,
            IntelligenceSuggestion,
            ReferralLink,
            SiteChangelogEntry,
        )
        from domains.models import DomainRegistration
        from reporting.models import (
            MonthlyReport,
            NPSSurvey,
            VulnerabilityScan,
        )

        month = datetime.date.today().replace(day=1)

        cls.annual = AnnualReport.objects.create(
            website_new=cls.website, report_year=month.year - 1,
            status='ready')
        cls.gap = CompetitorGapReport.objects.create(
            website_new=cls.website, report_month=month, status='ready')
        cls.suggestion = IntelligenceSuggestion.objects.create(
            website_new=cls.website, title='Add a booking page',
            description='They have no way to book online.')
        cls.scan = VulnerabilityScan.objects.create(
            website_new=cls.website, scan_type='full', status='complete')
        cls.domain = DomainRegistration.objects.create(
            account_new=cls.account, domain_name='canonical-only',
            tld='com', status='active')

        MonthlyReport.objects.create(
            website_new=cls.website, report_month=month)
        NPSSurvey.objects.create(website_new=cls.website, score=9)
        ReferralLink.objects.create(
            account_new=cls.account, code=uuid.uuid4().hex[:10])
        SiteChangelogEntry.objects.create(
            website_new=cls.website, title='Swapped the hero image')
        AIAssistantLog.objects.create(
            website_new=cls.website, intent='move_stage', success=True)

        # The Needs You queue: a site awaiting intake review.
        Website.objects.filter(pk=cls.website.pk).update(
            needs_admin_review_at=timezone.now(), admin_reviewed_at=None)

    def setUp(self):
        self.client.force_login(self.admin)

    def _assert_renders(self, url, expect_name=True):
        response = self.client.get(url, follow=True)
        self.assertEqual(
            response.status_code, 200,
            f'{url} did not render for a client with no legacy row')
        if expect_name:
            self.assertContains(
                response, SITE_NAME,
                msg_prefix=(
                    f'{url} returned 200 but the owner name is missing. '
                    'That is the silent form: the template read the name '
                    'through the legacy FK, Django resolved it to the '
                    'empty string, and the cell rendered blank.'))

    # ── list pages ──────────────────────────────────────────────────

    def test_annual_reports_list(self):
        self._assert_renders(reverse('admin_dashboard:annual_reports_list'))

    def test_competitor_gaps_list(self):
        self._assert_renders(reverse('admin_dashboard:competitor_gaps_list'))

    def test_intelligence_suggestions_list(self):
        self._assert_renders(
            reverse('admin_dashboard:intelligence_suggestions'))

    def test_nps_list(self):
        self._assert_renders(reverse('admin_dashboard:nps_list'))

    def test_reports_list(self):
        self._assert_renders(reverse('admin_dashboard:reports_list'))

    def test_referrals_list(self):
        self._assert_renders(reverse('admin_dashboard:referrals_list'))

    def test_scans_list(self):
        self._assert_renders(reverse('admin_dashboard:scans_list'))

    def test_changelog_list(self):
        self._assert_renders(reverse('admin_dashboard:changelog_list'))

    def test_ai_assistant(self):
        self._assert_renders(reverse('admin_dashboard:ai_assistant'))

    def test_needs_you(self):
        """The one the staging sweep caught: `select_related('user',
        'intake')` left over from ClientProfile made this 500 outright."""
        self._assert_renders(reverse('admin_dashboard:needs_you'))

    # ── detail pages ────────────────────────────────────────────────

    def test_annual_report_detail(self):
        self._assert_renders(reverse(
            'admin_dashboard:annual_report_detail',
            args=[self.annual.id]))

    def test_competitor_gap_detail(self):
        self._assert_renders(reverse(
            'admin_dashboard:competitor_gap_detail', args=[self.gap.id]))

    def test_intelligence_suggestion_detail(self):
        self._assert_renders(reverse(
            'admin_dashboard:intelligence_suggestion_detail',
            args=[self.suggestion.id]))

    def test_scan_detail(self):
        self._assert_renders(reverse(
            'admin_dashboard:scan_detail', args=[self.scan.id]))

    # ── domains ─────────────────────────────────────────────────────
    #
    # DomainRegistration is the odd one: its canonical owner is the
    # Account, but the page shows the *droplet IP*, which is a Website
    # fact. `owner_site` reaches it through `pointed_at_website`, so
    # these also cover a registration that is not pointed anywhere yet.

    def test_domains_list(self):
        self._assert_renders(reverse('admin_dashboard:admin_domain_list'))

    def test_domain_detail(self):
        self._assert_renders(reverse(
            'admin_dashboard:admin_domain_detail',
            kwargs={'reg_id': self.domain.id}))


class UnpointedDomainTests(TestCase):
    """A domain registered but not yet pointed at a build.

    `owner_site` returns None here, which is exactly the case that must
    not reach `{% url %}`. Real: a client buys the domain during
    onboarding, weeks before the site exists.
    """

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username='unpointed', email='unpointed@example.com',
            password='test-pass-123')
        owner = User.objects.create_user(
            username='nodomainsite', email='nodomainsite@example.com',
            password='test-pass-123')
        cls.account = Account.objects.create(
            user=owner, name='Domain Only Ltd')

        from domains.models import DomainRegistration

        cls.domain = DomainRegistration.objects.create(
            account_new=cls.account, domain_name='not-pointed-yet',
            tld='com', status='active')

    def setUp(self):
        self.client.force_login(self.admin)

    def test_the_list_renders_without_a_site(self):
        response = self.client.get(
            reverse('admin_dashboard:admin_domain_list'), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'not-pointed-yet')

    def test_the_detail_page_renders_without_a_site(self):
        response = self.client.get(
            reverse('admin_dashboard:admin_domain_detail',
                    kwargs={'reg_id': self.domain.id}), follow=True)
        self.assertEqual(response.status_code, 200)
