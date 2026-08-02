"""
301s for URLs inherited from the WordPress site this app replaced.

Search Console's "Not found (404)" report (Aug 2026) listed 17 URLs Google
still had on file from the old Astra/WordPress build. Most are worthless —
`wp-*.php` probes, theme asset paths, a bot-invented subdomain — and a 404
is the correct, honest answer for those. They are deliberately NOT listed
here.

What IS listed is the handful of real pages that still carry ranking
signal. The San Antonio and Georgia service pages matter most: the site
currently ranks 14-41 for "law firm website design san antonio", "web
design san antonio" and similar on GENERIC pages, because the specific
pages that earned those positions 404. Georgia has no impressions at all
despite an Atlanta address, for the same reason — all three Georgia pages
died in the migration.

Targets are URL *names*, resolved per-request by RedirectView, so a later
change to a path updates the redirect automatically. Where the old page
has no exact replacement it points at the closest topical parent rather
than the homepage — a redirect to an unrelated page is treated as a soft
404 by Google and helps nobody.

Only known URLs are redirected. There is no catch-all: an unknown legacy
URL should 404 rather than dump a visitor somewhere arbitrary.
"""

import re

from django.urls import re_path
from django.views.generic import RedirectView

# (old path without leading/trailing slash, target URL name)
LEGACY_REDIRECTS = [
    # ── Location pages — highest recoverable value ──────────────────
    ('services/san-antonio-web-design', 'public:location_san_antonio'),
    ('services/san-antonio-seo', 'public:location_san_antonio'),

    # ── Georgia service pages ───────────────────────────────────────
    # Repointed Aug 2026, now that Georgia pages exist (revised D5).
    # These were landing on national hubs, which is a weak match for a
    # geo-scoped URL — the closer the target's topic and geography, the
    # more of the old signal survives the hop.
    #   georgia-seo        → local SEO, which is what a place-scoped SEO
    #                        page actually was; the national hub was the
    #                        best available match before, not a good one
    #   web-design-georgia → the Atlanta page, the Georgia web-design
    #                        page with real demand behind it
    #   georgia-marketing  → unchanged; there is no Georgia marketing
    #                        page and inventing one to catch a redirect
    #                        would be backwards
    ('services/georgia-seo', 'public:service_local_seo'),
    ('services/web-design-georgia', 'public:location_atlanta'),
    ('services/georgia-marketing', 'public:service_digital_marketing'),

    # ── Old WordPress blog ──────────────────────────────────────────
    # /blog/ became /insights/. These two posts had crawl history; each
    # goes to the service page covering the same topic rather than to
    # the /insights/ index, which would be a soft 404.
    ('blog/what-is-seo', 'public:service_seo'),
    ('blog/advantages-of-having-a-professional-web-design',
     'public:service_web_design'),

    # ── Renamed / removed pages ─────────────────────────────────────
    ('our-privacy-policy', 'core:privacy_policy'),
    # A page-builder artifact from the old theme. Nothing maps to it;
    # home is the honest destination.
    ('heros', 'public:home'),
]


def legacy_redirect_patterns():
    """
    Build the URL patterns. Matches with OR without a trailing slash in
    one pattern — Google recorded most of these unslashed, and letting
    ``APPEND_SLASH`` handle that would produce a 301 chain (unslashed →
    slashed → target) where one hop will do.
    """
    return [
        re_path(
            r'^{}/?$'.format(re.escape(old)),
            RedirectView.as_view(pattern_name=name, permanent=True),
            name='legacy_{}'.format(old.replace('/', '_').replace('-', '_')),
        )
        for old, name in LEGACY_REDIRECTS
    ]
