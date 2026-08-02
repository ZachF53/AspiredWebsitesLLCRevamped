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


class ConversionEventWiringTests(TestCase):
    """
    The client half of §10's event spec: events.js and the CSP it has
    to live inside.
    """

    def _js(self, name):
        import os

        from django.conf import settings as _settings
        path = os.path.join(_settings.BASE_DIR, 'core', 'static', 'js', name)
        with open(path, encoding='utf-8') as fh:
            return fh.read()

    def test_events_script_loads_on_public_pages(self):
        html = self.client.get(reverse('public:home')).content.decode()
        self.assertIn('js/events.js', html)

    def test_events_script_loads_even_without_a_ga_id(self):
        """
        Unlike analytics.js this is NOT gated on GOOGLE_ANALYTICS_ID.
        Every emit no-ops without gtag, and loading it on staging means
        a broken selector shows up there rather than only in prod.
        """
        with override_settings(GOOGLE_ANALYTICS_ID=''):
            html = self.client.get(reverse('public:home')).content.decode()
        self.assertIn('js/events.js', html)
        self.assertNotIn('js/analytics.js', html)

    def test_event_payload_block_is_present_and_inert(self):
        resp = self.client.get(reverse('public:home'))
        html = resp.content.decode()
        self.assertIn(
            '<script id="analytics-events" type="application/json">', html)
        # A data block, not executable script — so it needs no nonce and
        # script-src must still be free of 'unsafe-inline'.
        self.assertNotIn("'unsafe-inline'", resp['Content-Security-Policy'])

    def test_no_inline_event_handlers(self):
        """
        onclick= attributes would be blocked by the CSP and silently
        lose the conversion. All wiring must be delegated from the
        external file.
        """
        for path in ('/', '/pricing/', '/portfolio/', '/contact/'):
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertNotIn('onclick=', html)

    def test_google_signals_stays_off_while_csp_excludes_google_com(self):
        """
        Paired invariant. GA4's Google Signals beacon goes to
        www.google.com/g/collect, which connect-src does not allow — it
        logged a CSP violation on every page view and delivered nothing.

        Either the flag is off, or google.com is allowed. Never neither:
        that combination is the broken state this asserts against.
        """
        from core.middleware import CSP_PUBLIC
        js = self._js('analytics.js')
        signals_off = 'allow_google_signals: false' in js
        google_allowed = 'https://www.google.com' in CSP_PUBLIC.split(
            'connect-src')[1].split(';')[0]
        self.assertTrue(
            signals_off or google_allowed,
            'Google Signals is enabled but connect-src does not allow '
            'www.google.com — the beacon will be CSP-blocked on every '
            'page view. Either keep allow_google_signals: false or add '
            'the host to CSP_PUBLIC.')

    def test_core_ga_endpoint_is_allowed(self):
        """The beacon that carries the conversions must not be blocked."""
        from core.middleware import CSP_PUBLIC
        connect = CSP_PUBLIC.split('connect-src')[1].split(';')[0]
        self.assertIn('google-analytics.com', connect)


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
class LegacyRedirectTests(TestCase):
    """
    301s for the old WordPress URLs (public/legacy_redirects.py).

    Search Console had 17 dead URLs from the previous build. These are
    the ones with real ranking signal — the San Antonio and Georgia
    service pages especially, which is why the site ranks for San
    Antonio terms on generic pages today.
    """

    def test_san_antonio_pages_redirect_permanently(self):
        for old in ('/services/san-antonio-web-design',
                    '/services/san-antonio-seo'):
            with self.subTest(old=old):
                resp = self.client.get(old)
                self.assertEqual(resp.status_code, 301)
                self.assertEqual(resp['Location'], '/locations/san-antonio/')

    def test_georgia_pages_redirect_to_their_closest_match(self):
        """
        Repointed Aug 2026 (revised D5). These used to land on national
        hubs because no Georgia page existed; a geo-scoped URL landing
        on a national hub keeps less of the old signal than one landing
        on a page with the same topic AND geography.

        georgia-marketing stays on the hub deliberately — there is no
        Georgia marketing page, and creating one just to catch a
        redirect would be the tail wagging the dog.
        """
        cases = {
            '/services/georgia-seo': '/services/seo/local-seo/',
            '/services/web-design-georgia': '/locations/atlanta/',
            '/services/georgia-marketing': '/services/digital-marketing/',
        }
        for old, target in cases.items():
            with self.subTest(old=old):
                resp = self.client.get(old)
                self.assertEqual(resp.status_code, 301)
                self.assertEqual(resp['Location'], target)

    def test_trailing_slash_variant_redirects_in_one_hop(self):
        """
        Google recorded most of these unslashed. Both forms must hit the
        target directly — letting APPEND_SLASH handle it would make a
        301 chain, which dilutes the signal the redirect exists to pass.
        """
        for old in ('/services/san-antonio-seo',
                    '/services/san-antonio-seo/'):
            with self.subTest(old=old):
                resp = self.client.get(old)
                self.assertEqual(resp.status_code, 301)
                self.assertEqual(resp['Location'], '/locations/san-antonio/')

    def test_legacy_blog_posts_redirect_to_topical_pages(self):
        resp = self.client.get('/blog/what-is-seo')
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp['Location'], '/services/seo/')

    def test_renamed_privacy_policy_redirects(self):
        resp = self.client.get('/our-privacy-policy')
        self.assertEqual(resp.status_code, 301)
        self.assertEqual(resp['Location'], '/privacy-policy/')

    def test_unknown_legacy_url_still_404s(self):
        """No catch-all. An unmapped old URL must 404, not guess."""
        for path in ('/blog/some-post-that-never-existed',
                     '/services/nonexistent-city-seo',
                     '/wp-login.php'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_live_routes_are_not_shadowed(self):
        """The legacy patterns must not intercept any real page."""
        for path in ('/services/seo/', '/services/web-design/',
                     '/privacy-policy/', '/'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


@override_settings(PRODUCTION_HOST='testserver',
                   SITE_BASE_URL='https://aspiredwebsites.com')
class SchedulerCanonicalTests(TestCase):
    """
    Four URLs render one calendar. Search Console indexed three of them
    as separate pages, so they all canonicalise to /design/schedule/ —
    the variant carried in the sitemap.
    """

    EXPECTED = ('<link rel="canonical" '
                'href="https://aspiredwebsites.com/design/schedule/">')

    def test_all_variants_canonicalise_to_design_schedule(self):
        for path in ('/schedule/', '/design/schedule/',
                     '/social/schedule/', '/seo/schedule/'):
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertIn(self.EXPECTED, html)
                self.assertEqual(html.count('rel="canonical"'), 1)

    def test_variants_remain_crawlable(self):
        """Consolidating the signal must not deindex the variants."""
        for path in ('/social/schedule/', '/seo/schedule/'):
            with self.subTest(path=path):
                resp = self.client.get(path)
                self.assertEqual(resp.status_code, 200)
                self.assertNotIn('noindex', resp.content.decode())


@override_settings(PRODUCTION_HOST='testserver')
class NoindexTests(TestCase):
    """
    Private and duplicate-prone pages must carry noindex, no canonical.

    PRODUCTION_HOST is pinned to the test host on purpose. Without it
    every page under `testserver` inherits the sitewide non-production
    noindex from base.html, which makes all three tests here vacuous —
    the "private pages are noindex" assertions would pass even if the
    per-page directives were deleted, and the indexable-pages test would
    fail on correctly-configured pages. Pinning it means these test the
    per-page directives, which is what they are for.
    """

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
        self.assertEqual(org['address']['addressLocality'], 'Warner Robins')
        self.assertEqual(org['address']['addressRegion'], 'GA')
        # Service-area business: city/state only. A streetAddress here
        # would be the registered-agent suite, asserting a staffed office
        # that does not exist. See base.html's NAP block.
        self.assertNotIn('streetAddress', org['address'])
        self.assertNotIn('postalCode', org['address'])
        served = {a['name'] for a in org['areaServed']}
        self.assertEqual(
            served,
            {'Warner Robins', 'Macon', 'Atlanta', 'Georgia', 'San Antonio'})

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

    def test_js_applied_classes_survive_the_split(self):
        """
        Regression: /design/schedule/ shipped unstyled to production.

        schedule_call.js builds the booking calendar with innerHTML
        strings, so its .cal__* classes appear in no template. The
        generator only scanned three hardcoded JS files and only looked
        for classList/className calls, so all 27 calendar rules were
        dropped and the calendar rendered as a wall of plain text.

        This asserts the general property rather than that one page:
        for every JS file referenced by a public template, any class it
        mentions that main.css actually styles must also be present in
        public.css.
        """
        import os
        import re as _re

        from django.conf import settings as _settings

        css_dir = os.path.join(_settings.BASE_DIR, 'core', 'static', 'css')
        js_dir = os.path.join(_settings.BASE_DIR, 'core', 'static', 'js')
        with open(os.path.join(css_dir, 'main.css'), encoding='utf-8') as fh:
            main_css = fh.read()
        with open(os.path.join(css_dir, 'public.css'), encoding='utf-8') as fh:
            public_css = fh.read()

        # Class names main.css actually defines a rule for.
        styled = set(_re.findall(r'\.(-?[A-Za-z_][A-Za-z0-9_-]*)\s*[,{:]',
                                 main_css))

        # Only JS the PUBLIC templates actually load. Admin-only files
        # such as deploy.js legitimately style classes the public
        # bundle should not carry.
        from core.management.commands.build_public_css import (
            PUBLIC_EXTRA_TEMPLATES, PUBLIC_TEMPLATE_ROOTS, _JS_REF_RE,
        )
        public_js = set()
        template_paths = []
        for parts in PUBLIC_TEMPLATE_ROOTS:
            root = os.path.join(_settings.BASE_DIR, *parts)
            for dirpath, _dirs, filenames in os.walk(root):
                template_paths += [os.path.join(dirpath, f)
                                   for f in filenames if f.endswith('.html')]
        for parts in PUBLIC_EXTRA_TEMPLATES:
            path = os.path.join(_settings.BASE_DIR, *parts)
            if os.path.exists(path):
                template_paths.append(path)
        for path in template_paths:
            with open(path, encoding='utf-8', errors='ignore') as fh:
                public_js.update(_JS_REF_RE.findall(fh.read()))

        missing = []
        for name in sorted(public_js):
            if not name.endswith('.js') or name.endswith('.min.js'):
                continue
            if not os.path.exists(os.path.join(js_dir, name)):
                continue
            with open(os.path.join(js_dir, name),
                      encoding='utf-8', errors='ignore') as fh:
                js = fh.read()
            # Classes named inside class="..." in JS-built markup —
            # the exact pattern that broke.
            mentioned = set()
            for match in _re.finditer(r'class=\\?["\']([^"\'\\]*)', js):
                mentioned.update(match.group(1).split())
            for cls in mentioned & styled:
                if f'.{cls}' not in public_css:
                    missing.append(f'{name}: .{cls}')

        self.assertEqual(
            missing, [],
            'Classes applied by public JS are styled in main.css but '
            'absent from public.css — they will render unstyled:\n  '
            + '\n  '.join(missing))


@override_settings(PRODUCTION_HOST='testserver')
class BulletListMarkerTests(TestCase):
    """
    Regression: .bullet-list sets `list-style: none` because it was
    written for a <li class="bullet-list__item"> wrapper carrying its
    own icon span. Every list on the Phase 2/3 pages used a plain
    <li> instead, so the native marker was suppressed and no
    replacement was drawn — they shipped as unbulleted sentences.

    Both forms must produce a visible marker.
    """

    def _css(self, name):
        import os

        from django.conf import settings as _settings
        path = os.path.join(_settings.BASE_DIR, 'core', 'static', 'css', name)
        with open(path, encoding='utf-8') as fh:
            return fh.read()

    def test_bare_list_items_get_a_marker_in_both_bundles(self):
        for bundle in ('main.css', 'public.css'):
            with self.subTest(bundle=bundle):
                css = self._css(bundle)
                self.assertIn('.bullet-list > li:not(.bullet-list__item)', css)
                self.assertIn(
                    '.bullet-list > li:not(.bullet-list__item)::before', css)

    def test_marker_is_the_brand_orange(self):
        css = self._css('main.css')
        rule = css.split(
            '.bullet-list > li:not(.bullet-list__item)::before')[1]
        self.assertIn('var(--color-orange)', rule.split('}')[0])

    def test_every_public_bullet_list_item_can_render_a_marker(self):
        """
        A <li> is styled either by the icon wrapper or by the ::before
        rule. Anything else renders bare — so no template may use a
        third form.
        """
        import os
        import re as _re

        from django.conf import settings as _settings

        bad = []
        for root, _dirs, files in os.walk(_settings.BASE_DIR):
            if 'myvenv' in root or '.git' in root:
                continue
            for name in files:
                if not name.endswith('.html'):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding='utf-8', errors='ignore') as fh:
                    html = fh.read()
                for block in _re.findall(
                        r'<ul class="bullet-list">(.*?)</ul>', html, _re.S):
                    for li in _re.findall(r'<li([^>]*)>', block):
                        # Either bare (::before covers it) or the icon
                        # wrapper. A different class on the <li> would
                        # match neither.
                        cls = _re.search(r'class="([^"]*)"', li)
                        if cls and 'bullet-list__item' not in cls.group(1):
                            bad.append(f'{name}: <li{li}>')
        self.assertEqual(bad, [], 'Unstyleable bullet-list items: %s' % bad)


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
        # ── Phase 3 ──
        '/services/web-design/custom-web-development/',
        '/locations/san-antonio/',
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

        Note the direction of the rule: FAQ schema is not *required*
        on every page — the location page has no FAQ section and
        correctly emits none. What is asserted is the conditional: if
        a page ships FAQPage schema, every question in it must appear
        in the visible copy.
        """
        for path in self.PAGES:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                faq = next((b for b in self._blocks(path)
                            if b.get('@type') == 'FAQPage'), None)
                if faq is None:
                    continue
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


@override_settings(SITE_BASE_URL='https://aspiredwebsites.com',
                   PRODUCTION_HOST='testserver')
class InsightsTests(TestCase):
    """/insights/ — the blog (Master Plan §12)."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_insights')

    def test_index_and_articles_render(self):
        from public.models import Article
        self.assertEqual(self.client.get('/insights/').status_code, 200)
        for article in Article.objects.filter(status='published'):
            with self.subTest(slug=article.slug):
                self.assertEqual(
                    self.client.get(article.get_absolute_url()).status_code,
                    200)

    def test_seed_is_idempotent(self):
        from public.models import Article
        before = Article.objects.count()
        call_command('seed_insights')
        self.assertEqual(Article.objects.count(), before)

    def test_draft_articles_are_invisible(self):
        from public.models import Article
        article = Article.objects.first()
        article.status = 'draft'
        article.save()
        self.assertEqual(
            self.client.get(article.get_absolute_url()).status_code, 404)
        self.assertNotIn(
            article.get_absolute_url(),
            self.client.get('/insights/').content.decode())

    def test_articles_have_named_authorship(self):
        """§11 lists named authorship on every article as an E-E-A-T fix."""
        from public.models import Article
        for article in Article.objects.filter(status='published'):
            with self.subTest(slug=article.slug):
                html = self.client.get(
                    article.get_absolute_url()).content.decode()
                self.assertIn(article.author_name, html)
                block = next(m for m in re.findall(
                    r'<script type="application/ld\+json">(.*?)</script>',
                    html, re.S) if '"Article"' in m)
                data = json.loads(block)
                self.assertEqual(
                    data['author'],
                    {'@id': 'https://aspiredwebsites.com/#zachery-long'})
                self.assertIn('datePublished', data)

    def test_every_article_links_to_its_commercial_page(self):
        """§12: supporting articles must link back to their money page."""
        from public.models import Article
        for article in Article.objects.filter(status='published'):
            with self.subTest(slug=article.slug):
                self.assertTrue(article.related_url)
                html = self.client.get(
                    article.get_absolute_url()).content.decode()
                self.assertIn(article.related_url, html)

    def test_articles_use_h2_not_a_second_h1(self):
        """The title is the page's only H1 (§5.4)."""
        from public.models import Article
        for article in Article.objects.filter(status='published'):
            with self.subTest(slug=article.slug):
                html = self.client.get(
                    article.get_absolute_url()).content.decode()
                self.assertEqual(len(re.findall(r'<h1[^>]*>', html)), 1)

    def test_articles_are_in_the_sitemap(self):
        xml = self.client.get('/sitemap.xml').content.decode()
        self.assertIn('/insights/how-much-does-a-custom-website-cost/', xml)

    def test_no_ranking_guarantee_in_articles(self):
        from public.models import Article
        for article in Article.objects.filter(status='published'):
            with self.subTest(slug=article.slug):
                body = article.body.lower()
                for phrase in ('we guarantee', 'guaranteed ranking',
                               'guaranteed first page'):
                    self.assertNotIn(phrase, body)

    def test_slug_autogenerates(self):
        from public.models import Article
        a = Article.objects.create(title='A Test Post', summary='x', body='y')
        self.assertEqual(a.slug, 'a-test-post')

    def test_publishing_sets_published_at(self):
        from public.models import Article
        a = Article.objects.create(title='Timing Test', summary='x', body='y')
        self.assertIsNone(a.published_at)
        a.status = 'published'
        a.save()
        self.assertIsNotNone(a.published_at)


@override_settings(SITE_BASE_URL='https://aspiredwebsites.com',
                   PRODUCTION_HOST='testserver')
class SanAntonioLocationPageTests(TestCase):
    """
    The one location page (D5) — and the one most at risk of breaking
    §15, which forbids fake offices and thin city pages.
    """

    @classmethod
    def setUpTestData(cls):
        call_command('seed_case_studies')

    def test_page_renders_and_is_indexable(self):
        html = self.client.get('/locations/san-antonio/').content.decode()
        self.assertEqual(html.count('rel="canonical"'), 1)
        self.assertNotIn('noindex', html)

    def test_states_plainly_there_is_no_san_antonio_office(self):
        """
        §15: no fake offices. The stronger requirement is that the page
        SAYS so rather than merely avoiding the claim — a prospect
        searching a city term is often looking for someone local and
        should find out how we work before the pitch, not after.
        """
        html = self.client.get('/locations/san-antonio/').content.decode()
        self.assertIn('Have a San Antonio Office', html)
        self.assertIn('based in Georgia', html)

    def test_does_not_declare_a_second_business_entity(self):
        """
        D8 — one business entity sitewide. A location page is the
        classic place a second LocalBusiness gets invented.
        """
        html = self.client.get('/locations/san-antonio/').content.decode()
        blocks = [json.loads(m) for m in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            html, re.S)]
        orgs = 0
        for block in blocks:
            for node in block.get('@graph', [block]):
                if node.get('@type') in ('Organization', 'ProfessionalService',
                                         'LocalBusiness'):
                    orgs += 1
        self.assertEqual(orgs, 1)

    def test_no_postal_address_claimed_for_san_antonio(self):
        """
        A location page must not fabricate a local office. The footer
        carries the operating city (Warner Robins, GA) and nothing else —
        no street address anywhere, in either metro.
        """
        html = self.client.get('/locations/san-antonio/').content.decode()
        self.assertNotIn('San Antonio, TX 7', html)   # any SA ZIP
        self.assertIn('Warner Robins, GA', html)      # footer master record
        self.assertNotIn('8735 Dunwoody', html)       # registered agent

    def test_carries_real_san_antonio_proof(self):
        """
        What stops this being a thin city page is three genuine SA
        clients, pulled from the database rather than hardcoded.
        """
        from clients.models import CaseStudy
        sa = CaseStudy.objects.filter(is_published=True,
                                      location__icontains='San Antonio')
        self.assertGreaterEqual(sa.count(), 3)

        html = self.client.get('/locations/san-antonio/').content.decode()
        for study in sa:
            with self.subTest(slug=study.slug):
                self.assertIn(study.get_absolute_url(), html)

    def test_sa_case_studies_link_back_to_the_location_page(self):
        """§8 — the cluster is wired both ways."""
        html = self.client.get(
            '/portfolio/denis-law-group/').content.decode()
        self.assertIn('/locations/san-antonio/', html)

    def test_still_no_locations_index(self):
        """
        D5 grew to three location pages (Aug 2026), but a /locations/
        hub is still refused. Three links and nothing of its own to say
        is exactly the thin page §15 forbids.
        """
        self.assertEqual(self.client.get('/locations/').status_code, 404)


