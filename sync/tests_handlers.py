"""
The inbound Moonieful bridge.

These handlers had no test coverage at all — the existing sync tests
cover HMAC signatures and handoff tokens, but nothing that actually
materialises a client. That is the riskiest gap in the app: the handlers
are the only writer for data owned by an outside party, and a mistake
here is discovered by Miki's client rather than by us.

Written alongside the Account/Website conversion, so they pin the field
ownership CLAUDE.md specifies:

    Moonieful owns  email, name, business info, intake answers  -> Account
    Aspired owns    project stages, revisions, maintenance      -> Website
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from clients.account_models import Account, Website
from clients.models import ClientDocument, IntakeResponse, ProjectStageLog

User = get_user_model()


def _bundle(**over):
    data = {
        'client': {
            'id': '11111111-1111-1111-1111-111111111111',
            'email': 'moon@example.com',
            'name': 'Dana Moon',
            'firm_name': 'Moon Studio',
            'phone': '210-555-0142',
            'website': 'https://moonstudio.example',
            'package': 'Brand + Site',
        },
        'intake': {'brand_colours': 'sage, cream'},
        'stage_history': [{'stage': 'brand', 'at': '2026-01-02'}],
        'documents': [],
    }
    data.update(over)
    return data


class ClientCreatedTests(TestCase):

    def test_creates_one_account_and_one_website(self):
        from sync.handlers import handle_client_created

        account = handle_client_created(_bundle())

        self.assertEqual(Account.objects.count(), 1)
        self.assertEqual(account.websites.count(), 1)
        self.assertEqual(Website.objects.count(), 1)

    def test_account_carries_the_moonieful_identity(self):
        """Identity is account-level: Moonieful owns it."""
        from sync.handlers import handle_client_created

        account = handle_client_created(_bundle())

        self.assertEqual(account.name, 'Moon Studio')
        self.assertEqual(account.contact_name, 'Dana Moon')
        self.assertEqual(account.phone, '210-555-0142')
        self.assertTrue(account.synced_from_moonieful)
        self.assertEqual(
            str(account.moonieful_client_id),
            '11111111-1111-1111-1111-111111111111')

    def test_website_carries_the_build_state(self):
        """Stage and package are Aspired's, and per site."""
        from sync.handlers import handle_client_created

        site = handle_client_created(_bundle()).websites.first()

        self.assertEqual(site.stage, 'intake')
        self.assertEqual(site.package, 'moonieful_referred')
        self.assertTrue(site.moonieful_referred)
        self.assertEqual(site.moonieful_package, 'Brand + Site')
        self.assertEqual(site.url, 'https://moonstudio.example')

    def test_business_type_is_never_the_law_firm_default(self):
        """CLAUDE.md: business_type is blank for Moonieful clients and set
        by hand. Inheriting the default would put law-firm phrasing into a
        brand studio's copy and prompts."""
        from sync.handlers import handle_client_created

        site = handle_client_created(_bundle()).websites.first()
        self.assertEqual(site.business_type, '')

    def test_intake_attaches_to_the_website(self):
        from sync.handlers import handle_client_created

        site = handle_client_created(_bundle()).websites.first()
        intake = IntakeResponse.objects.get(website_new=site)
        self.assertEqual(
            intake.moonieful_intake_raw, {'brand_colours': 'sage, cream'})

    def test_a_second_delivery_does_not_duplicate_the_website(self):
        """Sync jobs retry. The handler must be idempotent, or a retry
        leaves the client with two sites and the portal showing a chooser
        they should never see."""
        from sync.handlers import handle_client_created

        handle_client_created(_bundle())
        handle_client_created(_bundle())

        self.assertEqual(Account.objects.count(), 1)
        self.assertEqual(Website.objects.count(), 1)
        self.assertEqual(IntakeResponse.objects.count(), 1)

    def test_an_existing_user_is_linked_and_flagged_not_overwritten(self):
        from sync.handlers import handle_client_created

        User.objects.create_user(
            username='existing', email='moon@example.com', password='x')
        account = handle_client_created(_bundle())

        self.assertTrue(account.sync_conflict_flagged)
        self.assertEqual(User.objects.filter(
            email__iexact='moon@example.com').count(), 1)

    def test_documents_attach_to_the_website(self):
        from sync.handlers import handle_client_created

        bundle = _bundle(documents=[
            {'id': '22222222-2222-2222-2222-222222222222',
             'label': 'Brand guide'}])
        site = handle_client_created(bundle).websites.first()

        doc = ClientDocument.objects.get(website_new=site)
        self.assertEqual(doc.label, 'Brand guide')
        self.assertEqual(doc.direction, 'to_client')

    def test_a_bundle_with_no_email_is_rejected(self):
        from sync.handlers import handle_client_created

        bundle = _bundle()
        bundle['client']['email'] = ''
        with self.assertRaises(ValueError):
            handle_client_created(bundle)


