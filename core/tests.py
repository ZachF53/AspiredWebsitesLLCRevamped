import json
import re

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse


class SiteVerificationMetaTests(TestCase):
    """
    Search-console ownership tags (Master Plan Phase 0, §10).

    These render from settings via core.context_processors.
    site_verification. The important guarantee is that they are
    *inert* when unset — a blank token must emit no tag at all, not an
    empty one, because an empty content="" is a malformed verification
    tag that Google reports as a failed check.
    """

    def test_no_meta_tags_when_tokens_unset(self):
        """Default (blank) config renders neither verification tag."""
        with override_settings(GOOGLE_SITE_VERIFICATION='',
                               BING_SITE_VERIFICATION=''):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertNotIn('google-site-verification', html)
        self.assertNotIn('msvalidate.01', html)

    def test_google_tag_renders_when_token_set(self):
        with override_settings(GOOGLE_SITE_VERIFICATION='test-google-token',
                               BING_SITE_VERIFICATION=''):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertIn(
            '<meta name="google-site-verification" '
            'content="test-google-token">', html)
        self.assertNotIn('msvalidate.01', html)

    def test_bing_tag_renders_when_token_set(self):
        with override_settings(GOOGLE_SITE_VERIFICATION='',
                               BING_SITE_VERIFICATION='test-bing-token'):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertIn(
            '<meta name="msvalidate.01" content="test-bing-token">', html)
        self.assertNotIn('google-site-verification', html)

    def test_both_tags_render_together(self):
        with override_settings(GOOGLE_SITE_VERIFICATION='g-token',
                               BING_SITE_VERIFICATION='b-token'):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertIn('content="g-token"', html)
        self.assertIn('content="b-token"', html)

    def test_token_is_html_escaped(self):
        """A token is untrusted config — it must not break out of the tag."""
        with override_settings(
                GOOGLE_SITE_VERIFICATION='a"><script>x</script>',
                BING_SITE_VERIFICATION=''):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertNotIn('<script>x</script>', html)
        self.assertIn('&quot;&gt;&lt;script&gt;', html)


class GoogleAnalyticsTagTests(TestCase):
    """
    GA4 gtag on the marketing site.

    Two guarantees matter here. First, a blank id renders nothing — that
    is the only thing keeping staging and local traffic out of the real
    property, so it is worth a test rather than trusting the template.
    Second, the tag stays EXTERNAL: an inline <script> would force
    'unsafe-inline' into the public CSP and cost us most of its value.
    """

    def test_no_tag_when_id_unset(self):
        with override_settings(GOOGLE_ANALYTICS_ID=''):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertNotIn('analytics.js', html)
        self.assertNotIn('data-ga-id', html)

    def test_tag_renders_when_id_set(self):
        with override_settings(GOOGLE_ANALYTICS_ID='G-TESTID1234'):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertIn('data-ga-id="G-TESTID1234"', html)
        self.assertIn('js/analytics.js', html)

    def test_no_inline_gtag_snippet(self):
        """The config block must live in the external file, not the page."""
        with override_settings(GOOGLE_ANALYTICS_ID='G-TESTID1234'):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertNotIn('gtag(', html)
        self.assertNotIn('googletagmanager.com', html)

    def test_id_is_html_escaped(self):
        with override_settings(GOOGLE_ANALYTICS_ID='a"><script>x</script>'):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertNotIn('<script>x</script>', html)

    def test_public_csp_allows_ga_hosts(self):
        """The tag is useless if the policy serving it blocks the hosts."""
        resp = self.client.get(reverse('public:home'))
        csp = resp['Content-Security-Policy']
        self.assertIn(
            "script-src 'self' https://www.googletagmanager.com", csp)
        self.assertIn('https://*.google-analytics.com', csp)
        # Adding GA must not have smuggled inline execution back in.
        self.assertNotIn("'unsafe-inline'", csp)


@override_settings(SITE_BASE_URL='https://aspiredwebsites.com',
                   PRODUCTION_HOST='testserver')
