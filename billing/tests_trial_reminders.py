"""
Our own pre-charge reminder email, and the daily sweep that sends it.

This email is the legally required reminder before a free trial
converts, so the tests care about three things more than anything else:
that it says the amount and the date, that it goes to the right people
at the right time, and that it is impossible to send twice or to lose
silently.

Mail is captured by Django's locmem backend, so nothing leaves the
process.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.pricing_models import ServiceTier
from billing.terms_models import TermsAcceptance
from billing.trial_reminders import (
    due_reminders,
    run_trial_reminder_sweep,
    send_trial_ending_email,
)

User = get_user_model()

VOICE_SLUG = 'hvac-voice-receptionist'
REVIEW_SLUG = 'hvac-review-system'


def _tier(slug=VOICE_SLUG, name='AI Voice Receptionist', price='97.00'):
    tier, _ = ServiceTier.objects.update_or_create(
        slug=slug, defaults=dict(
            category='addon', name=name, price=Decimal(price),
            is_recurring=True, billing_interval='month', is_active=True,
            stripe_price_id=f'price_{slug}'))
    return tier


def _acceptance(*, slug=VOICE_SLUG, days_to_charge=3, email='b@example.com',
                sub='sub_test123', **kwargs):
    defaults = dict(
        email=email, product_slug=slug, terms_version='2026-11-01',
        accepted_at=timezone.now() - timedelta(days=11),
        stripe_customer_id='cus_test123', stripe_subscription_id=sub,
        trial_end=timezone.now() + timedelta(days=days_to_charge))
    defaults.update(kwargs)
    return TermsAcceptance.objects.create(**defaults)


@override_settings(TRIAL_DAYS=14, TRIAL_REMINDER_DAYS=3,
                   SITE_BASE_URL='https://aspiredwebsites.com',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class DueSelectionTests(TestCase):
    """Who the sweep picks up, and who it must not."""

    def setUp(self):
        _tier()

    def test_a_trial_charging_inside_the_window_is_due(self):
        row = _acceptance(days_to_charge=2)
        self.assertIn(row, list(due_reminders()))

    def test_a_trial_charging_beyond_the_window_is_not_yet_due(self):
        row = _acceptance(days_to_charge=9)
        self.assertNotIn(row, list(due_reminders()))

    def test_an_already_reminded_trial_is_not_due_again(self):
        """The guard against a daily job mailing three times."""
        row = _acceptance(days_to_charge=2,
                          reminder_sent_at=timezone.now())
        self.assertNotIn(row, list(due_reminders()))

    def test_a_cancelled_trial_is_not_due(self):
        """Nobody who cancelled gets told they are about to be charged."""
        row = _acceptance(days_to_charge=2, cancelled_at=timezone.now())
        self.assertNotIn(row, list(due_reminders()))

    def test_a_converted_subscription_is_not_due(self):
        row = _acceptance(days_to_charge=2, converted_at=timezone.now())
        self.assertNotIn(row, list(due_reminders()))

    def test_a_trial_that_already_charged_is_not_due(self):
        """Mailing a reminder after the charge is worse than not mailing."""
        row = _acceptance(days_to_charge=-1)
        self.assertNotIn(row, list(due_reminders()))

    def test_an_abandoned_checkout_is_not_due(self):
        """Accepted the terms, never completed: no subscription, no mail."""
        row = _acceptance(days_to_charge=2, sub='')
        self.assertNotIn(row, list(due_reminders()))

    def test_a_row_with_no_trial_end_is_not_due(self):
        row = _acceptance(days_to_charge=2, trial_end=None)
        self.assertNotIn(row, list(due_reminders()))

    def test_the_window_follows_the_setting(self):
        row = _acceptance(days_to_charge=6)
        self.assertNotIn(row, list(due_reminders()))
        with override_settings(TRIAL_REMINDER_DAYS=7):
            self.assertIn(row, list(due_reminders()))


@override_settings(TRIAL_DAYS=14, TRIAL_REMINDER_DAYS=3,
                   SITE_BASE_URL='https://aspiredwebsites.com',
                   DEFAULT_FROM_EMAIL='Zachery Long <zacherylong@aspiredwebsites.com>',
                   EMAIL_FROM_MAIN='Zachery Long <zacherylong@aspiredwebsites.com>',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class ReminderContentTests(TestCase):
    """What the email actually says. This is the compliance artefact."""

    def setUp(self):
        _tier()
        mail.outbox = []

    def test_it_comes_from_our_own_address(self):
        """Not a Stripe-branded billing notice from a stranger."""
        send_trial_ending_email(_acceptance())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('aspiredwebsites.com', mail.outbox[0].from_email)

    def test_it_states_the_amount_and_the_charge_date(self):
        row = _acceptance(days_to_charge=3)
        send_trial_ending_email(row)
        msg = mail.outbox[0]
        body = msg.body + msg.alternatives[0][0]
        shown = timezone.localtime(row.trial_end)
        self.assertIn('$97/mo', body)
        self.assertIn(f'{shown:%B}', body)
        self.assertIn(str(shown.day), body)
        self.assertIn('AI Voice Receptionist', body)

    def test_it_says_nothing_has_been_charged_yet(self):
        send_trial_ending_email(_acceptance())
        msg = mail.outbox[0]
        body = (msg.body + msg.alternatives[0][0]).lower()
        self.assertIn('nothing has been charged', body)

    def test_it_carries_our_cancel_link_not_a_stripe_one(self):
        row = _acceptance()
        send_trial_ending_email(row)
        msg = mail.outbox[0]
        body = msg.body + msg.alternatives[0][0]
        self.assertIn('aspiredwebsites.com/billing/trial/', body)
        self.assertIn(str(row.pk), body)
        self.assertNotIn('stripe.com', body)

    def test_the_subject_names_the_product_and_the_date(self):
        row = _acceptance()
        send_trial_ending_email(row)
        subject = mail.outbox[0].subject
        shown = timezone.localtime(row.trial_end)
        self.assertIn('AI Voice Receptionist', subject)
        self.assertIn(f'{shown:%B}', subject)

    def test_the_text_and_html_halves_agree_on_the_charge_date(self):
        """They did not, every evening after 7pm Eastern.

        trial_end is stored in UTC. Django's `|date:` filter localises
        an aware datetime; an f-string does not. So the subject line and
        plain-text body were built from the UTC date while the HTML body
        rendered the localised one, and the two disagreed by a day for
        five hours out of every twenty-four.

        An email that is specifically the pre-charge disclosure cannot
        state two different charge dates, so this pins it. The regression
        is invisible in the morning, which is why it needs a test with a
        deliberately late UTC instant rather than `now`.
        """
        from datetime import datetime, timezone as dt_tz

        # 01:30 UTC on Nov 7 is 20:30 on Nov 6 in America/New_York.
        row = _acceptance()
        row.trial_end = datetime(2026, 11, 7, 1, 30, tzinfo=dt_tz.utc)
        row.save()
        send_trial_ending_email(row)

        msg = mail.outbox[0]
        html = msg.alternatives[0][0]
        # The local date is the one to show, and both halves must agree.
        self.assertIn('November 6', msg.subject)
        self.assertIn('November 6', msg.body)
        self.assertIn('November 6', html)
        self.assertNotIn('November 7', msg.subject)
        self.assertNotIn('November 7', msg.body)
        self.assertNotIn('November 7', html)

    def test_click_tracking_is_suppressed(self):
        """The manage URL carries a token; SendGrid must not rewrite it."""
        send_trial_ending_email(_acceptance())
        self.assertIn('X-SMTPAPI', mail.outbox[0].extra_headers)

    def test_it_greets_by_first_name_when_we_know_it(self):
        user = User.objects.create_user(
            username='b@example.com', email='b@example.com', password='pw')
        from clients.account_models import Account
        account = (Account.objects.filter(user=user).first()
                   or Account.objects.create(user=user, name='Cool Air'))
        account.contact_name = 'Dave Moreno'
        account.save()

        send_trial_ending_email(_acceptance())
        body = mail.outbox[0].body
        self.assertIn('Hi Dave,', body)
        self.assertNotIn('Dave Moreno,', body)

    def test_it_falls_back_to_there_with_no_account(self):
        """"Hi there," beats "Hi ,"."""
        send_trial_ending_email(_acceptance())
        self.assertIn('Hi there,', mail.outbox[0].body)

    def test_the_price_comes_from_the_tier_not_a_literal(self):
        """The receptionist price is a cohort rung and moves."""
        _tier(price='147.00')
        send_trial_ending_email(_acceptance())
        body = mail.outbox[0].body
        self.assertIn('$147/mo', body)
        self.assertNotIn('$97', body)

    def test_a_multipart_email_is_sent(self):
        send_trial_ending_email(_acceptance())
        msg = mail.outbox[0]
        self.assertTrue(msg.body, 'no plain-text alternative')
        self.assertEqual(msg.alternatives[0][1], 'text/html')

    def test_review_automation_names_its_own_product(self):
        _tier(slug=REVIEW_SLUG, name='Review System')
        send_trial_ending_email(
            _acceptance(slug=REVIEW_SLUG, email='r@example.com',
                        sub='sub_r'))
        self.assertIn('Review System', mail.outbox[0].subject)

    def test_a_row_with_no_trial_end_sends_nothing(self):
        row = _acceptance(trial_end=None)
        self.assertFalse(send_trial_ending_email(row))
        self.assertEqual(mail.outbox, [])


@override_settings(TRIAL_DAYS=14, TRIAL_REMINDER_DAYS=3,
                   SITE_BASE_URL='https://aspiredwebsites.com',
                   EMAIL_FROM_MAIN='zacherylong@aspiredwebsites.com',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class SweepTests(TestCase):

    def setUp(self):
        _tier()
        _tier(slug=REVIEW_SLUG, name='Review System')
        mail.outbox = []

    def test_the_sweep_sends_and_stamps(self):
        row = _acceptance(days_to_charge=2)
        sent, failed = run_trial_reminder_sweep()
        self.assertEqual((sent, failed), (1, 0))
        row.refresh_from_db()
        self.assertIsNotNone(row.reminder_sent_at)

    def test_running_it_twice_sends_once(self):
        """A daily job must be safe to run twice in a day."""
        _acceptance(days_to_charge=2)
        run_trial_reminder_sweep()
        run_trial_reminder_sweep()
        self.assertEqual(len(mail.outbox), 1)

    def test_it_sends_to_every_due_trial(self):
        _acceptance(days_to_charge=1, email='a@example.com', sub='sub_a')
        _acceptance(days_to_charge=2, email='b@example.com', sub='sub_b')
        _acceptance(days_to_charge=9, email='c@example.com', sub='sub_c')
        sent, _failed = run_trial_reminder_sweep()
        self.assertEqual(sent, 2)
        recipients = {m.to[0] for m in mail.outbox}
        self.assertEqual(recipients, {'a@example.com', 'b@example.com'})

    def test_a_failure_leaves_the_row_due_for_tomorrow(self):
        """A lost reminder is a compliance failure, so it must retry.

        send_trial_ending_email stamps reminder_sent_at only AFTER the
        send returns. If the order were reversed, a SendGrid outage
        would mark every due trial as reminded and those customers
        would be charged with no warning at all.
        """
        row = _acceptance(days_to_charge=2)
        with patch('clients.emails.send_branded',
                   side_effect=Exception('sendgrid down')):
            sent, failed = run_trial_reminder_sweep()
        self.assertEqual((sent, failed), (0, 1))
        row.refresh_from_db()
        self.assertIsNone(row.reminder_sent_at,
                          'stamped despite the send failing')
        self.assertIn(row, list(due_reminders()))

    def test_one_failure_does_not_stop_the_others(self):
        """Independent customers, independent obligations."""
        _acceptance(days_to_charge=1, email='a@example.com', sub='sub_a')
        _acceptance(days_to_charge=2, email='b@example.com', sub='sub_b')

        calls = {'n': 0}

        def flaky(**kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise Exception('first one fails')
            return None

        with patch('clients.emails.send_branded', side_effect=flaky):
            sent, failed = run_trial_reminder_sweep()
        self.assertEqual((sent, failed), (1, 1))

    def test_an_empty_sweep_is_quiet(self):
        sent, failed = run_trial_reminder_sweep()
        self.assertEqual((sent, failed), (0, 0))
        self.assertEqual(mail.outbox, [])

    def test_the_management_command_runs(self):
        from io import StringIO

        from django.core.management import call_command

        _acceptance(days_to_charge=2)
        out = StringIO()
        call_command('send_trial_reminders', stdout=out)
        self.assertIn('1 sent', out.getvalue())

    def test_the_dry_run_sends_nothing(self):
        from io import StringIO

        from django.core.management import call_command

        row = _acceptance(days_to_charge=2)
        out = StringIO()
        call_command('send_trial_reminders', '--dry-run', stdout=out)
        self.assertIn(row.email, out.getvalue())
        self.assertIn('nothing sent', out.getvalue().lower())
        self.assertEqual(mail.outbox, [])
        row.refresh_from_db()
        self.assertIsNone(row.reminder_sent_at)
