"""
Regression cover for the Account/Website migration tooling.

Every test here corresponds to a defect the rehearsals actually found. They
exist because the parity validator cannot detect any of them after the
fact: a wrong-but-populated FK is indistinguishable from a right one, and a
field overwritten with a blank looks exactly like a field that was always
blank.
"""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from clients.account_models import Account, Website
from clients.models import ClientProfile, Project
from clients.parity import audit_account_website_parity


User = get_user_model()


class BackfillNonDestructiveTests(TestCase):
    """The real-data rehearsal caught the backfill reverting a live, paying
    client to awaiting_deposit and erasing its URL.

    A backfill fills gaps. It must never overwrite a populated canonical
    value with a blank or stale legacy one.
    """

    def setUp(self):
        user = User.objects.create_user(
            username='nondestructive', email='nd@example.com',
            password='test-pass-123')
        # A stale, largely empty legacy profile — the state real profiles
        # reach once the Website becomes the thing people actually edit.
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Whitehead Regression',
            business_type='', website='', package='',
            stage='design', payment_status='awaiting_deposit',
            revision_count=0)
        self.website = self.profile.migrated_account.websites.get()
        Project.objects.create(client=self.profile, stage='design')

        # The canonical row carries the real, current values.
        Website.objects.filter(pk=self.website.pk).update(
            business_type='Health and Wellness',
            url='https://whiteheadwellness.com',
            package='premium_build',
            payment_status='fully_paid',
            revision_count=1,
            maintenance_active=True,
            session_recording_enabled=True,
            do_droplet_name='whitehead-wellness-prod',
        )
        self.website.refresh_from_db()

    def _backfill(self):
        call_command('refactor_to_accounts',
                     stdout=StringIO(), stderr=StringIO())
        call_command('backfill_website_fks', apply=True,
                     stdout=StringIO(), stderr=StringIO())
        self.website.refresh_from_db()

    def test_populated_canonical_fields_survive_the_backfill(self):
        self._backfill()

        self.assertEqual(self.website.payment_status, 'fully_paid')
        self.assertEqual(self.website.url, 'https://whiteheadwellness.com')
        self.assertEqual(self.website.package, 'premium_build')
        self.assertEqual(self.website.business_type, 'Health and Wellness')
        self.assertEqual(self.website.revision_count, 1)

    def test_booleans_are_values_not_emptiness(self):
        """False and 0 are choices somebody made. A stale legacy row must
        not flip them just because they look falsy."""
        self._backfill()

        self.assertTrue(self.website.maintenance_active)
        self.assertTrue(self.website.session_recording_enabled)

    def test_do_droplet_name_is_preserved(self):
        """ClientProfile has no such column and the mapping supplies '' —
        which wiped the droplet name on nine production sites."""
        self._backfill()

        self.assertEqual(
            self.website.do_droplet_name, 'whitehead-wellness-prod')

    def test_legacy_values_still_fill_genuinely_empty_fields(self):
        """Filling gaps is the backfill's actual job. This must keep
        working, or the non-destructive fix would just disable it."""
        stamp = timezone.now()
        ClientProfile.objects.filter(pk=self.profile.pk).update(
            staging_url='https://staging.whitehead.com',
            needs_admin_review_at=stamp)
        Website.objects.filter(pk=self.website.pk).update(
            staging_url='', needs_admin_review_at=None)

        self._backfill()

        self.assertEqual(
            self.website.staging_url, 'https://staging.whitehead.com')
        self.assertIsNotNone(self.website.needs_admin_review_at)

    def test_second_pass_writes_nothing_and_preserves_updated_at(self):
        """The Moonieful bridge decides inbound staleness by comparing
        updated_at, so a backfill that touches every row on every run
        suppresses legitimate inbound updates."""
        self._backfill()
        account = self.profile.migrated_account
        account.refresh_from_db()
        before = (self.website.updated_at, account.updated_at)

        self._backfill()
        account.refresh_from_db()

        self.assertEqual(self.website.updated_at, before[0])
        self.assertEqual(account.updated_at, before[1])

    def test_backfill_adopts_rather_than_duplicating_the_website(self):
        """The autocreate signal leaves legacy_project null, which the old
        idempotency key never matched — doubling the table and pushing the
        new row onto a -2 slug, which is the portal URL."""
        original_pk, original_slug = self.website.pk, self.website.slug

        self._backfill()

        sites = Website.objects.filter(account=self.profile.migrated_account)
        self.assertEqual(sites.count(), 1)
        self.assertEqual(sites.get().pk, original_pk)
        self.assertEqual(sites.get().slug, original_slug)