class CanonicalTests(TestCase):
    """
    Self-referencing canonical + og:url (Master Plan §8).

    The site answers on both www and non-www. The canonical must always
    declare the non-www host regardless of which one served the
    request — that is the entire point of adding it.
    """

    def test_canonical_present_and_self_referencing(self):
        html = self.client.get('/pricing/').content.decode()
        self.assertIn(
            '<link rel="canonical" href="https://aspiredwebsites.com/pricing/">',
            html)

    @override_settings(ALLOWED_HOSTS=['www.aspiredwebsites.com',
                                      'aspiredwebsites.com', 'testserver'],
                      PRODUCTION_HOST='www.aspiredwebsites.com')
    def test_canonical_ignores_request_host(self):
        """
        The canonical is built from SITE_BASE_URL, never from the host
        that served the request.

        PRODUCTION_HOST is set to the www variant here only so the page
        still renders a canonical at all — otherwise the non-production
        noindex path (correctly) suppresses it and there is nothing to
        assert. What is being tested is the derivation: served from
        www, the tag must still say non-www.

        In production this scenario no longer reaches Django at all —
        nginx 301s www before it gets here — but the derivation is what
        stops any future alias host from canonicalising to itself.
        """
        html = self.client.get(
            '/pricing/', HTTP_HOST='www.aspiredwebsites.com').content.decode()
        self.assertIn(
            '<link rel="canonical" href="https://aspiredwebsites.com/pricing/">',
            html)
        self.assertNotIn('href="https://www.aspiredwebsites.com/', html)

    def test_canonical_drops_query_string(self):
        html = self.client.get(
            '/pricing/', {'utm_source': 'x', 'page': '2'}).content.decode()
        self.assertIn(
            '<link rel="canonical" href="https://aspiredwebsites.com/pricing/">',
            html)
        self.assertNotIn('utm_source', html.split('</head>')[0])

    def test_og_url_matches_canonical(self):
        html = self.client.get('/about/').content.decode()
        self.assertIn(
            '<meta property="og:url" content="https://aspiredwebsites.com/about/">',
            html)

    def test_og_image_is_absolute(self):
        html = self.client.get('/').content.decode()
        self.assertRegex(
            html,
            r'<meta property="og:image" content="https://aspiredwebsites\.com/'
            r'[^"]*og-default\.png">')

    def test_exactly_one_canonical_per_page(self):
        """Duplicated canonicals were a real defect before base.html owned it."""
        for path in ('/', '/services/web-design/', '/services/seo/',
                     '/services/digital-marketing/', '/for-law-firms/',
                     '/pricing/', '/portfolio/', '/about/', '/contact/',
                     '/audit/'):
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertEqual(html.count('rel="canonical"'), 1)
                self.assertEqual(html.count('property="og:url"'), 1)


@override_settings(PRODUCTION_HOST='testserver')
class NoindexTests(TestCase):
    """Private and duplicate-prone pages must carry noindex, no canonical."""

    NOINDEX_PATHS = ['/login/', '/password-reset/', '/contact/thanks/']

    def test_private_pages_are_noindex(self):
        for path in self.NOINDEX_PATHS:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertIn('name="robots" content="noindex, nofollow"',
                              html)

    def test_noindex_pages_have_no_canonical(self):
        """A canonical on a noindex page sends contradictory signals."""
        for path in self.NOINDEX_PATHS:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertNotIn('rel="canonical"', html)

    def test_indexable_pages_are_not_noindex(self):
        for path in ('/', '/pricing/', '/services/seo/', '/for-law-firms/'):
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertNotIn('noindex', html)

    def test_404_is_noindex(self):
        html = self.client.get('/no-such-page-here/').content.decode()
        self.assertIn('noindex', html)


