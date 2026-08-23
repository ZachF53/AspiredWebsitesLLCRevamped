"""
Audit follow-up sequence.

These people asked for the report, so this is warm and requested. It is
still commercial email, so the opt-out has to work.
"""

from datetime import timedelta

from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from outreach.models import SuppressionList
from public.audit_sequence import (
    build_followup_1, build_followup_2, build_report,
    resolve_unsubscribe_token, send_followup, send_report,
    unsubscribe_token,
)
from public.models import AuditLead


def audit(**kw):
    defaults = {
        'url': 'https://chenlawgroup.com',
        'performance_score': 54, 'seo_score': 71,
        'best_practices_score': 78, 'accessibility_score': 42,
        'email': 'sarah@chenlawgroup.com',
        'issues': {'accessibility': [{'title': 'Low contrast'}]},
    }
    defaults.update(kw)
    return AuditLead.objects.create(**defaults)


class TailoringTests(TestCase):
    """Sending everyone the same paragraph throws away the only thing
    that makes this sequence worth reading."""

    def test_worst_category_drives_the_copy(self):
        lead = audit(performance_score=20, seo_score=95,
                     accessibility_score=95, best_practices_score=95)
        self.assertEqual(lead.worst_category, ('performance', 20))
        _, body = build_followup_1(lead)
        self.assertIn('images', body)
        self.assertNotIn('colour contrast', body)

    def test_accessibility_copy_mentions_the_legal_angle(self):
        lead = audit(accessibility_score=30, performance_score=95,
                     seo_score=95, best_practices_score=95)
        _, body = build_report(lead)
        self.assertIn('ADA', body)

    def test_two_different_sites_get_different_advice(self):
        slow = audit(performance_score=15, seo_score=95,
                     accessibility_score=95, best_practices_score=95)
        unfindable = audit(seo_score=15, performance_score=95,
                           accessibility_score=95, best_practices_score=95)
        self.assertNotEqual(build_followup_1(slow)[1],
                            build_followup_1(unfindable)[1])


class HealthySiteTests(TestCase):
    """Manufacturing a problem to justify a follow-up is how a useful
    free tool turns into a funnel people resent."""

    def test_healthy_site_gets_no_followups(self):
        lead = audit(performance_score=95, seo_score=92,
                     accessibility_score=90, best_practices_score=95,
                     report_sent_at=timezone.now())
        self.assertTrue(lead.is_healthy)
        self.assertFalse(send_followup(lead, 1))
        self.assertEqual(len(mail.outbox), 0)

    def test_healthy_site_still_gets_the_report(self):
        lead = audit(performance_score=95, seo_score=92,
                     accessibility_score=90, best_practices_score=95)
        self.assertTrue(send_report(lead))
        self.assertIn('this is a good result', mail.outbox[0].body)

    def test_one_weak_category_is_not_healthy(self):
        """Good average, one bad score - still worth writing about."""
        lead = audit(performance_score=98, seo_score=98,
                     accessibility_score=45, best_practices_score=98)
        self.assertFalse(lead.is_healthy)


