"""
Tests for the payment-failure dunning sweep.

Everything here traces back to one production incident (2026-09-02).
Burgland Technology bought a $149.50/mo maintenance plan on 2026-08-26,
their card declined once and cleared on the retry a minute later, and a
week after paying they received a burst of "we were unable to process
your recent payment" emails.

Three defects lined up:

  1. Dunning started at all, on a first invoice that was paid moments
     later.
  2. The Day-7 email task never re-read the account before sending.
  3. Redis redelivered the parked ETA message ~42 times, so every copy
     sent.

The sweep is the structural answer to all three: nothing is scheduled
ahead, every action re-reads state, and the ledger makes each stage
run-once at the database level. These tests pin that down.

Stripe is mocked throughout — `check_delinquency` is the only thing that
talks to it.
"""

from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.dunning_models import DunningEvent
from billing.tests import _new_client


def _delinquent_account(firm, days_ago):
    """An account whose failure window opened `days_ago` days ago."""
    account = _new_client(firm=firm).migrated_account
    account.stripe_customer_id = f'cus_{firm.lower().replace(" ", "_")}'
    account.payment_failure_started_at = (
        timezone.now() - timezone.timedelta(days=days_ago))
    account.save()
    return account


def _patch_delinquency(value):
    """Pin what Stripe says: True / False / None."""
    return patch('billing.dunning.check_delinquency', return_value=value)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class SweepClosesFalsePositiveTests(TestCase):
    """Defect 1 — a window opened in error must close itself."""

    def test_window_closes_when_stripe_says_current(self):
        from billing.dunning import run_dunning_sweep

        account = _delinquent_account('Paid Up', days_ago=1)
        mail.outbox = []
        with _patch_delinquency(False):
            summary = run_dunning_sweep()

        account.refresh_from_db()
        self.assertIsNone(account.payment_failure_started_at)
        self.assertEqual(summary['closed'], 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_closing_charges_no_fee_and_records_no_offence(self):
        """The false-positive close must be free.

        An inflated `payment_failure_offenses` is what would have
        auto-charged Burgland $75 on their next genuine hiccup.
        """
        from billing.dunning import run_dunning_sweep

        account = _delinquent_account('No Fee', days_ago=5)
        account.payment_failure_offenses = 0
        account.save(update_fields=['payment_failure_offenses'])

        with _patch_delinquency(False), \
                patch('billing.stripe_helpers.charge_reinstatement_fee') as fee:
            run_dunning_sweep()

        account.refresh_from_db()
        self.assertEqual(account.payment_failure_offenses, 0)
        fee.assert_not_called()

    def test_a_checkout_retry_never_reaches_day_three(self):
        """End to end: window opens on day 0, closes before any email.

        This is the exact Burgland shape — the first charge declines, the
        retry succeeds, and the client should never hear about it.
        """
        from billing.dunning import run_dunning_sweep

        account = _delinquent_account('Checkout Retry', days_ago=0)
        mail.outbox = []
        with _patch_delinquency(False):
            run_dunning_sweep()

        account.refresh_from_db()
        self.assertIsNone(account.payment_failure_started_at)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(DunningEvent.objects.filter(account=account).exists())


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class SweepIdempotencyTests(TestCase):
    """Defects 2 and 3 — run-once, enforced by the database."""

    def test_repeated_sweeps_send_one_email_per_stage(self):
        from billing.dunning import run_dunning_sweep

        account = _delinquent_account('Repeat Sweep', days_ago=3)
        mail.outbox = []
        with _patch_delinquency(True):
            for _ in range(10):
                run_dunning_sweep()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            DunningEvent.objects.filter(
                account=account, stage=DunningEvent.STAGE_EMAIL_3).count(), 1)

    def test_claim_is_enforced_by_a_database_constraint(self):
        """Not a cache key, not a task id — the DB refuses the duplicate.

        That is what makes it hold across concurrent workers, restarts
        and a Redis outage, all of which the old cache marker missed.
        """
        from billing.dunning import claim_stage

        account = _delinquent_account('Constraint', days_ago=3)
        window = account.payment_failure_started_at

        first = claim_stage(account, DunningEvent.STAGE_EMAIL_3, window)
        second = claim_stage(account, DunningEvent.STAGE_EMAIL_3, window)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_account_level_stages_dedupe_despite_null_website(self):
        """The NULL-column trap.

        A plain unique_together across a nullable `website` enforces
        nothing on the NULL rows, because SQL treats NULLs as distinct.
        Every account-level email would have been free to insert again.
        """
        from billing.dunning import claim_stage

        account = _delinquent_account('Null Site', days_ago=7)
        window = account.payment_failure_started_at

        self.assertIsNotNone(
            claim_stage(account, DunningEvent.STAGE_EMAIL_7, window,
                        website=None))
        self.assertIsNone(
            claim_stage(account, DunningEvent.STAGE_EMAIL_7, window,
                        website=None))

    def test_a_new_window_claims_cleanly(self):
        """Reinstate, then fail again — the second window must not be
        blocked by the first window's ledger rows."""
        from billing.dunning import claim_stage

        account = _delinquent_account('Second Window', days_ago=3)
        first_window = account.payment_failure_started_at
        self.assertIsNotNone(
            claim_stage(account, DunningEvent.STAGE_EMAIL_3, first_window))

        account.payment_failure_started_at = timezone.now()
        account.save(update_fields=['payment_failure_started_at'])
        self.assertIsNotNone(
            claim_stage(account, DunningEvent.STAGE_EMAIL_3,
                        account.payment_failure_started_at))


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class SweepScheduleTests(TestCase):
    """Stages fire on their day, and not before."""

    def test_nothing_due_before_day_three(self):
        from billing.dunning import run_dunning_sweep

        _delinquent_account('Too Early', days_ago=2)
        mail.outbox = []
        with _patch_delinquency(True):
            run_dunning_sweep()
        self.assertEqual(len(mail.outbox), 0)

    def test_day_fourteen_suspends_the_site(self):
        from billing.dunning import run_dunning_sweep

        account = _delinquent_account('Suspend Me', days_ago=14)
        with _patch_delinquency(True), \
                patch('billing.do_helpers.set_site_maintenance_mode') as maint:
            run_dunning_sweep()
        maint.assert_called_once()
        self.assertTrue(DunningEvent.objects.filter(
            account=account, stage=DunningEvent.STAGE_MAINTENANCE,
            status=DunningEvent.STATUS_DONE).exists())

    def test_a_stage_that_raises_is_claimed_and_alerts(self):
        """A failing step must not retry daily and re-alert forever."""
        from billing.dunning import run_dunning_sweep

        account = _delinquent_account('Broken Droplet', days_ago=14)
        with _patch_delinquency(True), \
                patch('billing.do_helpers.set_site_maintenance_mode',
                      side_effect=RuntimeError('DO is down')), \
                patch('core.system_alerts.record_alert') as alert:
            run_dunning_sweep()

        event = DunningEvent.objects.get(
            account=account, stage=DunningEvent.STAGE_MAINTENANCE)
        self.assertEqual(event.status, DunningEvent.STATUS_FAILED)
        alert.assert_called()


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class DestructiveStagesHoldTests(TestCase):
    """Day 30 and Day 60 must wait for a human."""

    def test_destroy_holds_for_approval_instead_of_running(self):
        from billing.dunning import run_dunning_sweep

        account = _delinquent_account('Hold Destroy', days_ago=30)
        with _patch_delinquency(True), \
                patch('billing.do_helpers.destroy_client_droplet') as destroy, \
                patch('billing.do_helpers.set_site_maintenance_mode'), \
                patch('billing.do_helpers.set_site_offline'), \
                patch('core.system_alerts.record_alert') as alert:
            run_dunning_sweep()

        destroy.assert_not_called()
        self.assertTrue(DunningEvent.objects.filter(
            account=account, stage=DunningEvent.STAGE_DESTROY,
            status=DunningEvent.STATUS_AWAITING_APPROVAL).exists())
        self.assertTrue(any(
            c.kwargs.get('severity') == 'critical'
            for c in alert.call_args_list))

    def test_the_hold_alerts_once_not_every_day(self):
        from billing.dunning import run_dunning_sweep

        _delinquent_account('Quiet Hold', days_ago=30)
        with _patch_delinquency(True), \
                patch('billing.do_helpers.destroy_client_droplet'), \
                patch('billing.do_helpers.set_site_maintenance_mode'), \
                patch('billing.do_helpers.set_site_offline'), \
                patch('billing.dunning._alert_awaiting_approval') as alert:
            for _ in range(5):
                run_dunning_sweep()
        self.assertEqual(alert.call_count, 1)

    def test_approval_rechecks_stripe_and_refuses_if_paid(self):
        """The approval may be hours old. Never act on a stale decision —
        that is the whole lesson of this incident."""
        from billing.dunning import approve_stage

        account = _delinquent_account('Paid Before Approval', days_ago=30)
        event = DunningEvent.objects.create(
            account=account,
            window_started_at=account.payment_failure_started_at,
            stage=DunningEvent.STAGE_DESTROY,
            status=DunningEvent.STATUS_AWAITING_APPROVAL,
        )
        user = _new_client(firm='Operator').user

        with _patch_delinquency(False), \
                patch('billing.do_helpers.destroy_client_droplet') as destroy:
            ok, msg = approve_stage(event, user)

        self.assertFalse(ok)
        destroy.assert_not_called()
        account.refresh_from_db()
        self.assertIsNone(account.payment_failure_started_at)

    def test_approval_runs_the_step_when_still_delinquent(self):
        from billing.dunning import approve_stage

        account = _delinquent_account('Really Gone', days_ago=30)
        event = DunningEvent.objects.create(
            account=account,
            window_started_at=account.payment_failure_started_at,
            stage=DunningEvent.STAGE_DESTROY,
            status=DunningEvent.STATUS_AWAITING_APPROVAL,
        )
        user = _new_client(firm='Operator2').user

        with _patch_delinquency(True), \
                patch('billing.do_helpers.destroy_client_droplet') as destroy:
            ok, msg = approve_stage(event, user)

        self.assertTrue(ok, msg)
        destroy.assert_called_once()
        event.refresh_from_db()
        self.assertEqual(event.status, DunningEvent.STATUS_APPROVED)


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class StripeUnreachableTests(TestCase):
    """An API outage must not act in either direction."""

    def test_unknown_state_does_nothing_at_all(self):
        from billing.dunning import run_dunning_sweep

        account = _delinquent_account('Stripe Down', days_ago=30)
        mail.outbox = []
        with _patch_delinquency(None), \
                patch('billing.do_helpers.destroy_client_droplet') as destroy, \
                patch('billing.do_helpers.set_site_maintenance_mode') as maint:
            summary = run_dunning_sweep()

        destroy.assert_not_called()
        maint.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(summary['skipped_unknown'], 1)
        # Crucially the window stays OPEN — an outage must not silently
        # cancel enforcement against a real deadbeat either.
        account.refresh_from_db()
        self.assertIsNotNone(account.payment_failure_started_at)