@override_settings(PRODUCTION_HOST='testserver')
class StructuredDataTests(TestCase):
    """
    One business entity for the whole site (Master Plan D8).

    The failure this guards against: each service page used to inline
    its own Organization block, so crawlers saw several unrelated
    businesses with slightly different service areas.
    """

    def _blocks(self, path):
        html = self.client.get(path).content.decode()
        return [json.loads(m) for m in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            html, re.S)]

    def test_home_declares_single_organization_graph(self):
        graph = self._blocks('/')[0]['@graph']
        types = [node['@type'] for node in graph]
        self.assertIn('ProfessionalService', types)
        self.assertIn('Person', types)
        self.assertIn('WebSite', types)

    def test_organization_uses_master_record_nap(self):
        org = next(n for n in self._blocks('/')[0]['@graph']
                   if n['@type'] == 'ProfessionalService')
        self.assertEqual(org['telephone'], '+1-210-896-2536')
        self.assertEqual(org['email'], 'zacherylong@aspiredwebsites.com')
        self.assertEqual(org['address']['addressLocality'], 'Atlanta')
        self.assertEqual(org['address']['postalCode'], '30350')
        served = {a['name'] for a in org['areaServed']}
        self.assertEqual(
            served, {'Atlanta', 'Warner Robins', 'Georgia', 'San Antonio'})

    def test_service_pages_reference_the_org_by_id_not_a_copy(self):
        for path in ('/services/web-design/', '/services/seo/',
                     '/services/digital-marketing/'):
            with self.subTest(path=path):
                service = next(
                    b for b in self._blocks(path)
                    if b.get('@type') == 'Service')
                self.assertEqual(
                    service['provider'],
                    {'@id': 'https://aspiredwebsites.com/#organization'})

    def test_only_one_organization_node_sitewide(self):
        """No page may declare a second, competing business entity."""
        for path in ('/', '/services/seo/', '/pricing/', '/about/'):
            with self.subTest(path=path):
                orgs = 0
                for block in self._blocks(path):
                    nodes = block.get('@graph', [block])
                    orgs += sum(
                        1 for n in nodes
                        if n.get('@type') in ('Organization',
                                              'ProfessionalService',
                                              'LocalBusiness'))
                self.assertEqual(orgs, 1)

    def test_breadcrumbs_on_service_pages(self):
        crumbs = next(b for b in self._blocks('/services/seo/')
                      if b.get('@type') == 'BreadcrumbList')
        names = [i['name'] for i in crumbs['itemListElement']]
        self.assertEqual(names, ['Home', 'Services', 'SEO'])
        self.assertEqual(crumbs['itemListElement'][0]['item'],
                         'https://aspiredwebsites.com/')
        # The current page is the last crumb and carries no link.
        self.assertNotIn('item', crumbs['itemListElement'][-1])

    def test_no_breadcrumbs_on_top_level_pages(self):
        html = self.client.get('/').content.decode()
        self.assertNotIn('BreadcrumbList', html)


@override_settings(PRODUCTION_HOST='testserver')
class PublicCssBundleTests(TestCase):
    """
    public.css is generated from main.css and committed. If someone
    edits main.css and forgets to rebuild, the public site silently
    loses the new styles — so fail loudly here instead.
    """

    def test_bundle_is_not_stale(self):
        try:
            call_command('build_public_css', '--check')
        except CommandError as exc:
            self.fail(f'{exc}')

    def test_public_pages_load_the_public_bundle_not_main(self):
        html = self.client.get('/').content.decode()
        self.assertIn('css/public.css', html)
        self.assertNotIn('css/main.css', html)


@override_settings(SITE_BASE_URL='https://aspiredwebsites.com',
                   PRODUCTION_HOST='testserver')
