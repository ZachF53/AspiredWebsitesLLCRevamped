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

    Sept 2026 repositioning: the law-firm/small-business/SEO/social
    pages that used to sit here now 301 to service_web_design (see
    public/urls.py) rather than rendering, so they're gone from this
    list — a sitemap must only contain canonical, indexable, HTTP-200
    URLs (§8), same rule CaseStudySitemap below already follows.
    """
    priority = 0.9
    changefreq = 'monthly'
    pages = [
        'public:service_web_design',
        'public:service_review_automation',
        'public:location_san_antonio',
        'public:location_atlanta',
        'public:location_warner_robins',
    ]


class StrongIntentSitemap(_StaticPageMixin, Sitemap):
    """
    Pricing, portfolio, audit — high-intent funnels.

    portfolio_other added Sept 2026 once its noindex was lifted — it's
    the only real proof of finished work while the HVAC portfolio is
    still empty. Lower priority than the HVAC portfolio it isn't
    meant to compete with, so priority is a per-item method rather than
    the mixin's flat class attribute.
    """
    changefreq = 'monthly'
    pages = [
        'public:pricing',
        'public:portfolio',
        'public:audit',
    ]

    def items(self):
        return list(self.pages) + ['public:portfolio_other']

    def priority(self, item):
        return 0.6 if item == 'public:portfolio_other' else 0.8


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


class CaseStudySitemap(Sitemap):
    """
    Published case studies (Master Plan §11).

    Model-driven rather than a static list, so publishing a study in
    the admin puts it in the sitemap without a deploy. Only published
    rows are included — the detail view 404s on anything else, and a
    sitemap must contain only canonical, indexable, HTTP-200 URLs (§8).
    """
    priority = 0.8
    changefreq = 'yearly'

    def items(self):
        from clients.models import CaseStudy
        return CaseStudy.objects.filter(
            is_published=True).exclude(slug='').exclude(slug=None)

    def location(self, item):
        return item.get_absolute_url()

    def lastmod(self, item):
        return item.updated_at


class ArticleSitemap(Sitemap):
    """Published /insights/ articles (§12). Model-driven — publishing
    in the admin puts a post in the sitemap without a deploy."""
    priority = 0.6
    changefreq = 'monthly'

    def items(self):
        from public.models import Article
        return Article.objects.filter(status='published')

    def location(self, item):
        return item.get_absolute_url()

    def lastmod(self, item):
        return item.updated_at


SITEMAPS = {
    'core':       CoreSitemap,
    'services':   ServiceSitemap,
    'funnels':    StrongIntentSitemap,
    'secondary':  SecondarySitemap,
    'casestudies': CaseStudySitemap,
    'insights':   ArticleSitemap,
    'legal':      LegalSitemap,
}