class WindowCloseRestoresSitesTests(TestCase):
    """Closing a window undoes what that window did."""

    def test_suspended_site_is_restored_on_close(self):
        from billing.dunning import close_window

        account = _delinquent_account('Restore Me', days_ago=21)
        site = account.websites.first()
        DunningEvent.objects.create(
            account=account, website=site,
            window_started_at=account.payment_failure_started_at,
            stage=DunningEvent.STAGE_MAINTENANCE,
            status=DunningEvent.STATUS_DONE,
        )

        with patch('billing.do_helpers.restore_client_site') as restore:
            close_window(account, 'test')

        restore.assert_called_once()
        account.refresh_from_db()
        self.assertIsNone(account.payment_failure_started_at)

    def test_pending_destructive_steps_are_cancelled_on_close(self):
        from billing.dunning import close_window

        account = _delinquent_account('Cancel Pending', days_ago=30)
        event = DunningEvent.objects.create(
            account=account, website=account.websites.first(),
            window_started_at=account.payment_failure_started_at,
            stage=DunningEvent.STAGE_DESTROY,
            status=DunningEvent.STATUS_AWAITING_APPROVAL,
        )

        with patch('billing.do_helpers.restore_client_site'):
            close_window(account, 'client paid')

        event.refresh_from_db()
        self.assertEqual(event.status, DunningEvent.STATUS_CANCELLED)


