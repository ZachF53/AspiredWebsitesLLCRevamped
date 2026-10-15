"""
The portal surfaces — the hub's state machine and the two client controls.

The hub is one URL serving six states, so most of this file is "does the
right page come back". The two assertions that protect money and data:

* **Pause must never touch billing.** Pausing is not cancelling, and
  conflating them would stop someone's subscription because they wanted
  a quiet week. Asserted by patching the Stripe module and requiring
  zero calls.
* **Cross-tenant scoping.** Client A must never see client B's program.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.account_models import Account, Website
from clients.service_models import MaintenancePlan
from onboarding.models import Onboarding
from reviews.entitlement import program_for
from reviews.models import ReviewEvent

User = get_user_model()

_seq = 0


def _client_account(plan_slug=None, *, onboarded=False, setup_status='',
                    with_website=True):
    global _seq
    _seq += 1
    user = User.objects.create_user(
        username=f'rvportal{_seq}', email=f'rvportal{_seq}@example.com',
        password='test-pass-123')
    account = Account.objects.create(
        user=user, name=f'Portal Co {_seq}', onboarding_status='complete')
    if with_website:
        Website.objects.create(
            account=account, name=f'Portal Site {_seq}', stage='live',
            onboarding_status='complete')
    if plan_slug:
        MaintenancePlan.objects.create(
            account=account, tier_slug=plan_slug, status='active',
            started_at=timezone.now(),
            stripe_subscription_id=f'sub_p{_seq}')
    if onboarded:
        Onboarding.objects.create(
            user=user, product_type='review_system',
            tier_slug='hvac-review-system', completed_at=timezone.now())
    if setup_status:
        program = program_for(account, create=True)
        program.setup_status = setup_status
        program.sms_body_template = 'Hi {customer_first_name} {business_name} {review_link}'
        program.save()
    return account


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class HubStateTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def _get(self, account):
        self.client.force_login(account.user)
        return self.client.get(reverse('reviews:hub'), follow=True)

    def test_no_access_shows_the_sales_page_with_the_live_price(self):
        from billing.pricing_models import ServiceTier
        account = _client_account()
        resp = self._get(account)
        self.assertEqual(resp.status_code, 200)
        tier = ServiceTier.objects.get(slug='hvac-review-system')
        self.assertContains(resp, tier.get_price_display())

    def test_hosting_only_is_offered_both_routes(self):
        account = _client_account('hvac-hosting-security')
        resp = self._get(account)
        self.assertContains(resp, reverse('reviews:signup'))
        self.assertContains(resp, 'Upgrade to the Full Plan')

    def test_legacy_maintenance_gets_no_full_plan_button(self):
        account = _client_account('maintenance-growth')
        resp = self._get(account)
        self.assertNotContains(resp, 'Upgrade to the Full Plan')

    def test_entitled_without_onboarding_is_asked_to_finish_setup(self):
        account = _client_account('hvac-full-plan')
        resp = self._get(account)
        self.assertContains(resp, 'Finish setting up')

    def test_a_full_plan_client_is_never_shown_the_price(self):
        """They already pay for it. Offering it again is the commercial
        bug the entitlement helper exists to prevent."""
        account = _client_account('hvac-full-plan')
        resp = self._get(account)
        self.assertNotContains(resp, reverse('reviews:signup'))

    def test_awaiting_a2p_shows_the_waiting_page(self):
        account = _client_account('hvac-full-plan', onboarded=True,
                                  setup_status='awaiting_a2p')
        resp = self._get(account)
        self.assertContains(resp, 'being registered')

    def test_live_redirects_to_manage(self):
        account = _client_account('hvac-review-system', onboarded=True,
                                  setup_status='live')
        self.client.force_login(account.user)
        resp = self.client.get(reverse('reviews:hub'))
        self.assertRedirects(resp, reverse('reviews:manage'))

    def test_suspended_with_a_rejection_reason_shows_it(self):
        account = _client_account('hvac-full-plan', onboarded=True,
                                  setup_status='suspended')
        program = program_for(account)
        program.a2p_rejection_reason = 'Legal business name does not match'
        program.save(update_fields=['a2p_rejection_reason'])
        resp = self._get(account)
        self.assertContains(resp, 'does not match')

    def test_suspended_without_a_reason_reads_as_a_lapsed_subscription(self):
        account = _client_account('hvac-full-plan', onboarded=True,
                                  setup_status='suspended')
        resp = self._get(account)
        self.assertContains(resp, 'inactive')

    def test_a_subscription_only_client_with_no_website_can_load_the_hub(self):
        """A reviews-only buyer has no Website at all — the portal must
        not assume one exists."""
        account = _client_account('hvac-review-system', onboarded=True,
                                  setup_status='live', with_website=False)
        self.client.force_login(account.user)
        resp = self.client.get(reverse('reviews:hub'), follow=True)
        self.assertEqual(resp.status_code, 200)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ManagePageTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        self.account = _client_account(
            'hvac-review-system', onboarded=True, setup_status='live')
        self.program = program_for(self.account)
        self.client.force_login(self.account.user)

    def test_manage_renders_with_no_events_at_all(self):
        resp = self.client.get(reverse('reviews:manage'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Requests sent')

    def test_events_appear_in_the_log_and_the_counts(self):
        now = timezone.now()
        for i in range(3):
            ReviewEvent.objects.create(
                program=self.program, event_type='sent',
                occurred_at=now, dedupe_key=f'sent-{i}',
                customer_name='Dana Ruiz',
                customer_phone_e164='+12105550134')
        ReviewEvent.objects.create(
            program=self.program, event_type='review_left',
            occurred_at=now, dedupe_key='rev-1')
        resp = self.client.get(reverse('reviews:manage'))
        self.assertContains(resp, 'Dana Ruiz')
        # 3 sent, 1 review -> 33%
        self.assertContains(resp, '33%')

    def test_the_phone_number_is_masked(self):
        ReviewEvent.objects.create(
            program=self.program, event_type='sent',
            occurred_at=timezone.now(), dedupe_key='mask-1',
            customer_phone_e164='+12105550134')
        resp = self.client.get(reverse('reviews:manage'))
        self.assertNotContains(resp, '+12105550134')
        self.assertContains(resp, '0134')

    def test_manage_bounces_to_the_hub_before_setup_is_done(self):
        account = _client_account('hvac-full-plan')
        self.client.force_login(account.user)
        resp = self.client.get(reverse('reviews:manage'))
        self.assertRedirects(resp, reverse('reviews:hub'))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class PauseDoesNotTouchBillingTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        self.account = _client_account(
            'hvac-review-system', onboarded=True, setup_status='live')
        self.program = program_for(self.account)
        self.plan = self.account.maintenance_plans.get()
        self.client.force_login(self.account.user)

    def test_pause_flips_the_flag_and_calls_no_stripe(self):
        with patch('billing.stripe_helpers.stripe') as stripe_mock:
            resp = self.client.post(reverse('reviews:pause'))
        self.assertEqual(resp.status_code, 302)
        self.program.refresh_from_db()
        self.plan.refresh_from_db()
        self.assertTrue(self.program.sending_paused)
        self.assertTrue(self.program.paused_by_client)
        self.assertEqual(self.plan.status, 'active', (
            'pausing sending changed the subscription status — pausing is '
            'not cancelling'))
        self.assertFalse(stripe_mock.method_calls)

    def test_resume_clears_it(self):
        self.client.post(reverse('reviews:pause'))
        self.client.post(reverse('reviews:resume'))
        self.program.refresh_from_db()
        self.assertFalse(self.program.sending_paused)
        self.assertIsNone(self.program.paused_at)

    def test_pause_requires_post(self):
        self.assertEqual(
            self.client.get(reverse('reviews:pause')).status_code, 405)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class WordingGuardTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def setUp(self):
        self.account = _client_account(
            'hvac-review-system', onboarded=True, setup_status='live')
        self.program = program_for(self.account)
        self.client.force_login(self.account.user)

    def _post(self, sms):
        return self.client.post(reverse('reviews:wording_save'), {
            'sms_body_template': sms,
            'email_subject_template': 'Thanks from {business_name}',
            'email_body_template': 'Thanks! {review_link}',
        }, follow=True)

    def test_an_incentive_is_rejected_and_nothing_is_saved(self):
        before = self.program.sms_body_template
        resp = self._post(
            '{business_name} review us for 10% off {review_link}')
        self.assertContains(resp, 'Google forbids')
        self.program.refresh_from_db()
        self.assertEqual(self.program.sms_body_template, before)

    def test_gating_is_rejected(self):
        resp = self._post(
            'If you were happy with {business_name}, review {review_link}')
        self.assertContains(resp, 'asked the same way')

    def test_good_wording_saves_and_marks_the_workflow_out_of_sync(self):
        self.program.wording_synced_to_ghl = True
        self.program.save(update_fields=['wording_synced_to_ghl'])
        self._post('Thanks from {business_name}! {review_link}')
        self.program.refresh_from_db()
        self.assertIn('{review_link}', self.program.sms_body_template)
        self.assertFalse(self.program.wording_synced_to_ghl)

    def test_a_stripped_link_is_put_back_by_normalisation(self):
        """Validation reports it; normalisation guarantees it. Both,
        deliberately — a client must not be able to ship a message with
        no link by racing the form."""
        from reviews.templating import normalise_template
        out = normalise_template('Thanks from {business_name}', channel='sms')
        self.assertIn('{review_link}', out)
        self.assertIn('STOP', out)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ScopingTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing')

    def test_one_client_cannot_see_another_clients_activity(self):
        a = _client_account('hvac-review-system', onboarded=True,
                            setup_status='live')
        b = _client_account('hvac-review-system', onboarded=True,
                            setup_status='live')
        ReviewEvent.objects.create(
            program=program_for(b), event_type='sent',
            occurred_at=timezone.now(), dedupe_key='other-1',
            customer_name='Someone Elses Customer')

        self.client.force_login(a.user)
        resp = self.client.get(reverse('reviews:manage'))
        self.assertNotContains(resp, 'Someone Elses Customer')

    def test_the_hub_requires_a_login(self):
        resp = self.client.get(reverse('reviews:hub'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp['Location'])


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class ReportingWindowTests(TestCase):
    """Year-to-date means from 1 January, not the trailing 365 days."""

    def setUp(self):
        self.account = _client_account(
            'hvac-review-system', onboarded=True, setup_status='live')
        self.program = program_for(self.account)

    def test_ytd_starts_on_january_first(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from reviews.reporting import window_stats

        now = datetime(2026, 10, 14, 12, 0, tzinfo=ZoneInfo('UTC'))
        # Last December is inside a trailing year but outside YTD.
        ReviewEvent.objects.create(
            program=self.program, event_type='sent', dedupe_key='last-year',
            occurred_at=datetime(2025, 12, 20, tzinfo=ZoneInfo('UTC')))
        ReviewEvent.objects.create(
            program=self.program, event_type='sent', dedupe_key='this-year',
            occurred_at=datetime(2026, 3, 2, tzinfo=ZoneInfo('UTC')))

        stats = window_stats(self.program, now=now)
        self.assertEqual(stats['ytd']['sent'], 1)
        self.assertEqual(stats['ytd']['label'], '2026 to date')

    def test_empty_program_reports_zeros_rather_than_erroring(self):
        from reviews.reporting import monthly_series, window_stats
        stats = window_stats(self.program)
        self.assertEqual(stats['d30']['sent'], 0)
        self.assertIsNone(stats['d30']['review_rate'])
        self.assertEqual(len(monthly_series(self.program)), 12)
