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


@override_settings(SITE_BASE_URL='https://aspiredwebsites.com')
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
                                      'aspiredwebsites.com', 'testserver'])
    def test_canonical_ignores_www_host(self):
        """Reached via www, the page still canonicalises to non-www."""
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