@override_settings(SITE_BASE_URL='https://aspiredwebsites.com',
                   PRODUCTION_HOST='testserver')
class GeorgiaLocationPageTests(TestCase):
    """
    The Georgia pages (revised D5, Aug 2026).

    Both are held to the same bar as San Antonio: no invented office,
    no implied local clients, one business entity sitewide. The Atlanta
    page is the riskier of the two — it has real demand (2,160/mo) and
    zero Atlanta case studies, which is the exact shape §15 warns about
    — so most of these assertions point at it.
    """

    PAGES = ['/locations/atlanta/', '/locations/warner-robins/']

    @classmethod
    def setUpTestData(cls):
        call_command('seed_case_studies', verbosity=0)

    def test_pages_render_with_one_h1_and_a_canonical(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertEqual(len(re.findall(r'<h1[^>]*>', html)), 1)
                self.assertEqual(
                    len(re.findall(r'<link rel="canonical"', html)), 1)

    def test_no_street_address_is_reintroduced(self):
        """
        The registered-agent suite was deliberately removed from the
        site. A city page is the most tempting place for it to creep
        back, so assert it does not.
        """
        for path in self.PAGES:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                self.assertNotIn('8735 Dunwoody', html)
                self.assertNotIn('Dunwoody Place', html)

    def test_atlanta_page_states_it_has_no_atlanta_office(self):
        html = self.client.get('/locations/atlanta/').content.decode()
        self.assertIn('Georgia-Based, Not Atlanta-Based', html)
        self.assertIn('Warner Robins', html)

    def test_atlanta_page_admits_it_has_no_atlanta_clients(self):
        """
        §15's thin-city-page line. With no Atlanta case studies the
        page must say so outright — and must stop saying so by itself
        once an Atlanta client is published, rather than needing a
        template edit.
        """
        from clients.models import CaseStudy
        self.assertFalse(
            CaseStudy.objects.filter(is_published=True,
                                     location__icontains='Atlanta').exists(),
            'Seed data now has an Atlanta client — this test guards the '
            'no-clients-yet copy and should be updated with the page.')
        html = self.client.get('/locations/atlanta/').content.decode()
        self.assertIn('No Atlanta clients yet', html)

    def test_warner_robins_page_claims_local_because_it_is_true(self):
        """
        The one page where "we are local" is literally true — the
        sitewide schema and footer NAP both resolve here.
        """
        html = self.client.get('/locations/warner-robins/').content.decode()
        self.assertIn('Based Here, Not Just Targeting Here', html)
        self.assertIn('Warner Robins', html)

    def test_warner_robins_does_not_claim_a_storefront(self):
        html = self.client.get('/locations/warner-robins/').content.decode()
        self.assertIn('no walk-in office', html)

    def test_service_schema_references_the_one_org_and_adds_no_second(self):
        """D8 — one business entity sitewide, always."""
        for path in self.PAGES:
            with self.subTest(path=path):
                html = self.client.get(path).content.decode()
                blocks = re.findall(
                    r'<script type="application/ld\+json">(.*?)</script>',
                    html, re.S)
                service = next(json.loads(b) for b in blocks
                               if '"Service"' in b)
                self.assertEqual(
                    service['provider']['@id'],
                    'https://aspiredwebsites.com/#organization')
                # No page may mint a second LocalBusiness/ProfessionalService.
                for block in blocks:
                    data = json.loads(block)
                    nodes = data.get('@graph') or [data]
                    for node in nodes:
                        if node.get('@type') == 'ProfessionalService':
                            self.assertEqual(
                                node.get('@id'),
                                'https://aspiredwebsites.com/#organization')
                        self.assertNotEqual(node.get('@type'), 'LocalBusiness')

    def test_georgia_pages_cross_link(self):
        """§8 — the Georgia cluster is wired both ways."""
        atl = self.client.get('/locations/atlanta/').content.decode()
        self.assertIn('/locations/warner-robins/', atl)
        wr = self.client.get('/locations/warner-robins/').content.decode()
        self.assertIn('/locations/atlanta/', wr)

    def test_pages_are_in_the_sitemap(self):
        xml = self.client.get('/sitemap.xml').content.decode()
        for path in self.PAGES:
            with self.subTest(path=path):
                self.assertIn(path, xml)

    def test_no_ranking_guarantee(self):
        for path in self.PAGES:
            with self.subTest(path=path):
                low = self.client.get(path).content.decode().lower()
                for phrase in ('guarantee you rank', 'guaranteed rankings',
                               'we guarantee first page'):
                    self.assertNotIn(phrase, low)


@override_settings(PRODUCTION_HOST='testserver')
class CityIntentOwnershipTests(TestCase):
    """
    §6.1 — one page owns one intent.

    /locations/atlanta/ owns Atlanta. Before it existed, three other
    pages carried "Atlanta" in their titles; leaving them there would
    manufacture the cannibalisation §6.1 exists to prevent. The
    homepage's title also contradicted its own schema, which resolves
    to Warner Robins after the registered-agent address was removed.
    """

    def test_only_the_atlanta_page_targets_atlanta_in_its_title(self):
        offenders = []
        for path in ('/', '/services/seo/', '/contact/',
                     '/services/web-design/', '/pricing/', '/about/'):
            html = self.client.get(path).content.decode()
            title = re.search(r'<title>(.*?)</title>', html, re.S).group(1)
            if 'atlanta' in title.lower():
                offenders.append(f'{path} → {title}')
        self.assertEqual(
            offenders, [],
            'These titles compete with /locations/atlanta/ for city '
            'intent: %s' % offenders)

    def test_the_atlanta_page_does_target_atlanta(self):
        html = self.client.get('/locations/atlanta/').content.decode()
        title = re.search(r'<title>(.*?)</title>', html, re.S).group(1)
        self.assertIn('Atlanta', title)

    def test_homepage_title_still_carries_the_service(self):
        """Dropping the city must not cost the head term."""
        html = self.client.get('/').content.decode()
        title = re.search(r'<title>(.*?)</title>', html, re.S).group(1)
        self.assertIn('Custom Web Design', title)
        self.assertLessEqual(len(title), 65, f'Title too long: {title}')

    def test_homepage_h1_is_unchanged(self):
        """D9 — the retitle is a <title> change only."""
        html = self.client.get('/').content.decode()
        h1 = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S).group(1)
        # The H1 wraps its last words in a <span class="accent">, so
        # compare the text content rather than the raw markup.
        text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', h1)).strip()
        self.assertEqual(
            text, 'Custom Web Design Built to Work as Hard as You Do')


