"""
Sitemap definitions for the public marketing site.

Wired into the project URL conf via django.contrib.sitemaps.views.sitemap.
Every URL here is canonical (no trailing /index/, no ?qs); Google + Bing
crawl this for fast discovery of new pages.

Priority rationale:
  1.0  home — root
  0.9  services/<*> — money pages that should rank
  0.8  for-law-firms, pricing, portfolio, audit — strong intent
  0.7  about, contact, design/schedule
  0.5  refund-policy, privacy-policy, terms-of-service — required, low-priority
"""

from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class _StaticPageMixin:
    """Drop-in base for a sitemap whose 'items' are URL-conf names."""
    changefreq = 'monthly'

    def items(self):
        return list(self.pages)

    def location(self, item):
        return reverse(item)


class CoreSitemap(_StaticPageMixin, Sitemap):
    """Homepage + top-level marketing pages."""
    priority = 1.0
    pages = ['public:home']


class ServiceSitemap(_StaticPageMixin, Sitemap):
    """
    Service pages — the SEO money pages.

    Phase 2 children sit at the same 0.9 priority as their hubs: the
    law-firm pages carry more measured commercial value than the
    generic hubs above them (law firm SEO alone is 8,000 searches/mo
    at $31-165 bids), so demoting them by depth would misrepresent
    their importance.
    """
    priority = 0.9
    changefreq = 'monthly'
    pages = [
        'public:service_web_design',
        'public:service_digital_marketing',
        'public:service_seo',
        'public:law_firms',
        # ── Phase 2 ──
        'public:service_law_firm_seo',
        'public:service_law_firm_web_design',
        'public:service_local_seo',
        'public:service_small_business_web_design',
        'public:service_website_redesign',
    ]


class StrongIntentSitemap(_StaticPageMixin, Sitemap):
    """Pricing, portfolio, audit — high-intent funnels."""
    priority = 0.8
    changefreq = 'monthly'
    pages = [
        'public:pricing',
        'public:portfolio',
        'public:audit',
    ]


class SecondarySitemap(_StaticPageMixin, Sitemap):
    """About, contact, schedule — discovery + conversion supplementary."""
    priority = 0.7
    changefreq = 'yearly'
    pages = [
        'public:about',
        'public:contact',
        'scheduler:design_schedule',
    ]


class LegalSitemap(_StaticPageMixin, Sitemap):
    """Privacy / terms / refund — required, low priority."""
    priority = 0.5
    changefreq = 'yearly'

    def items(self):
        items = []
        # Optional — only include if the URL name resolves
        for name in ('core:privacy_policy',
                     'core:terms_of_service',
                     'core:refund_policy'):
            try:
                reverse(name)
            except Exception:
                continue
            items.append(name)
        return items

    def location(self, item):
        return reverse(item)


SITEMAPS = {
    'core':       CoreSitemap,
    'services':   ServiceSitemap,
    'funnels':    StrongIntentSitemap,
    'secondary':  SecondarySitemap,
    'legal':      LegalSitemap,
}