class UnsubscribeTests(TestCase):

    def test_every_email_carries_an_unsubscribe_link(self):
        lead = audit()
        for build in (build_report, build_followup_1, build_followup_2):
            with self.subTest(build=build.__name__):
                self.assertIn('Unsubscribe:', build(lead)[1])

    def test_token_round_trips(self):
        lead = audit()
        self.assertEqual(
            resolve_unsubscribe_token(unsubscribe_token(lead)).pk, lead.pk)

    def test_bad_token_resolves_to_nothing(self):
        self.assertIsNone(resolve_unsubscribe_token('nonsense'))

    def test_one_click_unsubscribe_works_without_login(self):
        lead = audit()
        url = reverse('public:audit_unsubscribe',
                      args=[unsubscribe_token(lead)])
        self.assertEqual(self.client.get(url).status_code, 200)
        lead.refresh_from_db()
        self.assertTrue(lead.unsubscribed)

    def test_unsubscribing_also_suppresses_globally(self):
        """Saying no is saying no to us, not to one mailing."""
        lead = audit()
        self.client.get(reverse('public:audit_unsubscribe',
                                args=[unsubscribe_token(lead)]))
        self.assertTrue(SuppressionList.objects.filter(
            email=lead.email).exists())

    def test_bad_token_still_renders_the_page(self):
        """Telling somebody their opt-out failed, when there is nothing
        they can do, sends them to the spam button instead."""
        url = reverse('public:audit_unsubscribe', args=['garbage'])
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_unsubscribed_lead_receives_nothing_further(self):
        lead = audit(unsubscribed=True, report_sent_at=timezone.now())
        self.assertFalse(send_followup(lead, 1))
        self.assertEqual(len(mail.outbox), 0)

    def test_globally_suppressed_address_receives_nothing(self):
        """Opting out of cold outreach last month still counts."""
        lead = audit(report_sent_at=timezone.now())
        SuppressionList.objects.create(email=lead.email, reason='prior')
        self.assertFalse(send_followup(lead, 1))
        self.assertEqual(len(mail.outbox), 0)


class SendingTests(TestCase):

    def test_report_sends_and_stamps(self):
        lead = audit()
        self.assertTrue(send_report(lead))
        lead.refresh_from_db()
        self.assertIsNotNone(lead.report_sent_at)
        self.assertEqual(len(mail.outbox), 1)

    def test_followup_is_not_sent_twice(self):
        lead = audit(report_sent_at=timezone.now())
        self.assertTrue(send_followup(lead, 1))
        self.assertFalse(send_followup(lead, 1))
        self.assertEqual(len(mail.outbox), 1)

    def test_sends_from_the_main_address(self):
        """They just visited the site; it should come from the address
        they expect, not a secondary sending domain."""
        from django.conf import settings
        send_report(audit())
        self.assertEqual(mail.outbox[0].from_email,
                         settings.DEFAULT_FROM_EMAIL)


class ScheduleTests(TestCase):

    def test_task_respects_the_delays(self):
        from public.tasks import send_audit_followups_task

        fresh = audit(email='a@x.com', report_sent_at=timezone.now())
        due = audit(email='b@x.com',
                    report_sent_at=timezone.now() - timedelta(days=4))
        send_audit_followups_task()

        fresh.refresh_from_db()
        due.refresh_from_db()
        self.assertIsNone(fresh.followup_1_sent_at)
        self.assertIsNotNone(due.followup_1_sent_at)

    def test_second_followup_waits_for_the_first(self):
        from public.tasks import send_audit_followups_task

        lead = audit(report_sent_at=timezone.now() - timedelta(days=10))
        send_audit_followups_task()
        lead.refresh_from_db()
        self.assertIsNotNone(lead.followup_1_sent_at)
        self.assertIsNone(lead.followup_2_sent_at)


class PostalAddressTests(TestCase):
    """Found in production: COMPANY_POSTAL_ADDRESS was set locally but on
    neither server, because .env is gitignored and had never deployed.
    The emails went out without the physical address CAN-SPAM requires.
    """

    from django.test import override_settings as _os

    @_os(COMPANY_POSTAL_ADDRESS='8735 Dunwoody Place, Atlanta, GA')
    def test_address_appears_in_every_email(self):
        lead = audit()
        for build in (build_report, build_followup_1, build_followup_2):
            with self.subTest(build=build.__name__):
                self.assertIn('Dunwoody', build(lead)[1])

    @_os(COMPANY_POSTAL_ADDRESS='')
    def test_followups_refuse_without_an_address(self):
        """Marketing email, and nobody is waiting on it."""
        lead = audit(report_sent_at=timezone.now())
        self.assertFalse(send_followup(lead, 1))
        self.assertEqual(len(mail.outbox), 0)

    @_os(COMPANY_POSTAL_ADDRESS='')
    def test_report_still_sends_without_an_address(self):
        """They asked for it thirty seconds ago. Withholding it punishes
        the visitor for an operator mistake."""
        self.assertTrue(send_report(audit()))
        self.assertEqual(len(mail.outbox), 1)