class MultiWebsiteAllocationTests(TestCase):
    """The cutover contract forbids attaching legacy rows to the oldest
    Website. Rows resolve through their own project FK, or stay null for a
    human — they are never guessed."""

    def setUp(self):
        user = User.objects.create_user(
            username='alloc', email='alloc@example.com',
            password='test-pass-123')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Vance Regression', stage='live',
            payment_status='fully_paid')
        self.account = self.profile.migrated_account
        self.first_project = Project.objects.create(
            client=self.profile, stage='live')
        self.second_project = Project.objects.create(
            client=self.profile, stage='review')

        self.older = self.account.websites.get()
        Website.objects.filter(pk=self.older.pk).update(
            legacy_project=self.first_project,
            created_at=timezone.now() - timedelta(days=200))
        self.newer = Website.objects.create(
            account=self.account, name='Vance Mediation Regression',
            legacy_project=self.second_project,
            created_at=timezone.now() - timedelta(days=5))
        self.older.refresh_from_db()

    def _backfill(self):
        call_command('refactor_to_accounts',
                     stdout=StringIO(), stderr=StringIO())
        call_command('backfill_website_fks', apply=True,
                     stdout=StringIO(), stderr=StringIO())

    def test_row_follows_its_project_not_the_oldest_website(self):
        from clients.models import SupportTicket

        ticket = SupportTicket.objects.create(
            client=self.profile, project=self.second_project,
            subject='Mediation intake broken', description='x')

        self._backfill()

        ticket.refresh_from_db()
        self.assertEqual(ticket.website_new_id, self.newer.pk)
        self.assertNotEqual(ticket.website_new_id, self.older.pk)

    def test_unresolvable_row_is_left_null_rather_than_guessed(self):
        """A PaymentRecord carries no project FK. On a multi-website
        account nothing can infer its site, so it must stay null and keep
        surfacing until somebody maps it."""
        from clients.models import PaymentRecord

        payment = PaymentRecord.objects.create(
            client=self.profile, kind='deposit', amount=Decimal('2250.00'),
            stripe_id='pi_alloc_regression', paid_at=timezone.now())

        self._backfill()

        payment.refresh_from_db()
        self.assertIsNone(payment.website_id)
        self.assertEqual(payment.account_id, self.account.pk)


