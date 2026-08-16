"""
Write-time canonical FK stamping.

The failure this prevents is silent: a row saved with only its legacy
`client` FK is invisible to every canonical reader, and nothing errors.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from clients.account_models import Website
from clients.models import (
    ClientProfile, OnboardingToken, Project, ReferralLink, SupportTicket)


User = get_user_model()


class CanonicalStampingTests(TestCase):

    def setUp(self):
        user = User.objects.create_user(
            username='stamp', email='stamp@example.com',
            password='test-pass-123')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Stamping Firm', package='essential_build')
        self.account = self.profile.migrated_account
        self.website = self.account.websites.get()

    def test_account_fk_is_stamped_from_the_legacy_client(self):
        link = ReferralLink.objects.create(
            client=self.profile, code='STAMP-1')

        self.assertEqual(link.account_new_id, self.account.pk)
        link.refresh_from_db()
        self.assertEqual(link.account_new_id, self.account.pk)

    def test_website_fk_is_stamped_when_the_account_owns_one_site(self):
        ticket = SupportTicket.objects.create(
            client=self.profile, subject='Stamped', description='x')

        self.assertEqual(ticket.website_new_id, self.website.pk)
        self.assertEqual(ticket.account_new_id, self.account.pk)

    def test_project_fk_wins_over_the_account_default(self):
        """A row that names its own project must follow that project's
        Website, not whichever site happens to be the only one."""
        second_project = Project.objects.create(
            client=self.profile, stage='review')
        second_site = Website.objects.create(
            account=self.account, name='Second Stamped Site',
            legacy_project=second_project)

        ticket = SupportTicket.objects.create(
            client=self.profile, project=second_project,
            subject='Follows its project', description='x')

        self.assertEqual(ticket.website_new_id, second_site.pk)

    def test_multi_website_account_is_left_null_not_guessed(self):
        """No project FK and several sites means there is no correct
        answer. Guessing is the silent mis-filing the cutover forbids."""
        Website.objects.create(
            account=self.account, name='Another Stamped Site')

        link = ReferralLink.objects.create(
            client=self.profile, code='STAMP-2')
        ticket = SupportTicket.objects.create(
            client=self.profile, subject='Ambiguous', description='x')

        # Account is unambiguous and still gets stamped.
        self.assertEqual(link.account_new_id, self.account.pk)
        self.assertEqual(ticket.account_new_id, self.account.pk)
        # Website is not.
        self.assertIsNone(ticket.website_new_id)

    def test_an_explicitly_set_canonical_fk_is_never_overwritten(self):
        other_site = Website.objects.create(
            account=self.account, name='Explicit Site')

        ticket = SupportTicket.objects.create(
            client=self.profile, website_new=other_site,
            subject='Explicit', description='x')

        self.assertEqual(ticket.website_new_id, other_site.pk)

    def test_onboarding_token_is_stamped(self):
        token = OnboardingToken.objects.create(client=self.profile)
        self.assertEqual(token.account_new_id, self.account.pk)

    def test_stamping_survives_a_profile_with_no_account(self):
        """A pre-signal orphan must not break unrelated writes."""
        user = User.objects.create_user(
            username='orphan-stamp', email='orphan@example.com',
            password='test-pass-123')
        orphan = ClientProfile(user=user, firm_name='Orphan Firm')
        orphan._skip_autocreate = True
        orphan.save()

        link = ReferralLink.objects.create(client=orphan, code='STAMP-3')
        self.assertIsNone(link.account_new_id)

    def test_payment_record_is_stamped(self):
        from clients.models import PaymentRecord

        payment = PaymentRecord.objects.create(
            client=self.profile, kind='deposit', amount=Decimal('100.00'),
            stripe_id='pi_stamp_test', paid_at=timezone.now())

        self.assertEqual(payment.account_id, self.account.pk)
        self.assertEqual(payment.website_id, self.website.pk)

    def test_the_plan_covers_the_models_that_matter(self):
        from clients import canonical_stamping

        labels = {m._meta.label for m in canonical_stamping.build_plan()}
        for expected in (
                'clients.SupportTicket', 'clients.ReferralLink',
                'clients.PaymentRecord', 'clients.ClientDocument',
                'clients.RevisionRequest', 'reporting.NPSSurvey',
                'billing.MiniInvoice', 'sync.SyncJob'):
            with self.subTest(model=expected):
                self.assertIn(expected, labels)
