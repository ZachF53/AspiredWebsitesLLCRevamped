import html
import json
import re

from django.core.management import call_command
from django.test import TestCase, override_settings

FAQ_PAGES = ['/services/web-design/', '/services/review-automation/',
             '/services/hosting-maintenance/', '/pricing/']


def _json_ld(body):
    return [json.loads(b) for b in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', body, re.S)]


def _visible_text(body):
    body = re.sub(r'<script.*?</script>', ' ', body, flags=re.S)
    return html.unescape(re.sub(r'<[^>]+>', ' ', body))


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class StructuredDataTests(TestCase):
    """FAQ and Review markup must describe content that is on the page:
    structured data for invisible content is a Google guideline violation,
    and drift between the two is how that happens."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_pricing', verbosity=0)

    def test_every_json_ld_block_parses(self):
        for path in ['/'] + FAQ_PAGES:
            with self.subTest(path=path):
                self.assertTrue(_json_ld(self.client.get(path).content.decode()))

    def test_every_faq_question_in_schema_is_visible(self):
        for path in FAQ_PAGES:
            body = self.client.get(path).content.decode()
            questions = [q['name'] for block in _json_ld(body)
                         if block.get('@type') == 'FAQPage'
                         for q in block['mainEntity']]
            visible = _visible_text(body)
            with self.subTest(path=path):
                self.assertTrue(questions)
                for question in questions:
                    self.assertIn(question, visible)

    def test_homepage_has_no_self_serving_review_markup(self):
        """Google ignores reviews an organization publishes about itself;
        the testimonials stay as visible text only."""
        body = self.client.get('/').content.decode()
        self.assertNotIn('"Review"', body)
        self.assertNotIn('aggregateRating', body)
        self.assertIn('Christopher Chilton', body)

    def test_hosting_page_states_the_hourly_rate_once(self):
        body = self.client.get('/services/hosting-maintenance/').content.decode()
        self.assertNotIn('per hour/hour', body)
