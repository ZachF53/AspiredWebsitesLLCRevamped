"""
Scheduler conversion + submission-safety regression tests.

From the fresh-buyer review (`BRAND_REMEDIATION_HANDOFF.md`, P3 and P10):

  * the booking form declared no HTTP method, so any submit that reached
    the browser became a GET and put the visitor's name, email, phone and
    project description into the URL;
  * there was no no-JavaScript state at all, so a failed script looked
    like a broken page;
  * recurring-plan cross-sells were shown before the prospect had had a
    first conversation;
  * pre-sale copy called the meeting a "kickoff", which is post-sale
    language.
"""

from django.test import TestCase, override_settings


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class SchedulerSubmissionSafetyTests(TestCase):

    PATHS = ['/design/schedule/', '/social/schedule/', '/seo/schedule/']

    def test_form_posts_so_personal_details_cannot_reach_the_url(self):
        """A form with no method defaults to GET. Name, email, phone and
        the project description would land in the query string, and from
        there in server logs, Referer headers and analytics."""
        for path in self.PATHS:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertIn('id="schedule-form"', html)
                form = html.split('id="schedule-form"', 1)[1][:200]
                self.assertIn('method="post"', form)

    def test_csrf_token_is_present(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertIn('csrfmiddlewaretoken', html)

    def test_no_javascript_state_is_explained(self):
        for path in self.PATHS:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertIn('<noscript>', html)
                self.assertIn('/contact/', html)

    def test_post_fallback_is_answered_and_stores_nothing(self):
        """Reaching the POST branch means the calendar never ran, so no
        slot was chosen. It must say so plainly rather than silently
        dropping the submission — and must not invent a lead record."""
        from scheduler.models import ScheduledCall

        before = ScheduledCall.objects.count()
        response = self.client.post('/design/schedule/', {
            'name': 'No JS Visitor',
            'email': 'nojs@example.com',
            'phone': '210-555-0100',
            'inquiry': 'Sensitive project details',
        })

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('no time was reserved', html.lower())
        self.assertEqual(ScheduledCall.objects.count(), before)

    def test_post_fallback_does_not_echo_details_into_a_url(self):
        response = self.client.post('/design/schedule/', {
            'name': 'No JS Visitor', 'email': 'nojs@example.com',
        })
        self.assertNotIn('nojs@example.com', response.request.get(
            'QUERY_STRING', ''))
        # And no redirect that could carry them either.
        self.assertEqual(response.status_code, 200)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class SchedulerConversionCopyTests(TestCase):

    PATHS = ['/design/schedule/', '/social/schedule/', '/seo/schedule/']

    def test_no_recurring_plan_cross_sell_before_the_first_call(self):
        """Maintenance and social plan opt-ins used to appear on the
        booking form, asking for a subscription decision before the
        prospect had spoken to anyone."""
        for path in self.PATHS:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode().lower()
                self.assertNotIn('save 10% on your first month', html)
                self.assertNotIn('addon-fieldset', html)

    def test_the_canonical_call_name_is_used(self):
        """SUPERSEDED 2026-08-17. This previously asserted that "kickoff"
        never appeared pre-sale, following the handoff's advice to reserve
        it for paying customers. The owner has since named the sales call
        the Kickoff Call, which is their decision to make; the test now
        holds the canonical name instead of forbidding it.

        The collision this creates with the refund policy's "until the
        kickoff call happens" clause is recorded in
        docs/brand_fact_matrix.md and is an owner/legal decision.
        """
        from core.site_facts import CALL_NAME

        for path in self.PATHS:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertIn(CALL_NAME, html)
