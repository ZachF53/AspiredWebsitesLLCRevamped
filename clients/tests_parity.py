"""Representative structural tests for the Phase-D parity auditor."""

from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from clients.account_models import Account, Website
from clients.account_setup import AccountSetupError, ensure_account
from clients.models import ClientProfile, Project
from clients.parity import audit_account_website_parity


User = get_user_model()


class AccountWebsiteParityTests(TestCase):
    def _profile(self, suffix, **kwargs):
        user = User.objects.create_user(
            username=f'parity-{suffix}',
            email=f'parity-{suffix}@example.com',
            password='test-pass-123',
        )
        return ClientProfile.objects.create(
            user=user,
            firm_name=f'Parity {suffix}',
            **kwargs,
        )

    def test_signal_created_account_and_website_are_clean(self):
        self._profile('clean')

        report = audit_account_website_parity()

        self.assertEqual(report.error_count, 0, report.as_dict())
        self.assertEqual(report.warning_count, 0, report.as_dict())

    def test_account_only_vault_profile_is_clean(self):
        user = User.objects.create_user(
            username='parity-vault-only',
            email='vault-only@example.com',
            password='test-pass-123',
        )
        ClientProfile.objects.create(user=user, firm_name='')

        report = audit_account_website_parity()

        self.assertEqual(report.error_count, 0, report.as_dict())
        self.assertEqual(report.warning_count, 0, report.as_dict())

    def test_missing_account_is_a_structural_error(self):
        profile = self._profile('missing-account')
        profile.migrated_account.delete()

        report = audit_account_website_parity()

        finding = next(item for item in report.findings
                       if item.code == 'client-profile-missing-account')
        self.assertEqual(finding.count, 1)
        self.assertGreater(report.error_count, 0)

    def test_unmapped_legacy_project_is_a_structural_error(self):
        profile = self._profile('project')
        Project.objects.create(client=profile)

        report = audit_account_website_parity()

        finding = next(item for item in report.findings
                       if item.code == 'legacy-project-missing-website')
        self.assertEqual(finding.count, 1)

    def test_duplicate_stripe_customer_is_an_error(self):
        first = self._profile('stripe-one')
        second = self._profile('stripe-two')
        Account.objects.filter(pk__in=[
            first.migrated_account.pk,
            second.migrated_account.pk,
        ]).update(stripe_customer_id='cus_duplicate')

        report = audit_account_website_parity()

        finding = next(
            item for item in report.findings
            if item.code == 'clients.Account.stripe_customer_id-duplicate')
        self.assertEqual(finding.count, 1)

    def test_multi_website_legacy_account_requires_manual_review(self):
        profile = self._profile('multi-site')
        Website.objects.create(
            account=profile.migrated_account,
            name='Second Parity Site',
        )

        report = audit_account_website_parity()

        finding = next(
            item for item in report.findings
            if item.code == 'multi-website-manual-review')
        self.assertEqual(finding.count, 1)

    def test_strict_management_command_fails_on_errors(self):
        profile = self._profile('strict')
        profile.migrated_account.delete()

        with self.assertRaises(CommandError):
            call_command(
                'audit_account_website_parity',
                strict=True,
                stdout=StringIO(),
                stderr=StringIO(),
            )


class MultiWebsiteReviewTests(TestCase):
    """The multi-website warning must clear on an explicit mapping, and
    re-open when a new site lands after that mapping."""

    def _account_with_two_sites(self):
        user = User.objects.create_user(
            username='review-user', email='review@example.com',
            password='test-pass-123')
        profile = ClientProfile.objects.create(
            user=user, firm_name='Review Firm')
        account = profile.migrated_account
        Website.objects.create(account=account, name='Second Review Site')
        return account

    def _multi_website_findings(self):
        return [
            item for item in audit_account_website_parity().findings
            if item.code == 'multi-website-manual-review'
        ]

    def test_recorded_review_clears_the_warning(self):
        account = self._account_with_two_sites()
        self.assertTrue(self._multi_website_findings())

        account.multi_website_reviewed_at = timezone.now()
        account.multi_website_review_note = 'Mapped by hand.'
        account.save(update_fields=[
            'multi_website_reviewed_at', 'multi_website_review_note',
            'updated_at'])

        self.assertFalse(self._multi_website_findings())

    def test_website_added_after_review_reopens_the_warning(self):
        account = self._account_with_two_sites()
        account.multi_website_reviewed_at = timezone.now()
        account.save(update_fields=[
            'multi_website_reviewed_at', 'updated_at'])
        self.assertFalse(self._multi_website_findings())

        Website.objects.create(
            account=account, name='Third Site',
            created_at=timezone.now() + timedelta(minutes=1))

        self.assertTrue(self._multi_website_findings())


