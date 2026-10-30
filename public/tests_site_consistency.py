"""
Every public route answers, and none of them carry stale facts.

The brief that prompted this described pages rendering an old nav and
footer: no "AI Voice Receptionist" or "Free Audit" link, "Hosting &
Maintenance" pointing at /pricing/, a footer saying "About Me" and
"Other Businesses We've Helped", and the old (210) 896-2536 number.

None of that was true of the templates. All 36 public templates extend
one base.html, and it already had the current nav, footer and phone
number. What matched the description exactly was docs/baseline/html/ --
an archived capture of production taken 2026-09-26, before the Oct 2026
repositioning landed. The live site was behind the repo, not the repo
behind the site.

So this file exists to make the claim checkable rather than argued: it
walks the URLconf, fetches every public page, and fails on a bad status
or on any of those stale strings. If a page ever does drift onto an old
nav or an old number, this says which page and which string.

Routes are discovered from the URLconf, not listed by hand. A hardcoded
list is exactly how /services/ai-receptionist/ could be added to the
site and missed by its own coverage test.
"""
import re

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver
from django.urls.resolvers import RoutePattern

# Stale facts that must never render again, each with the reason -- a
# bare blocklist invites someone to delete an entry they cannot explain.
#
# Matched case-insensitively against rendered HTML.
FORBIDDEN = [
    # Old business number, replaced Oct 2026 (clients/migrations/0076).
    ('896-2536', 'the old phone number'),
    ('8962536', 'the old phone number, unformatted'),
    # Footer said "About Me" when the site was one person's portfolio.
    ('about me', 'first-person footer wording'),
    ("other businesses we", 'a footer section that no longer exists'),
]

# These are only defects when they are talking about the REVIEW SYSTEM.
# The home page and web-design page both say SEO fundamentals are "built
# in, not sold separately" -- true, SEO is not a product here -- so these
# cannot be failed on sight.
#
# Same rule and the same window as public/tests_review_system_standalone,
# which documented it first; this file re-checks it per-page across every
# route rather than only the four known selling pages.
REVIEW_CONTEXT_PHRASES = [
    ('not sold on its own', 'review automation is sold standalone'),
    ('not sold separately', 'review automation is sold standalone'),
    ('not available on its own', 'review automation is sold standalone'),
]
CONTEXT_CHARS = 180


def _talks_about_reviews_near(html, phrase):
    """Occurrences of `phrase` whose surrounding text mentions reviews.

    Returns the snippets, so a failure names the actual sentence instead
    of asserting something is wrong somewhere in 40KB of HTML.
    """
    hits = []
    start = 0
    while True:
        index = html.find(phrase, start)
        if index == -1:
            return hits
        window = html[max(0, index - CONTEXT_CHARS):
                      index + len(phrase) + CONTEXT_CHARS]
        if 'review' in window:
            hits.append(' '.join(window.split()))
        start = index + len(phrase)

# Routes where a plain anonymous GET proves nothing about the site, so
# including them would only add permanent noise. Everything else must
# answer 200 and render clean copy.
SKIP_PREFIXES = (
    '/admin/',       # Django admin
    # Internal admin. Every one of these redirects an anonymous GET to
    # the login page, so including them tests the login page ~130 times
    # and asserts nothing about the public site.
    '/admin-dashboard/',
    '/api/',         # JSON, machine clients only
    '/ws/',          # websockets
    '/billing/',     # webhooks + authenticated checkout
    '/portal/',      # authenticated client portal
    '/onboarding/',  # authenticated wizard
    '/sendgrid/',    # inbound webhook
    '/instantly/',   # inbound webhook
    '/proposals/',   # token-gated
    '/intelligence/',  # token-gated
    '/nps/',         # token-gated
    '/ref/',         # referral redirector
)
SKIP_EXACT = {
    # Need a finished audit in the session; covered by
    # tests_audit_lead_capture.
    '/audit/results/',
    '/audit/ai-review/',
    # Logs the caller out -- would clobber the test client's session.
    '/logout/',
    # Destination for re-pointed DNS, not linked from the site.
    '/parked/',
    # HMAC-token gated: a bare GET is correctly a 400.
    '/maintenance/start/',
    # RedirectViews to files under STATIC_URL. The test client does not
    # serve collected static files, so these 404 here and 200 in
    # production -- asserting on them would test staticfiles, not the
    # site.
    '/favicon.ico',
    '/apple-touch-icon.png',
    '/site.webmanifest',
}


def _public_paths():
    """Every GET-able, parameterless path in the URLconf.

    path()-declared routes only. re_path() entries are skipped: the
    legacy-redirect table (public/legacy_redirects.py) is a long list of
    anchored regexes like r'^blog/some\\-old\\-post/$', and str() on
    those yields the pattern source, not a URL -- fetching it produces a
    guaranteed 404 that says nothing about the live site. Those
    redirects have their own coverage.

    Parameterised routes are skipped here and covered by the detail-page
    test below, which supplies real slugs from seeded content.
    """
    found = []

    def walk(patterns, prefix=''):
        for p in patterns:
            if isinstance(p, URLResolver):
                if not isinstance(p.pattern, RoutePattern):
                    continue
                walk(p.url_patterns, prefix + str(p.pattern))
            elif isinstance(p, URLPattern):
                if not isinstance(p.pattern, RoutePattern):
                    continue
                route = prefix + str(p.pattern)
                if not route.startswith('/'):
                    route = '/' + route
                # Parameterless only: no <int:pk>, <slug:slug>.
                if '<' in route:
                    continue
                found.append(route)

    walk(get_resolver().url_patterns)
    return sorted(set(found))