class ClientUpdatedTests(TestCase):

    def setUp(self):
        from sync.handlers import handle_client_created
        self.account = handle_client_created(_bundle())

    def test_updates_the_account_and_the_site_url(self):
        from sync.handlers import handle_client_updated

        bundle = _bundle()
        bundle['client']['firm_name'] = 'Moon Studio Co'
        bundle['client']['website'] = 'https://new.example'
        handle_client_updated(bundle)

        self.account.refresh_from_db()
        self.assertEqual(self.account.name, 'Moon Studio Co')
        self.assertEqual(
            self.account.websites.first().url, 'https://new.example')

    def test_an_unknown_moonieful_id_raises(self):
        from sync.handlers import handle_client_updated

        bundle = _bundle()
        bundle['client']['id'] = '99999999-9999-9999-9999-999999999999'
        with self.assertRaises(ValueError):
            handle_client_updated(bundle)

    def test_a_stale_update_is_skipped(self):
        """Staleness check: a bundle older than our row does not win."""
        from sync.handlers import handle_client_updated

        bundle = _bundle()
        bundle['client']['firm_name'] = 'Should Not Apply'
        bundle['updated_at'] = '2020-01-01T00:00:00+00:00'
        handle_client_updated(bundle)

        self.account.refresh_from_db()
        self.assertEqual(self.account.name, 'Moon Studio')


class ProjectCompleteTests(TestCase):

    def setUp(self):
        from sync.handlers import handle_client_created
        self.account = handle_client_created(_bundle())

    def test_moves_the_website_live_and_stamps_the_handoff(self):
        from sync.handlers import handle_project_complete

        handle_project_complete(_bundle())

        site = self.account.websites.first()
        site.refresh_from_db()
        self.assertEqual(site.stage, 'live')
        self.assertIsNotNone(site.moonieful_handoff_at)

    def test_logs_the_stage_change_against_the_website(self):
        from sync.handlers import handle_project_complete

        handle_project_complete(_bundle())

        log = ProjectStageLog.objects.get()
        self.assertEqual(log.website_new_id, self.account.websites.first().id)
        self.assertEqual(log.from_stage, 'intake')
        self.assertEqual(log.to_stage, 'live')
        self.assertEqual(log.set_by, 'sync')

    def test_sends_the_maintenance_handoff_email(self):
        from django.core import mail

        from sync.handlers import handle_project_complete

        mail.outbox = []
        handle_project_complete(_bundle())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('/maintenance/start/?token=', mail.outbox[0].body)


class HandoffTokenCompatibilityTests(TestCase):
    """Tokens live 48 hours, so ones minted before the cutover carry a
    legacy ClientProfile id. Rejecting them would meet a client with
    "this link has expired" through no fault of their own."""

    def test_an_account_id_resolves(self):
        from sync.handlers import handle_client_created
        from sync.views import _account_for_token

        account = handle_client_created(_bundle())
        self.assertEqual(_account_for_token(str(account.id)), account)

    def test_a_legacy_profile_id_still_resolves(self):
        from clients.models import ClientProfile
        from sync.views import _account_for_token

        user = User.objects.create_user(
            username='legacy', email='legacy@example.com', password='x')
        profile = ClientProfile.objects.create(
            user=user, firm_name='Legacy Co')
        account = profile.migrated_account

        self.assertEqual(_account_for_token(str(profile.id)), account)

    def test_an_unknown_id_returns_none(self):
        from sync.views import _account_for_token

        self.assertIsNone(
            _account_for_token('33333333-3333-3333-3333-333333333333'))

    def test_no_id_returns_none(self):
        from sync.views import _account_for_token

        self.assertIsNone(_account_for_token(None))
        self.assertIsNone(_account_for_token(''))


class HandoffFollowupTests(TestCase):

    def setUp(self):
        from sync.handlers import handle_client_created
        self.account = handle_client_created(_bundle())
        self.site = self.account.websites.first()

    def _hand_off(self, days_ago):
        from datetime import timedelta
        self.site.moonieful_handoff_at = (
            timezone.now() - timedelta(days=days_ago))
        self.site.maintenance_active = False
        self.site.save()

    def test_day_three_followup_is_sent_once(self):
        from django.core import mail
        from django.core.management import call_command

        self._hand_off(4)
        mail.outbox = []
        call_command('send_handoff_followups')
        self.assertEqual(len(mail.outbox), 1)

        # Second run must not re-send.
        mail.outbox = []
        call_command('send_handoff_followups')
        self.assertEqual(len(mail.outbox), 0)

    def test_nothing_is_sent_before_day_three(self):
        from django.core import mail
        from django.core.management import call_command

        self._hand_off(1)
        mail.outbox = []
        call_command('send_handoff_followups')
        self.assertEqual(len(mail.outbox), 0)

    def test_a_site_already_on_maintenance_is_not_chased(self):
        from django.core import mail
        from django.core.management import call_command

        self._hand_off(20)
        self.site.maintenance_active = True
        self.site.save()
        mail.outbox = []
        call_command('send_handoff_followups')
        self.assertEqual(len(mail.outbox), 0)

    def test_each_site_is_chased_independently(self):
        """The reason this iterates websites. A client who received two
        sites was chased about the first and never about the second."""
        from django.core import mail
        from django.core.management import call_command

        second = Website.objects.create(
            account=self.account, name='Moon Studio Shop')
        self._hand_off(4)
        second.moonieful_handoff_at = self.site.moonieful_handoff_at
        second.maintenance_active = False
        second.save()

        mail.outbox = []
        call_command('send_handoff_followups')
        self.assertEqual(len(mail.outbox), 2)