@override_settings(PRODUCTION_HOST='testserver')
class CustomWebDevPageTests(TestCase):
    """
    Keyword positioning for /custom-web-development/.

    The trap this guards: "hand coded" is the brand story but gets 10
    searches/mo, while "custom" gets ~3,780. Leading with the former
    would feel on-brand and cost the head term.
    """

    PATH = '/services/web-design/custom-web-development/'

    def test_h1_leads_with_custom_not_hand_coded(self):
        html = self.client.get(self.PATH).content.decode()
        h1 = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S).group(1).lower()
        self.assertIn('custom', h1)
        self.assertNotIn('hand coded', h1)
        self.assertNotIn('hand-coded', h1)

    def test_title_and_meta_carry_the_head_terms(self):
        html = self.client.get(self.PATH).content.decode()
        title = re.search(r'<title>(.*?)</title>', html, re.S).group(1).lower()
        self.assertIn('custom web development', title)
        desc = re.search(
            r'<meta name="description" content="([^"]*)', html).group(1).lower()
        self.assertIn('custom', desc)

    def test_still_makes_the_wordpress_comparison(self):
        """The differentiator belongs in the body, not the H1."""
        html = self.client.get(self.PATH).content.decode().lower()
        self.assertIn('wordpress', html)

    def test_admits_when_custom_is_the_wrong_choice(self):
        html = self.client.get(self.PATH).content.decode().lower()
        self.assertIn('isn&rsquo;t worth it', html)