class Phase2ServicePageTests(TestCase):
    """
    Phase 2 service pages. Each must satisfy the same definition of
    done as the Phase 1 pages: indexable, one canonical, one H1,
    Service schema referencing the single org by @id, breadcrumbs,
    and no second Organization node (D8).
    """

    PAGES = [
        '/services/seo/law-firm-seo/',
        '/services/web-design/law-firm-web-design/',
        '/services/seo/local-seo/',
        '/services/web-design/small-business-web-design/',
        '/services/web-design/website-redesign/',
    ]

    def _blocks(self, path):
        html = self.client.get(path).content.decode()
        return [json.loads(m) for m in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            html, re.S)]

    def test_pages_render(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_indexable_with_single_canonical(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertEqual(html.count('rel="canonical"'), 1)
                self.assertIn(
                    f'<link rel="canonical" '
                    f'href="https://aspiredwebsites.com{path}">', html)
                self.assertNotIn('noindex', html)

    def test_exactly_one_h1(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertEqual(len(re.findall(r'<h1[^>]*>', html)), 1)

    def test_service_schema_references_org_by_id(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                service = next(b for b in self._blocks(path)
                               if b.get('@type') == 'Service')
                self.assertEqual(
                    service['provider'],
                    {'@id': 'https://aspiredwebsites.com/#organization'})

    def test_no_second_organization_node(self):
        """D8 — one business entity sitewide, even on new pages."""
        for path in self.PAGES:
            with self.subTest(path=path):
                orgs = 0
                for block in self._blocks(path):
                    for node in block.get('@graph', [block]):
                        if node.get('@type') in ('Organization',
                                                 'ProfessionalService',
                                                 'LocalBusiness'):
                            orgs += 1
                self.assertEqual(orgs, 1)

    def test_faq_schema_questions_appear_on_page(self):
        """
        §8 permits FAQPage schema only where the FAQs are visible.
        Guard against the schema and the page drifting apart.
        """
        for path in self.PAGES:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                faq = next(b for b in self._blocks(path)
                           if b.get('@type') == 'FAQPage')
                for item in faq['mainEntity']:
                    # Strip the smart punctuation the template renders
                    # as HTML entities before comparing.
                    stem = item['name'].split('?')[0][:24].replace("'", '')
                    self.assertIn(stem, html.replace('&rsquo;', ''))

    def test_breadcrumbs_present(self):
        crumbs = next(b for b in self._blocks('/services/seo/law-firm-seo/')
                      if b.get('@type') == 'BreadcrumbList')
        names = [i['name'] for i in crumbs['itemListElement']]
        self.assertEqual(names, ['Home', 'Services', 'SEO', 'Law Firm SEO'])

    def test_pages_are_in_the_sitemap(self):
        """
        Path-only assertion on purpose: django.contrib.sitemaps builds
        <loc> from the REQUEST host, not SITE_BASE_URL, so under test
        these render as http://testserver/... . The host is correct in
        production because www now 301s to non-www before the sitemap
        is ever served.
        """
        xml = self.client.get('/sitemap.xml').content.decode()
        for path in self.PAGES:
            with self.subTest(path=path):
                self.assertIn(f'<loc>http://testserver{path}</loc>', xml)

    def test_hub_pages_link_down_to_children(self):
        """§8 internal-link clusters must be wired both ways."""
        seo_hub = self.client.get('/services/seo/').content.decode()
        self.assertIn('/services/seo/law-firm-seo/', seo_hub)

        design_hub = self.client.get(
            '/services/web-design/').content.decode()
        self.assertIn(
            '/services/web-design/law-firm-web-design/', design_hub)

        law_hub = self.client.get('/for-law-firms/').content.decode()
        self.assertIn('/services/seo/law-firm-seo/', law_hub)
        self.assertIn(
            '/services/web-design/law-firm-web-design/', law_hub)

    def test_no_ranking_guarantees_anywhere(self):
        """
        §15: 'No guaranteed-rankings promises, ever.' The SEO pages are
        where that rule is easiest to violate, so assert it directly.

        Matches only AFFIRMATIVE promises. Bare 'guarantee first page'
        would fire on the FAQ question "Do you guarantee first page
        rankings?" — whose answer is "No" — so the phrasing here is
        deliberately narrow enough to distinguish a promise from a
        disclaimer.
        """
        promises = (
            'we guarantee first page', 'we guarantee a ranking',
            'we guarantee rankings', 'we guarantee top',
            'guaranteed rankings', 'guaranteed first page',
            'guaranteed first-page', 'guaranteed top 3',
            'ranking guaranteed',
        )
        for path in self.PAGES + ['/services/seo/']:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode().lower()
                for phrase in promises:
                    self.assertNotIn(phrase, html)

    def test_seo_pages_state_the_no_guarantee_position(self):
        """The disclaimer must be present, not merely the absence of a promise."""
        html = self.client.get(
            '/services/seo/law-firm-seo/').content.decode().lower()
        self.assertIn('no ranking guarantees', html)
        self.assertIn('nobody controls', html)


@override_settings(PRODUCTION_HOST='aspiredwebsites.com',
                   ALLOWED_HOSTS=['aspiredwebsites.com',
                                  'staging.aspiredwebsites.com'])
class NonProductionHostTests(TestCase):
    """
    Staging must never be indexable, and must never run the tracker.

    staging.aspiredwebsites.com has public DNS, a valid certificate and
    returns 200s. Phase 1 made the risk worse by adding a
    self-referencing canonical — staging was asserting itself as
    canonical for content that duplicates production.

    Gated on PRODUCTION_HOST, not SITE_BASE_URL: staging correctly sets
    SITE_BASE_URL to its own domain, so deriving from it would make
    staging "canonical for itself" and defeat the whole check.
    """

    PROD = {'HTTP_HOST': 'aspiredwebsites.com'}
    STAGING = {'HTTP_HOST': 'staging.aspiredwebsites.com'}

    def test_staging_is_noindex_sitewide(self):
        for path in ('/', '/pricing/', '/services/seo/law-firm-seo/'):
            with self.subTest(path=path):
                html = self.client.get(path, **self.STAGING).content.decode()
                self.assertIn('name="robots" content="noindex, nofollow"',
                              html)

    def test_production_is_not_noindex(self):
        for path in ('/', '/pricing/', '/services/seo/law-firm-seo/'):
            with self.subTest(path=path):
                html = self.client.get(path, **self.PROD).content.decode()
                self.assertNotIn('noindex', html)

    def test_staging_emits_no_canonical(self):
        """A noindex page must not also claim to be canonical."""
        html = self.client.get('/', **self.STAGING).content.decode()
        self.assertNotIn('rel="canonical"', html)

    def test_production_emits_canonical(self):
        html = self.client.get('/', **self.PROD).content.decode()
        self.assertIn('rel="canonical"', html)

    def test_staging_robots_txt_disallows_everything(self):
        body = self.client.get('/robots.txt', **self.STAGING).content.decode()
        self.assertIn('Disallow: /', body)
        self.assertNotIn('Allow: /', body)
        self.assertNotIn('Sitemap:', body)

    def test_production_robots_txt_allows_and_declares_sitemap(self):
        body = self.client.get('/robots.txt', **self.PROD).content.decode()
        self.assertIn('Allow: /', body)
        self.assertIn('Sitemap: https://aspiredwebsites.com/sitemap.xml', body)

    def test_tracker_loads_only_on_production(self):
        """
        The tracker JS hardcodes absolute https://aspiredwebsites.com
        endpoints — correct, since it runs on CLIENT sites on other
        domains. Off production those calls are cross-origin, the CSP
        blocks them, and staging traffic tries to report into prod.
        """
        prod = self.client.get('/', **self.PROD).content.decode()
        staging = self.client.get('/', **self.STAGING).content.decode()
        self.assertIn('aspired-tracker.js', prod)
        self.assertNotIn('aspired-tracker.js', staging)


@override_settings(SITE_BASE_URL='https://aspiredwebsites.com',
                   PRODUCTION_HOST='testserver')
class CaseStudyTests(TestCase):
    """
    Per-project case-study pages (Master Plan §11).

    The rule this guards hardest: §15 forbids fabricated results,
    statistics and testimonials. Denis Law Group was a NEW build with
    no "before" traffic, so it must ship with zero metrics and zero
    testimonial rather than placeholder numbers.
    """

    @classmethod
    def setUpTestData(cls):
        call_command('seed_case_studies')

    def test_seed_is_idempotent(self):
        from clients.models import CaseStudy
        before = CaseStudy.objects.count()
        call_command('seed_case_studies')
        self.assertEqual(CaseStudy.objects.count(), before)

    def test_each_study_has_its_own_indexable_url(self):
        from clients.models import CaseStudy
        for study in CaseStudy.objects.filter(is_published=True):
            with self.subTest(slug=study.slug):
                resp = self.client.get(study.get_absolute_url())
                self.assertEqual(resp.status_code, 200)
                html = resp.content.decode()
                self.assertEqual(html.count('rel="canonical"'), 1)
                self.assertNotIn('noindex', html)

    def test_portfolio_links_to_every_study(self):
        from clients.models import CaseStudy
        html = self.client.get('/portfolio/').content.decode()
        for study in CaseStudy.objects.filter(is_published=True):
            with self.subTest(slug=study.slug):
                self.assertIn(study.get_absolute_url(), html)

    def test_unpublished_study_404s(self):
        from clients.models import CaseStudy
        study = CaseStudy.objects.first()
        study.is_published = False
        study.save()
        self.assertEqual(
            self.client.get(study.get_absolute_url()).status_code, 404)

    def test_studies_are_in_the_sitemap(self):
        xml = self.client.get('/sitemap.xml').content.decode()
        self.assertIn('/portfolio/denis-law-group/', xml)

    def test_denis_law_group_publishes_no_invented_metrics(self):
        """
        §15. It was a new practice launch — there is no before/after to
        report, so the page must show no metrics and no testimonial
        rather than plausible-looking placeholders.
        """
        from clients.models import CaseStudy
        study = CaseStudy.objects.get(slug='denis-law-group')
        self.assertEqual(study.metrics(), [])
        self.assertEqual(study.testimonial_quote, '')

        html = self.client.get(study.get_absolute_url()).content.decode()
        self.assertNotIn('By The Numbers', html)
        self.assertNotIn('What The Client Said', html)
        # And it should say why, rather than staying silent about it.
        self.assertIn('no &quot;before&quot; traffic', html.replace(
            '“', '&quot;').replace('”', '&quot;'))

    def test_no_study_ships_a_placeholder_metric(self):
        from clients.models import CaseStudy
        for study in CaseStudy.objects.all():
            for label, value in study.metrics():
                with self.subTest(slug=study.slug, label=label):
                    self.assertNotIn('%', str(value).replace('%', '')
                                     or 'ok')
                    self.assertNotIn('TBD', str(value))
                    self.assertNotIn('XX', str(value))

    def test_slug_autogenerates_and_stays_unique(self):
        from clients.models import CaseStudy
        a = CaseStudy.objects.create(title='Acme Roofing Co')
        b = CaseStudy.objects.create(title='Acme Roofing Co')
        self.assertEqual(a.slug, 'acme-roofing-co')
        self.assertEqual(b.slug, 'acme-roofing-co-2')

    def test_breadcrumbs_on_case_study(self):
        html = self.client.get('/portfolio/denis-law-group/').content.decode()
        block = next(m for m in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            html, re.S) if 'BreadcrumbList' in m)
        names = [i['name'] for i in json.loads(block)['itemListElement']]
        self.assertEqual(names, ['Home', 'Portfolio', 'Denis Law Group'])


@override_settings(PRODUCTION_HOST='testserver')
class RobotsTxtTests(TestCase):
    def test_declares_sitemap_and_blocks_app_surfaces(self):
        body = self.client.get('/robots.txt').content.decode()
        self.assertIn('Sitemap: https://aspiredwebsites.com/sitemap.xml', body)
        for path in ('/admin-dashboard/', '/portal/', '/api/', '/pay/'):
            self.assertIn(f'Disallow: {path}', body)

    def test_does_not_block_pages_that_rely_on_noindex(self):
        """Blocking these would hide the noindex tag from crawlers."""
        body = self.client.get('/robots.txt').content.decode()
        self.assertNotIn('Disallow: /login/', body)
        self.assertNotIn('Disallow: /audit/', body)

    def test_sitemap_contains_only_indexable_urls(self):
        xml = self.client.get('/sitemap.xml').content.decode()
        for path in ('/login/', '/contact/thanks/', '/audit/results/'):
            self.assertNotIn(f'<loc>https://aspiredwebsites.com{path}</loc>',
                             xml)
