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

The fixture below is a literal copy of Moonieful's actual bundle shape
(sync/bundle.py on the Moonieful side, pinned in docs/sync_contract.md in
both repos) — not a shape guessed from what the handlers happen to read.
An earlier version of this fixture WAS reverse-engineered from the (buggy)
handler code, which is exactly why these tests passed despite the real
bundle never matching what the handlers expected.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from clients.account_models import Account, Website
from clients.models import ClientDocument, IntakeResponse

User = get_user_model()


def _bundle(**over):
    data = {
        'schema_version': 1,
        'source_site': 'moonieful',
        'event_type': 'client_created',
        'event_id': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
        'synced_at': '2026-01-02T00:00:00+00:00',
        'client': {
            'moonieful_client_id': '11111111-1111-1111-1111-111111111111',
            'created_at': '2026-01-01T00:00:00+00:00',
            'updated_at': '2026-01-02T00:00:00+00:00',
            'account': {
                'email': 'moon@example.com',
                'username': 'danamoon',
                'first_name': 'Dana',
                'last_name': 'Moon',
                'is_active': True,
                'password_hash': 'pbkdf2_sha256$fake$fakehash',
            },
            'business_name': 'Moon Studio',
            'phone': '210-555-0142',
            'website': 'https://moonstudio.example',
            'moonieful_package': 'Brand + Site',
            'status': 'active',
        },
        'stage_history': [
            {'id': 'sh-1', 'stage_name': 'brand', 'note': '',
             'created_at': '2026-01-02T00:00:00+00:00',
             'updated_at': '2026-01-02T00:00:00+00:00'},
        ],
        'documents': [],
        'intake': [
            {'form_title': 'Brand Intake', 'submitted_at': '2026-01-01T00:00:00+00:00',
             'answers': [
                 {'question_text': 'Brand colours', 'question_type': 'text',
                  'value_text': 'sage, cream', 'file_ref': None},
             ]},
        ],
        'revision_requests': [],
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
        self.assertEqual(account.user.password, 'pbkdf2_sha256$fake$fakehash')

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

        bundle = _bundle()
        site = handle_client_created(bundle).websites.first()
        intake = IntakeResponse.objects.get(website_new=site)
        self.assertEqual(intake.moonieful_intake_raw, bundle['intake'])

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
        bundle['client']['account']['email'] = ''
        with self.assertRaises(ValueError):
            handle_client_created(bundle)

    def test_a_pre_existing_unrelated_website_is_never_hijacked(self):
        """An Account can own multiple Websites. If this person is already
        an Aspired-direct client with their own build, a Moonieful referral
        must create a SECOND, distinct Website — never overwrite the
        existing one's stage/business_type/package with referral data."""
        from sync.handlers import handle_client_created

        user = User.objects.create_user(
            username='existing2', email='moon@example.com', password='x')
        account = Account.objects.create(user=user, name='Existing Direct Co')
        original_site = Website.objects.create(
            account=account, name='Existing Direct Build',
            business_type='Law Firm', stage='live', package='core')

        synced_account = handle_client_created(_bundle())

        self.assertEqual(synced_account.pk, account.pk)
        original_site.refresh_from_db()
        self.assertEqual(original_site.business_type, 'Law Firm')
        self.assertEqual(original_site.stage, 'live')
        self.assertFalse(original_site.moonieful_referred)

        self.assertEqual(account.websites.count(), 2)
        referred = account.websites.filter(moonieful_referred=True).get()
        self.assertEqual(referred.stage, 'intake')
        self.assertEqual(referred.business_type, '')


class ClientUpdatedTests(TestCase):

    def setUp(self):
        from sync.handlers import handle_client_created
        self.account = handle_client_created(_bundle())

    def test_updates_the_account_and_the_site_url(self):
        from sync.handlers import handle_client_updated

        bundle = _bundle(event_type='client_updated')
        bundle['client']['business_name'] = 'Moon Studio Co'
        bundle['client']['website'] = 'https://new.example'
        bundle['client']['updated_at'] = timezone.now().isoformat()
        handle_client_updated(bundle)

        self.account.refresh_from_db()
        self.assertEqual(self.account.name, 'Moon Studio Co')
        self.assertEqual(
            self.account.websites.first().url, 'https://new.example')

    def test_an_unknown_moonieful_id_raises(self):
        from sync.handlers import handle_client_updated

        bundle = _bundle(event_type='client_updated')
        bundle['client']['moonieful_client_id'] = (
            '99999999-9999-9999-9999-999999999999')
        with self.assertRaises(ValueError):
            handle_client_updated(bundle)

    def test_a_stale_update_is_skipped(self):
        """Staleness check: a bundle older than our row does not win."""
        from sync.handlers import handle_client_updated

        bundle = _bundle(event_type='client_updated')
        bundle['client']['business_name'] = 'Should Not Apply'
        bundle['client']['updated_at'] = '2020-01-01T00:00:00+00:00'
        handle_client_updated(bundle)

        self.account.refresh_from_db()
        self.assertEqual(self.account.name, 'Moon Studio')

    def test_does_not_touch_an_unrelated_website_on_the_same_account(self):
        """The update must resolve the Moonieful-referred site specifically,
        not just 'the account's first website'."""
        from sync.handlers import handle_client_updated

        other_site = Website.objects.create(
            account=self.account, name='Unrelated Direct Build',
            url='https://unrelated.example')

        bundle = _bundle(event_type='client_updated')
        bundle['client']['website'] = 'https://new.example'
        bundle['client']['updated_at'] = timezone.now().isoformat()
        handle_client_updated(bundle)

        other_site.refresh_from_db()
        self.assertEqual(other_site.url, 'https://unrelated.example')


class ProjectCompleteTests(TestCase):

    def setUp(self):
        from sync.handlers import handle_client_created
        self.account = handle_client_created(_bundle())

    def test_moves_the_website_live_and_stamps_the_handoff(self):
        from sync.handlers import handle_project_complete

        handle_project_complete(_bundle(event_type='project_complete'))

        site = self.account.websites.get(moonieful_referred=True)
        site.refresh_from_db()
        self.assertEqual(site.stage, 'live')
        self.assertIsNotNone(site.moonieful_handoff_at)

    def test_logs_the_stage_change_against_the_website(self):
        from sync.handlers import handle_project_complete

        handle_project_complete(_bundle(event_type='project_complete'))

        # WebsiteStageLog, because that is the relation the portal's
        # Activity Log and project timeline read. A ProjectStageLog row
        # satisfied this assertion while being invisible to the client.
        from clients.account_models import WebsiteStageLog

        site = self.account.websites.get(moonieful_referred=True)
        log = WebsiteStageLog.objects.get()
        self.assertEqual(log.website_id, site.id)
        self.assertEqual(log.from_stage, 'intake')
        self.assertEqual(log.to_stage, 'live')
        self.assertEqual(log.set_by, 'sync')

    def test_sends_the_maintenance_handoff_email(self):
        from django.core import mail

        from sync.handlers import handle_project_complete

        mail.outbox = []
        handle_project_complete(_bundle(event_type='project_complete'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('/maintenance/start/?token=', mail.outbox[0].body)


class DocumentAddedTests(TestCase):

    def setUp(self):
        from sync.handlers import handle_client_created
        self.account = handle_client_created(_bundle())

    def test_attaches_documents_to_the_moonieful_referred_website(self):
        from sync.handlers import handle_document_added

        other_site = Website.objects.create(
            account=self.account, name='Unrelated Direct Build')

        bundle = _bundle(event_type='document_added', documents=[
            {'id': '33333333-3333-3333-3333-333333333333',
             'label': 'Logo files'},
        ])
        handle_document_added(bundle)

        referred_site = self.account.websites.get(moonieful_referred=True)
        doc = ClientDocument.objects.get(moonieful_document_id=
                                          '33333333-3333-3333-3333-333333333333')
        self.assertEqual(doc.website_new_id, referred_site.id)
        self.assertNotEqual(doc.website_new_id, other_site.id)

    def test_no_documents_raises(self):
        from sync.handlers import handle_document_added

        bundle = _bundle(event_type='document_added', documents=[])
        with self.assertRaises(ValueError):
            handle_document_added(bundle)


class StageChangedTests(TestCase):

    def setUp(self):
        from sync.handlers import handle_client_created
        self.account = handle_client_created(_bundle())

    def test_mirrors_moonieful_stage_history_without_touching_own_stage(self):
        from sync.handlers import handle_stage_changed

        site = self.account.websites.get(moonieful_referred=True)
        self.assertEqual(site.stage, 'intake')

        bundle = _bundle(event_type='stage_changed')
        bundle['stage_history'] = [
            {'id': 'sh-1', 'stage_name': 'brand', 'note': '',
             'created_at': '2026-01-02T00:00:00+00:00',
             'updated_at': '2026-01-02T00:00:00+00:00'},
            {'id': 'sh-2', 'stage_name': 'design', 'note': 'moving on',
             'created_at': '2026-01-05T00:00:00+00:00',
             'updated_at': '2026-01-05T00:00:00+00:00'},
        ]
        handle_stage_changed(bundle)

        site.refresh_from_db()
        self.assertEqual(len(site.moonieful_stage_history), 2)
        self.assertEqual(site.moonieful_stage_history[1]['stage_name'], 'design')
        # Aspired's own build stage is untouched — it is Aspired-owned.
        self.assertEqual(site.stage, 'intake')

    def test_does_not_touch_an_unrelated_website(self):
        from sync.handlers import handle_stage_changed

        other_site = Website.objects.create(
            account=self.account, name='Unrelated Direct Build', stage='live')

        handle_stage_changed(_bundle(event_type='stage_changed'))

        other_site.refresh_from_db()
        self.assertEqual(other_site.stage, 'live')
        self.assertEqual(other_site.moonieful_stage_history, [])


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


class HandoffAppearsOnTheClientTimelineTests(TestCase):
    """The handoff must land on the log the portal actually reads.

    `handle_project_complete` wrote a ProjectStageLog. The portal's
    Activity Log and project timeline both read `stage_logs`, which is
    the WebsiteStageLog accessor — so the single most significant event
    in a Moonieful-referred client's project, "your site is live", never
    appeared on their timeline. It was also the legacy model, so the row
    went away with the drop regardless.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from clients.account_models import Account, Website

        User = get_user_model()
        user = User.objects.create_user(
            username='handoffowner', email='handoff@example.com',
            password='test-pass-123')
        self.account = Account.objects.create(
            user=user, name='Handoff Co',
            moonieful_client_id='11111111-1111-1111-1111-111111111111',
            synced_from_moonieful=True)
        self.website = Website.objects.create(
            account=self.account, name='Handoff Site', stage='review',
            moonieful_referred=True)

    def _complete(self):
        from unittest.mock import patch
        from sync.handlers import handle_project_complete

        with patch('sync.handlers.send_maintenance_handoff_email'):
            return handle_project_complete({
                'client': {
                    'moonieful_client_id': str(self.account.moonieful_client_id),
                },
            })

    def test_the_site_goes_live(self):
        self._complete()
        self.website.refresh_from_db()
        self.assertEqual(self.website.stage, 'live')
        self.assertIsNotNone(self.website.moonieful_handoff_at)

    def test_the_transition_is_on_the_relation_the_portal_reads(self):
        self._complete()

        logs = list(self.website.stage_logs.all())
        self.assertEqual(
            [log.to_stage for log in logs], ['live'],
            'the handoff is not on `stage_logs`, so the client never sees '
            'it on their project timeline')
        self.assertIn('Moonieful', logs[0].note)

    def test_no_outbound_job_is_queued_back_to_moonieful(self):
        """Loop prevention. The handler sets `_from_sync` before saving
        and the outbound signal checks it — otherwise Moonieful telling
        us the project is complete makes us tell Moonieful the stage
        changed, forever."""
        from sync.models import SyncJob

        before = SyncJob.objects.count()
        self._complete()
        self.assertEqual(SyncJob.objects.count(), before)