@override_settings(PRODUCTION_HOST='aspiredwebsites.com',
                   ALLOWED_HOSTS=['aspiredwebsites.com', 'testserver'],
                   SECURE_SSL_REDIRECT=False)
class SiteConsistencyTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        for command in ('seed_pricing', 'seed_case_studies', 'seed_insights'):
            call_command(command, verbosity=0)

    def _get(self, path):
        return self.client.get(path, HTTP_HOST='aspiredwebsites.com',
                               follow=True)

    def _candidates(self):
        for path in _public_paths():
            if path in SKIP_EXACT:
                continue
            if any(path.startswith(p) for p in SKIP_PREFIXES):
                continue
            yield path

    def test_the_sweep_actually_covers_the_site(self):
        """Guard against the sweep silently shrinking.

        Every other test here iterates _candidates(). If a future skip
        rule or a URLconf refactor emptied that, they would all pass by
        checking nothing. The floor is deliberately well under the
        current count (~40) so ordinary route changes do not trip it.
        """
        paths = list(self._candidates())
        self.assertGreater(len(paths), 25)
        for must_have in ('/', '/pricing/', '/audit/', '/about/',
                          '/contact/', '/services/ai-receptionist/',
                          '/services/review-automation/'):
            self.assertIn(must_have, paths)

    def test_every_public_route_answers(self):
        broken = []
        for path in self._candidates():
            try:
                resp = self._get(path)
            except Exception as exc:  # noqa: BLE001
                broken.append(f'{path} raised {type(exc).__name__}: {exc}')
                continue
            # 405 means the route exists but is not a GET page (a
            # POST-only form target or webhook). That is a fact about
            # the route, not a broken page.
            if resp.status_code not in (200, 405):
                broken.append(f'{path} -> {resp.status_code}')
        self.assertEqual(broken, [], 'routes not returning 200:\n' +
                         '\n'.join(broken))

    def _stale_facts_in(self, path, html):
        """Forbidden strings plus review-context phrases, for one page."""
        problems = []
        for needle, why in FORBIDDEN:
            if needle in html:
                problems.append(f'{path}: {needle!r} ({why})')
        for needle, why in REVIEW_CONTEXT_PHRASES:
            for snippet in _talks_about_reviews_near(html, needle):
                problems.append(f'{path}: {needle!r} ({why}) in: {snippet}')
        return problems

    def test_no_page_carries_a_stale_fact(self):
        hits = []
        for path in self._candidates():
            resp = self._get(path)
            if resp.status_code != 200:
                continue
            html = resp.content.decode('utf-8', 'replace').lower()
            hits.extend(self._stale_facts_in(path, html))
        self.assertEqual(hits, [], 'stale copy still rendering:\n' +
                         '\n'.join(hits))

    def test_detail_pages_answer_and_are_clean(self):
        """Slug routes, with real slugs from the seeded content."""
        from clients.models import CaseStudy
        from public.models import Article

        paths = []
        for cs in CaseStudy.objects.filter(is_published=True)[:5]:
            paths.append(f'/portfolio/{cs.slug}/')
        for art in Article.objects.filter(status='published')[:5]:
            paths.append(f'/insights/{art.slug}/')
        self.assertTrue(paths, 'no seeded detail pages to check')

        problems = []
        for path in paths:
            resp = self._get(path)
            if resp.status_code != 200:
                problems.append(f'{path} -> {resp.status_code}')
                continue
            html = resp.content.decode('utf-8', 'replace').lower()
            problems.extend(self._stale_facts_in(path, html))
        self.assertEqual(problems, [], '\n'.join(problems))

    def test_nav_and_footer_come_from_the_shared_includes(self):
        """Both are defined once. If a page ever inlines its own copy,
        the count goes above one and this catches it."""
        html = self._get('/').content.decode()
        self.assertEqual(html.count('class="site-header"'), 1)
        self.assertEqual(html.count('class="site-footer"'), 1)

    def test_every_page_shows_the_current_nav_and_footer(self):
        """The specific links the brief reported missing."""
        for path in ('/', '/pricing/', '/audit/',
                     '/services/review-automation/',
                     '/services/ai-receptionist/', '/about/', '/contact/',
                     '/login/', '/portfolio/', '/insights/',
                     '/locations/atlanta/'):
            with self.subTest(path=path):
                html = self._get(path).content.decode()
                # Nav
                self.assertIn('AI Voice Receptionist', html)
                self.assertIn('Free Audit', html)
                self.assertIn('/services/hosting-maintenance/', html)
                # Footer
                self.assertIn('(210) 201-0375', html)
                self.assertIn('Warner Robins, GA', html)
                self.assertIn('>About<', html)

    def test_the_hosting_nav_link_does_not_point_at_pricing(self):
        """The reported symptom was Hosting & Maintenance -> /pricing/."""
        html = self._get('/').content.decode()
        self.assertNotRegex(
            html, r'href="/pricing/"[^>]*>\s*Hosting')