class BackfillIdempotencyTests(TestCase):
    """A second backfill pass must not change anything — including
    updated_at, which the Moonieful bridge compares to decide staleness."""

    def setUp(self):
        user = User.objects.create_user(
            username='backfill-user', email='backfill@example.com',
            password='test-pass-123')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Backfill Firm',
            package='essential_build', stage='design',
            payment_status='deposit_paid')
        Project.objects.create(
            client=self.profile, package='essential_build', stage='design')

    def _run_backfills(self):
        for command in ('refactor_to_accounts',):
            call_command(command, stdout=StringIO(), stderr=StringIO())
        call_command('backfill_website_fks', apply=True,
                     stdout=StringIO(), stderr=StringIO())
        call_command('backfill_account_data', apply=True,
                     stdout=StringIO(), stderr=StringIO())

    def test_second_pass_writes_nothing(self):
        self._run_backfills()
        account = Account.objects.get(legacy_client_profile=self.profile)
        website = account.websites.get()
        before = (account.updated_at, website.updated_at)
        website_count = Website.objects.count()

        self._run_backfills()

        account.refresh_from_db()
        website.refresh_from_db()
        self.assertEqual((account.updated_at, website.updated_at), before)
        self.assertEqual(Website.objects.count(), website_count)

    def test_backfill_adopts_the_signal_created_website(self):
        """The signal creates a Website with no legacy_project; the backfill
        must adopt it rather than create a duplicate on a -2 slug."""
        original = Account.objects.get(
            legacy_client_profile=self.profile).websites.get()

        self._run_backfills()

        websites = Website.objects.filter(account__legacy_client_profile=
                                          self.profile)
        self.assertEqual(websites.count(), 1)
        adopted = websites.get()
        self.assertEqual(adopted.pk, original.pk)
        self.assertEqual(adopted.slug, original.slug)
        self.assertIsNotNone(adopted.legacy_project_id)


class AccountSetupTests(TestCase):
    """ensure_account has to hold the invariant the cutover depends on:
    one Account per legacy profile, owned by the same user."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='setup-user', email='setup@example.com',
            password='test-pass-123')

    def test_returns_the_existing_linked_account(self):
        profile = ClientProfile.objects.create(
            user=self.user, firm_name='Setup Firm')
        self.assertEqual(
            ensure_account(profile).pk, profile.migrated_account.pk)

    def test_creates_the_account_when_the_signal_did_not(self):
        profile = ClientProfile(user=self.user, firm_name='Orphan Firm')
        profile._skip_autocreate = True
        profile.save()
        self.assertFalse(
            Account.objects.filter(legacy_client_profile=profile).exists())

        account = ensure_account(profile)

        self.assertEqual(account.user_id, self.user.pk)
        self.assertEqual(account.legacy_client_profile_id, profile.pk)
        self.assertEqual(account.name, 'Orphan Firm')
        self.assertFalse(audit_account_website_parity().findings)

    def test_links_an_unlinked_account_owned_by_the_same_user(self):
        account = Account.objects.create(user=self.user, name='Loose Account')
        profile = ClientProfile(user=self.user, firm_name='Loose Firm')
        profile._skip_autocreate = True
        profile.save()

        resolved = ensure_account(profile)

        self.assertEqual(resolved.pk, account.pk)
        self.assertEqual(resolved.legacy_client_profile_id, profile.pk)
        self.assertEqual(Account.objects.count(), 1)

    def test_user_conflict_is_raised_not_guessed(self):
        profile = ClientProfile.objects.create(
            user=self.user, firm_name='Conflict Firm')
        other = User.objects.create_user(
            username='other-user', email='other@example.com',
            password='test-pass-123')
        Account.objects.filter(legacy_client_profile=profile).update(
            user=other)

        with self.assertRaises(AccountSetupError):
            ensure_account(profile.__class__.objects.get(pk=profile.pk))

    def test_account_onboarding_status_reflects_the_profile(self):
        profile = ClientProfile.objects.create(
            user=self.user, firm_name='Status Firm',
            onboarding_status='onboarding_complete')
        self.assertEqual(
            profile.migrated_account.onboarding_status, 'complete')