class DunningAdminPageTests(TestCase):
    """The approval page must actually render — a template error here
    means the only route to an irreversible step is a broken page."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.staff = User.objects.create_user(
            username='dunning_admin', password='x',
            email='ops@example.com', is_staff=True)
        self.client.force_login(self.staff)

    def test_page_renders_when_empty(self):
        from django.urls import reverse
        r = self.client.get(reverse('admin_dashboard:dunning'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Payment-Failure Dunning')

    def test_page_renders_an_open_window_and_a_pending_step(self):
        from django.urls import reverse

        account = _delinquent_account('Renders Fine', days_ago=30)
        DunningEvent.objects.create(
            account=account, website=account.websites.first(),
            window_started_at=account.payment_failure_started_at,
            stage=DunningEvent.STAGE_DESTROY,
            status=DunningEvent.STATUS_AWAITING_APPROVAL,
        )
        r = self.client.get(reverse('admin_dashboard:dunning'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Approve &amp; run')
        self.assertContains(r, 'Renders Fine')

    def test_page_requires_staff(self):
        from django.urls import reverse
        self.client.logout()
        r = self.client.get(reverse('admin_dashboard:dunning'))
        self.assertIn(r.status_code, (302, 403))


class NoScheduledWorkTests(TestCase):
    """The structural guarantee: dunning schedules nothing ahead.

    Both original bugs were properties of scheduling far in the future.
    If anyone reintroduces a countdown here, this fails.
    """

    def test_sweep_queues_no_celery_tasks(self):
        from billing.dunning import run_dunning_sweep

        _delinquent_account('No Queue', days_ago=30)
        with _patch_delinquency(True), \
                patch('billing.do_helpers.set_site_maintenance_mode'), \
                patch('billing.do_helpers.set_site_offline'), \
                patch('core.system_alerts.record_alert'), \
                patch('celery.app.task.Task.apply_async') as mock_apply:
            run_dunning_sweep()
        mock_apply.assert_not_called()

    def test_no_countdown_exceeds_the_broker_visibility_timeout(self):
        """Redis redelivers anything parked longer than this, and every
        copy runs. Nothing in the codebase may cross it."""
        import pathlib
        import re

        from django.conf import settings

        timeout = settings.CELERY_BROKER_TRANSPORT_OPTIONS['visibility_timeout']
        root = pathlib.Path(settings.BASE_DIR)
        offenders = []
        for path in root.rglob('*.py'):
            if 'myvenv' in path.parts or 'migrations' in path.parts:
                continue
            if path.name.startswith('test') or 'tests' in path.name:
                continue
            try:
                text = path.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError):
                continue
            # countdown=<int> * DAY / DAY_SECONDS / 86400, or a bare int
            for m in re.finditer(
                    r'countdown=\s*(\d+)\s*\*\s*(DAY|DAY_SECONDS|86400)', text):
                if int(m.group(1)) * 86400 > timeout:
                    offenders.append(f'{path.name}: {m.group(0)}')
            for m in re.finditer(r'countdown=\s*(\d{6,})\b', text):
                if int(m.group(1)) > timeout:
                    offenders.append(f'{path.name}: {m.group(0)}')
        self.assertEqual(offenders, [], f'countdown exceeds broker timeout: {offenders}')
