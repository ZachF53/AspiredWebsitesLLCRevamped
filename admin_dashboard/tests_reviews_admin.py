"""
The Review System operator pages.

Three properties this file exists to hold:

* **A pasted GHL token never renders again.** It is stored encrypted and
  the page shows only a hint. A blank submit must leave it alone, or
  every unrelated edit would wipe the credential.
* **"Mark live" refuses without a consent attestation.** That record can
  only be created by the client completing the wizard themselves, and it
  is the only thing standing between us and an unanswerable TCPA claim.
* **Revealing an EIN is audited.** A tax ID is not something to read
  without leaving a trace.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account
from reviews.entitlement import program_for
from reviews.models import ReviewConsentAttestation, ReviewEvent

User = get_user_model()

_seq = 0

TOKEN = 'pit-super-secret-token-value-123'


def _staff():
    global _seq
    _seq += 1
    return User.objects.create_user(
        username=f'rvadmin{_seq}', email=f'rvadmin{_seq}@example.com',
        password='test-pass-123', is_staff=True, is_superuser=True)


def _program(**kwargs):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'rvadmcl{_seq}', email=f'rvadmcl{_seq}@example.com',
        password='test-pass-123')
    account = Account.objects.create(
        user=user, name=f'Admin Co {_seq}', onboarding_status='complete')
    program = program_for(account, create=True)
    for field, value in kwargs.items():
        setattr(program, field, value)
    if kwargs:
        program.save()
    return program


def _ready(program):
    """Everything "Mark live" needs except the attestation."""
    program.ghl_location_id = 'loc_ready'
    program.sending_number = '+14785550134'
    program.a2p_brand_status = 'approved'
    program.a2p_campaign_status = 'approved'
    program.google_review_url = 'https://g.page/r/CQabc/review'
    program.save()
    return program


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class QueueTests(TestCase):

    def setUp(self):
        self.client.force_login(_staff())

    def test_the_queue_renders_and_lists_programs(self):
        _program(legal_business_name='Johnson HVAC')
        resp = self.client.get(reverse('admin_dashboard:reviews_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Johnson HVAC')

    def test_awaiting_a2p_is_counted_for_the_operator(self):
        _program(setup_status='awaiting_a2p', legal_business_name='A Co')
        _program(setup_status='live', legal_business_name='B Co')
        resp = self.client.get(reverse('admin_dashboard:reviews_list'))
        self.assertEqual(resp.context['awaiting_count'], 1)

    def test_unattributed_events_are_surfaced_not_silent(self):
        """An event with a GHL location we don't know is always a
        configuration mistake, and invisible unless something says so."""
        ReviewEvent.objects.create(
            program=None, event_type='sent', dedupe_key='orphan-1',
            occurred_at=timezone.now(), ghl_location_id='unknown')
        resp = self.client.get(reverse('admin_dashboard:reviews_list'))
        self.assertEqual(resp.context['unresolved_events'], 1)
        self.assertContains(resp, 'could not be matched')

    def test_an_empty_queue_says_so_rather_than_breaking(self):
        resp = self.client.get(reverse('admin_dashboard:reviews_list'))
        self.assertContains(resp, 'No review programs yet')

    def test_non_staff_cannot_reach_it(self):
        self.client.logout()
        user = User.objects.create_user(
            username='notstaff-rv', email='notstaff-rv@example.com',
            password='test-pass-123')
        self.client.force_login(user)
        resp = self.client.get(reverse('admin_dashboard:reviews_list'))
        self.assertNotEqual(resp.status_code, 200)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class CredentialHandlingTests(TestCase):

    def setUp(self):
        self.client.force_login(_staff())
        self.program = _program()
        self.url = reverse('admin_dashboard:review_save_credentials',
                           args=[self.program.pk])
        self.detail = reverse('admin_dashboard:review_program_detail',
                              args=[self.program.pk])

    def test_a_pasted_token_is_stored_encrypted(self):
        self.client.post(self.url, {
            'ghl_location_id': 'loc_1', 'ghl_token': TOKEN,
            'sending_number': '+14785550134'})
        self.program.refresh_from_db()
        self.assertEqual(self.program.ghl_location_id, 'loc_1')
        self.assertNotIn(TOKEN, self.program.ghl_token_encrypted)
        self.assertEqual(self.program.ghl_token(), TOKEN)
        self.assertIsNotNone(self.program.ghl_token_set_at)
        self.assertIsNotNone(self.program.ghl_token_set_by_id)

    def test_the_token_never_appears_on_the_page_again(self):
        self.client.post(self.url, {
            'ghl_location_id': 'loc_1', 'ghl_token': TOKEN,
            'sending_number': ''})
        resp = self.client.get(self.detail)
        self.assertNotContains(resp, TOKEN)
        # The hint is fine — it is three characters.
        self.program.refresh_from_db()
        self.assertContains(resp, self.program.ghl_token_hint)

    def test_a_blank_submit_keeps_the_existing_token(self):
        """Otherwise editing the sending number would wipe the
        credential, which is a very annoying way to lose an afternoon."""
        self.client.post(self.url, {
            'ghl_location_id': 'loc_1', 'ghl_token': TOKEN,
            'sending_number': ''})
        self.client.post(self.url, {
            'ghl_location_id': 'loc_1', 'ghl_token': '',
            'sending_number': '+14785550199'})
        self.program.refresh_from_db()
        self.assertEqual(self.program.ghl_token(), TOKEN)
        self.assertEqual(self.program.sending_number, '+14785550199')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class MarkLiveTests(TestCase):

    def setUp(self):
        self.client.force_login(_staff())
        self.program = _program()

    def _mark_live(self):
        return self.client.post(
            reverse('admin_dashboard:review_mark_live',
                    args=[self.program.pk]), follow=True)

    def test_it_refuses_without_an_attestation_and_says_why(self):
        _ready(self.program)
        resp = self._mark_live()
        self.program.refresh_from_db()
        self.assertNotEqual(self.program.setup_status, 'live', (
            'a program went live with no consent attestation on file'))
        self.assertContains(resp, 'attestation')

    def test_it_refuses_and_lists_every_missing_thing(self):
        resp = self._mark_live()
        for fragment in ('location ID', 'sending number', 'brand'):
            self.assertContains(resp, fragment)

    def test_it_succeeds_once_everything_is_on_file(self):
        _ready(self.program)
        ReviewConsentAttestation.objects.create(
            program=self.program, version='v1-test',
            attestation_text='text', typed_name='Dale Johnson',
            agreed_at=timezone.now())
        self._mark_live()
        self.program.refresh_from_db()
        self.assertEqual(self.program.setup_status, 'live')

    def test_going_live_names_the_required_workflow_step(self):
        """Pause is decorative unless the GHL workflow polls the status
        endpoint, so the operator is told at the moment it matters."""
        _ready(self.program)
        ReviewConsentAttestation.objects.create(
            program=self.program, version='v1-test',
            attestation_text='text', typed_name='Dale',
            agreed_at=timezone.now())
        resp = self._mark_live()
        self.assertContains(resp, '/api/reviews/status/')

    def test_mark_live_requires_post(self):
        resp = self.client.get(
            reverse('admin_dashboard:review_mark_live',
                    args=[self.program.pk]))
        self.assertEqual(resp.status_code, 405)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class A2PStatusTests(TestCase):

    def setUp(self):
        self.client.force_login(_staff())
        self.program = _program()
        self.url = reverse('admin_dashboard:review_save_a2p',
                           args=[self.program.pk])

    def test_submitting_stamps_the_submitted_date_once(self):
        self.client.post(self.url, {
            'a2p_brand_status': 'submitted',
            'a2p_campaign_status': 'not_started'})
        self.program.refresh_from_db()
        first = self.program.a2p_submitted_at
        self.assertIsNotNone(first)

        self.client.post(self.url, {
            'a2p_brand_status': 'pending',
            'a2p_campaign_status': 'not_started'})
        self.program.refresh_from_db()
        self.assertEqual(self.program.a2p_submitted_at, first)

    def test_both_approved_stamps_the_approval_date(self):
        self.client.post(self.url, {
            'a2p_brand_status': 'approved',
            'a2p_campaign_status': 'approved'})
        self.program.refresh_from_db()
        self.assertIsNotNone(self.program.a2p_approved_at)

    def test_a_bogus_status_is_ignored_rather_than_stored(self):
        self.client.post(self.url, {
            'a2p_brand_status': 'definitely-not-a-choice',
            'a2p_campaign_status': 'approved'})
        self.program.refresh_from_db()
        self.assertEqual(self.program.a2p_brand_status, 'not_started')
        self.assertEqual(self.program.a2p_campaign_status, 'approved')

    def test_a_rejection_reason_reaches_the_clients_portal(self):
        self.client.post(self.url, {
            'a2p_brand_status': 'rejected',
            'a2p_campaign_status': 'not_started',
            'a2p_rejection_reason': 'Legal name does not match IRS records'})
        self.program.refresh_from_db()
        self.assertIn('IRS', self.program.a2p_rejection_reason)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class EinRevealTests(TestCase):

    def setUp(self):
        self.client.force_login(_staff())
        self.program = _program()

    def test_the_ein_is_not_on_the_page_by_default(self):
        from vault.crypto import server_encrypt
        self.program.ein_encrypted = server_encrypt('123456789')
        self.program.ein_last4 = '6789'
        self.program.save()
        resp = self.client.get(
            reverse('admin_dashboard:review_program_detail',
                    args=[self.program.pk]))
        self.assertNotContains(resp, '123456789')
        self.assertContains(resp, '6789')

    def test_revealing_it_writes_an_audit_alert(self):
        from vault.crypto import server_encrypt
        self.program.ein_encrypted = server_encrypt('123456789')
        self.program.ein_last4 = '6789'
        self.program.save()
        with patch('core.system_alerts.record_alert') as alert:
            resp = self.client.post(
                reverse('admin_dashboard:review_reveal_ein',
                        args=[self.program.pk]), follow=True)
        self.assertContains(resp, '123456789')
        self.assertTrue(alert.called)
        self.assertEqual(alert.call_args.kwargs['source'],
                         'reviews.ein_revealed')

    def test_revealing_a_missing_ein_says_so_rather_than_erroring(self):
        resp = self.client.post(
            reverse('admin_dashboard:review_reveal_ein',
                    args=[self.program.pk]), follow=True)
        self.assertContains(resp, 'No EIN on file')


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class DetailPagePrivacyTests(TestCase):

    def setUp(self):
        self.client.force_login(_staff())
        self.program = _program(ghl_location_id='loc_p')

    def test_a_raw_event_payload_is_redacted_before_render(self):
        """The payload holds the client's customers' phone numbers and
        emails. None of that belongs on an admin page in the clear."""
        ReviewEvent.objects.create(
            program=self.program, event_type='reply',
            dedupe_key='priv-1', occurred_at=timezone.now(),
            customer_phone_e164='+12105550134',
            payload={'phone': '+12105550134',
                     'email': 'dana@example.com'})
        resp = self.client.get(
            reverse('admin_dashboard:review_program_detail',
                    args=[self.program.pk]))
        self.assertNotContains(resp, 'dana@example.com')
        self.assertNotContains(resp, '+12105550134')


class NavigationTests(TestCase):
    """The nav item has to resolve in both dashboard versions."""

    def test_review_system_is_in_both_navigations(self):
        from admin_dashboard.navigation import NAVIGATION, NAVIGATION_V2, all_items
        for nav in (NAVIGATION, NAVIGATION_V2):
            labels = [item.label for item in all_items(nav)]
            self.assertIn('Review System', labels)

    def test_its_url_resolves(self):
        self.assertEqual(reverse('admin_dashboard:reviews_list'),
                         '/admin-dashboard/reviews/')
