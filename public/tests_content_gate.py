"""
The content gate (plan §12.1) as a test: seed the real public content,
render every public page, and fail on any forbidden wording. A change
that reintroduces "Premium Build", "local SEO", "three to four weeks" or
a first-person "I" in site copy breaks the suite, not just a manual run.
"""

from django.core.management import call_command
from django.test import TestCase, override_settings

from public import content_gate


@override_settings(PRODUCTION_HOST='aspiredwebsites.com',
                   ALLOWED_HOSTS=['aspiredwebsites.com', 'testserver'],
                   SECURE_SSL_REDIRECT=False)
class ContentGateTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        for command in ('seed_pricing', 'seed_case_studies', 'seed_insights'):
            call_command(command, verbosity=0)

    def _render(self, path):
        resp = self.client.get(path, HTTP_HOST='aspiredwebsites.com')
        self.assertEqual(resp.status_code, 200, path)
        return resp.content.decode('utf-8')

    def test_every_public_page_passes_the_gate(self):
        hits = []
        for path in content_gate.all_paths():
            hits.extend(content_gate.scan(path, self._render(path)))
        self.assertEqual([str(h) for h in hits], [])

    def test_gate_catches_the_known_regressions(self):
        """The rules themselves must keep catching what shipped before."""
        page = (
            '<main><h2>Recent HVAC Builds</h2><p>I build every site.</p>'
            '<option>Premium Build ($4,500)</option>'
            '<p>Three to four weeks. Leave whenever and take every file.</p>'
            '<p>see docs/brand_fact_matrix.md</p></main>'
        )
        labels = {h.label for h in content_gate.scan('/x/', page)}
        for expected in ('A pricing/tiers', 'C legacy/internal',
                         'E timeline/duration', 'F ownership overclaims',
                         'G proof overclaims', 'H voice'):
            self.assertIn(expected, labels)

    def test_whitelist_allows_the_real_law_firm_client(self):
        page = '<main><span class="pill-tag">Law Firm &middot; San Antonio, TX</span></main>'
        self.assertEqual(content_gate.scan('/portfolio/', page), [])
        self.assertEqual(
            content_gate.scan('/portfolio/denis-law-group/',
                              '<main><p>a law firm we maintain</p></main>'), [])

    def test_archived_article_is_noindexed_and_unlisted(self):
        html = self._render('/insights/how-much-does-law-firm-web-design-cost/')
        self.assertIn('noindex', html)
        self.assertIn('Archived article', html)
        index = self._render('/insights/')
        self.assertNotIn('/insights/how-much-does-law-firm-web-design-cost/', index)
        sitemap = self.client.get('/sitemap.xml', HTTP_HOST='aspiredwebsites.com')
        self.assertNotIn(b'law-firm-web-design-cost', sitemap.content)
        self.assertIn(b'/insights/</loc>', sitemap.content)
        self.assertNotIn(b'/portfolio/other/', sitemap.content)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class SiteContentGatingTests(TestCase):
    """Owner-input sections render nothing until the owner fills them."""

    def test_gated_sections_hidden_until_filled_then_render(self):
        from public.models import SiteContent
        call_command('seed_pricing', verbosity=0)
        row = SiteContent.get_solo()
        row.build_scope = ''
        row.founding_client_enabled = False
        row.save()
        html = self.client.get('/pricing/').content.decode()
        self.assertNotIn('What the build includes', html)
        self.assertNotIn('Founding Client Offer', html)

        row.build_scope = 'Up to 8 pages\nCopywriting'
        row.founding_client_enabled = True
        row.founding_client_headline = 'Be our first HVAC case study'
        row.founding_client_offer = 'A real discount'
        row.save()
        html = self.client.get('/pricing/').content.decode()
        self.assertIn('What the build includes', html)
        self.assertIn('Up to 8 pages', html)
        self.assertIn('Be our first HVAC case study', html)

    def test_review_facts_render_only_filled_fields(self):
        from public.models import SiteContent
        row = SiteContent.get_solo()
        row.review_consent_model = 'Customers opt in on the work order.'
        row.save()
        html = self.client.get('/services/review-automation/').content.decode()
        self.assertIn('How It Connects', html)
        self.assertIn('Customers opt in on the work order.', html)
        self.assertNotIn('Message costs', html)


@override_settings(ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False)
class CallbackFormTests(TestCase):

    def _post(self, **data):
        from public.views import _signed_form_timestamp
        import time
        payload = {'name': 'Pat Jones', 'phone': '(478) 555-0142',
                   'best_time': 'after 4', 'website_url': '',
                   'form_timestamp': _signed_form_timestamp()}
        payload.update(data)
        time.sleep(0)  # timestamp age is faked below
        return self.client.post('/callback/', payload)

    def test_valid_request_creates_a_tagged_lead(self):
        from unittest import mock
        from outreach.models import Lead
        with mock.patch('public.views._form_age_seconds', return_value=(10, True)):
            resp = self._post()
        self.assertEqual(resp.status_code, 302)
        lead = Lead.objects.get()
        self.assertEqual(lead.tags, 'callback')
        self.assertIn('after 4', lead.inquiry_text)

    def test_honeypot_creates_nothing(self):
        from unittest import mock
        from outreach.models import Lead
        with mock.patch('public.views._form_age_seconds', return_value=(10, True)):
            self._post(website_url='http://spam.example')
        self.assertFalse(Lead.objects.exists())

    def test_get_redirects_to_contact(self):
        self.assertEqual(self.client.get('/callback/').status_code, 302)