@override_settings(PRODUCTION_HOST='testserver')
class ConversionBlockTests(TestCase):
    """
    The §7.3 conversion blocks: ownership, objection FAQ, FindLaw
    switching FAQ, credential verification, and fit.
    """

    def test_pricing_has_ownership_block(self):
        html = self.client.get('/pricing/').content.decode()
        self.assertIn('Your Website Should Belong to You', html)
        for claim in ('source code', 'domain', '30 days notice'):
            self.assertIn(claim, html)

    def test_pricing_objection_faq_covers_the_required_questions(self):
        html = self.client.get('/pricing/').content.decode()
        for question in ('Do I own my website?',
                         'Is there a contract?',
                         'Can I move the site later?',
                         'How long does a build take?',
                         'Is hosting included?',
                         'Do you write the content?',
                         'Is SEO included?',
                         'What counts as out of scope?'):
            with self.subTest(question=question):
                self.assertIn(question, html)

    def test_pricing_faq_schema_matches_visible_questions(self):
        """§8 allows FAQPage schema only where the FAQs are visible."""
        html = self.client.get('/pricing/').content.decode()
        faq = next(json.loads(m) for m in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            html, re.S) if '"FAQPage"' in m)
        plain = html.replace('&rsquo;', "'").replace('&mdash;', '—')
        for item in faq['mainEntity']:
            with self.subTest(q=item['name']):
                self.assertIn(item['name'].split('?')[0][:22], plain)

    def test_pricing_card_splits_the_price_into_sized_parts(self):
        """
        The $1,199 maintenance tier is the widest price on the site. It
        must reach the page as three spans, not one flat string —
        rendered flat at the card's 3rem numeral size it overflowed a
        3-up card and broke mid-number ("$1,199/mont" + "h").
        """
        from decimal import Decimal

        from billing.pricing_models import ServiceTier
        ServiceTier.objects.create(
            category='maintenance', name='Dominant', slug='dominant-test',
            price=Decimal('1199'), is_recurring=True,
            billing_interval='month', is_active=True)
        html = self.client.get('/pricing/').content.decode()
        self.assertIn(
            '<div class="card__price">'
            '<span class="card__price-currency">$</span>1,199'
            '<span class="card__price-unit">/month</span></div>',
            html)

    def test_law_firms_has_switching_faq(self):
        html = self.client.get('/for-law-firms/').content.decode()
        for topic in ('locked into a contract', 'owns my domain',
                      'take my content', 'site go down',
                      'lose my Google rankings'):
            with self.subTest(topic=topic):
                self.assertIn(topic, html)

    def test_about_links_credentials_to_verification(self):
        """§11 — an unverifiable credential claim is worth less than none."""
        html = self.client.get('/about/').content.decode()
        self.assertIn('isc2.org', html)
        self.assertIn('CISSP', html)

    def test_about_says_who_it_is_not_for(self):
        html = self.client.get('/about/').content.decode()
        self.assertIn('Who This Is For', html)
        self.assertIn('builder is genuinely better value', html)

    def test_security_claims_stay_honest(self):
        """
        §15 forbids implying security itself boosts rankings. The about
        and law-firm-seo pages both make security arguments, so assert
        neither crosses that line.
        """
        for path in ('/about/', '/services/seo/law-firm-seo/'):
            with self.subTest(path=path):
                html = self.client.get(path).content.decode().lower()
                for claim in ('security improves your ranking',
                              'security boosts your ranking',
                              'secure sites rank higher'):
                    self.assertNotIn(claim, html)


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