class FixtureRawLoadTests(TestCase):
    """Restoring a snapshot must not re-run business logic. Without the raw
    guards, loading a fixture tries to create a second Account for a user
    who is about to get theirs from that same fixture."""

    def _load(self, fields, pk):
        import json
        import tempfile
        from pathlib import Path

        fixture = [{
            'model': 'clients.clientprofile',
            'pk': str(pk),
            'fields': fields,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'raw.json'
            path.write_text(json.dumps(fixture), encoding='utf-8')
            call_command('loaddata', str(path),
                         stdout=StringIO(), stderr=StringIO())

    def test_raw_load_creates_no_related_records(self):
        user = User.objects.create_user(
            username='rawload', email='rawload@example.com',
            password='test-pass-123')
        profile = ClientProfile.objects.create(
            user=user, firm_name='Raw Load Firm')
        account_id = profile.migrated_account.pk
        website_id = profile.migrated_account.websites.get().pk

        self._load({
            'user': user.pk,
            'firm_name': 'Raw Load Firm Renamed',
            'created_at': profile.created_at.isoformat(),
            'updated_at': profile.updated_at.isoformat(),
        }, profile.pk)

        self.assertEqual(Account.objects.filter(user=user).count(), 1)
        self.assertEqual(Account.objects.get(user=user).pk, account_id)
        self.assertEqual(
            Website.objects.filter(account_id=account_id).count(), 1)
        self.assertEqual(
            Website.objects.get(account_id=account_id).pk, website_id)

        from vault.models import ClientVault
        self.assertEqual(
            ClientVault.objects.filter(client=profile).count(), 1)

    def test_raw_load_does_not_mutate_the_existing_account(self):
        user = User.objects.create_user(
            username='rawmutate', email='rawmutate@example.com',
            password='test-pass-123')
        profile = ClientProfile.objects.create(
            user=user, firm_name='Raw Mutate Firm')
        account = profile.migrated_account
        before_name, before_stamp = account.name, account.updated_at

        self._load({
            'user': user.pk,
            'firm_name': 'Completely Different Name',
            'phone': '210-555-0000',
            'created_at': profile.created_at.isoformat(),
            'updated_at': profile.updated_at.isoformat(),
        }, profile.pk)

        account.refresh_from_db()
        self.assertEqual(account.name, before_name)
        self.assertEqual(account.updated_at, before_stamp)

    def test_raw_load_does_not_queue_a_moonieful_sync(self):
        from sync.models import SyncJob

        user = User.objects.create_user(
            username='rawsync', email='rawsync@example.com',
            password='test-pass-123')
        profile = ClientProfile.objects.create(
            user=user, firm_name='Raw Sync Firm', stage='design')
        SyncJob.objects.all().delete()

        self._load({
            'user': user.pk,
            'firm_name': 'Raw Sync Firm',
            'stage': 'content',
            'created_at': profile.created_at.isoformat(),
            'updated_at': profile.updated_at.isoformat(),
        }, profile.pk)

        self.assertEqual(SyncJob.objects.count(), 0)


class LedgerEvidenceTests(TestCase):
    """`fully_paid` releases the launch gate, so it has to be corroborated
    by something in the ledger — or explicitly verified by a person."""

    def setUp(self):
        user = User.objects.create_user(
            username='ledger', email='ledger@example.com',
            password='test-pass-123')
        self.profile = ClientProfile.objects.create(
            user=user, firm_name='Ledger Firm', stage='pre_launch',
            payment_status='fully_paid')
        self.website = self.profile.migrated_account.websites.get()
        Website.objects.filter(pk=self.website.pk).update(
            payment_status='fully_paid')
        self.website.refresh_from_db()

    def test_audit_reports_unverified_payment_as_operational(self):
        report = audit_account_website_parity()

        finding = next(
            item for item in report.findings
            if item.code == 'website-fully-paid-without-ledger-evidence')
        self.assertEqual(finding.severity, 'operational')
        self.assertEqual(report.operational_count, 1)
        # Real money is not a schema problem — it must not block the gate.
        self.assertEqual(report.error_count, 0)

    def test_ledger_evidence_clears_the_finding(self):
        from clients.models import PaymentRecord

        PaymentRecord.objects.create(
            client=self.profile, account=self.profile.migrated_account,
            website=self.website, kind='final', amount=Decimal('2500.00'),
            stripe_id='pi_ledger_evidence', paid_at=timezone.now())

        codes = [f.code for f in audit_account_website_parity().findings]
        self.assertNotIn(
            'website-fully-paid-without-ledger-evidence', codes)

    def test_launch_is_blocked_without_evidence(self):
        from clients.services import GuardError, mark_live

        with self.assertRaises(GuardError) as ctx:
            mark_live(self.profile)

        self.assertIn('fully_paid', str(ctx.exception))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.stage, 'pre_launch')

    def test_operator_can_launch_after_verifying_manually(self):
        from clients.services import mark_live

        mark_live(self.profile, payment_verified=True)

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.stage, 'live')

    def test_operator_attestation_settles_it_permanently(self):
        """A payment made outside Stripe is real. Recording that a named
        person confirmed it must clear the finding and open the launch
        gate — without fabricating a PaymentRecord."""
        from clients.models import PaymentRecord
        from clients.services import mark_live

        call_command('verify_website_payment', self.website.slug,
                     by='Zachery Long', note='Paid outside Stripe',
                     apply=True, stdout=StringIO())

        self.website.refresh_from_db()
        self.assertIsNotNone(self.website.payment_verified_at)
        self.assertEqual(self.website.payment_verified_by, 'Zachery Long')

        # The audit stops reporting it.
        codes = [f.code for f in audit_account_website_parity().findings]
        self.assertNotIn(
            'website-fully-paid-without-ledger-evidence', codes)

        # No transaction was invented to achieve that.
        self.assertEqual(PaymentRecord.objects.count(), 0)

        # And the launch gate now lets it through unaided.
        mark_live(self.profile)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.stage, 'live')

    def test_attestation_requires_a_name(self):
        """An attestation with nobody attached is not evidence."""
        with self.assertRaises(CommandError):
            call_command('verify_website_payment', self.website.slug,
                         apply=True, stdout=StringIO(), stderr=StringIO())

    def test_attestation_is_idempotent(self):
        call_command('verify_website_payment', self.website.slug,
                     by='Zachery Long', apply=True, stdout=StringIO())
        self.website.refresh_from_db()
        first = self.website.payment_verified_at

        out = StringIO()
        call_command('verify_website_payment', self.website.slug,
                     by='Someone Else', apply=True, stdout=out)
        self.website.refresh_from_db()
        self.assertEqual(self.website.payment_verified_at, first)
        self.assertEqual(self.website.payment_verified_by, 'Zachery Long')
        self.assertIn('Already verified', out.getvalue())

    def test_strict_gate_ignores_operational_items_by_default(self):
        call_command('audit_account_website_parity', strict=True,
                     fail_on_warnings=True,
                     stdout=StringIO(), stderr=StringIO())

        with self.assertRaises(CommandError):
            call_command('audit_account_website_parity', strict=True,
                         fail_on_operational=True,
                         stdout=StringIO(), stderr=StringIO())
