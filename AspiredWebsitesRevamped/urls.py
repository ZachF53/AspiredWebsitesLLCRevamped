"""URL configuration for AspiredWebsitesRevamped project."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap as sitemap_view
from django.http import HttpResponse
from django.urls import include, path

from billing.views import pay_invoice, pay_success
from clients.views import (
    intelligence_approve, intelligence_decline, onboarding_setup,
    proposal_view_tracking, referral_click,
)
from outreach.sendgrid_webhook import receive as sendgrid_events
from public.legacy_redirects import legacy_redirect_patterns
from public.sitemaps import SITEMAPS
from reporting.views import nps_response


def robots_txt(request):
    """
    robots.txt — allow the marketing site, keep app surfaces out.

    Master Plan §8: robots.txt must allow CSS/JS/images, declare the
    sitemap, and block client-portal/billing/login paths — but it is
    never a substitute for ``noindex``. A URL disallowed here can still
    be indexed from an external link, and Google cannot see a noindex
    tag on a page it is not allowed to crawl.

    So the split is deliberate:
      - Disallow  → application surfaces with nothing to index and real
                    crawl cost (dashboards, APIs, token-gated flows).
      - noindex   → pages that must stay crawlable so the directive is
                    actually read (/login/, /audit/results/, thanks and
                    password-reset pages). See core/templates/
                    _meta_noindex.html.
    """
    # Non-production hosts (staging) disallow everything. Staging has
    # public DNS and a valid certificate, so an "Allow: /" there invites
    # Google to index a full duplicate of the production site. Paired
    # with the sitewide noindex in base.html: robots.txt keeps crawlers
    # out, and the meta tag handles anything already discovered.
    if request.get_host().split(':', 1)[0].lower() != getattr(
            settings, 'PRODUCTION_HOST', 'aspiredwebsites.com').lower():
        return HttpResponse(
            "User-agent: *\nDisallow: /\n", content_type='text/plain')

    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin/\n"
        "Disallow: /admin-dashboard/\n"
        "Disallow: /portal/\n"
        "Disallow: /onboarding/\n"
        "Disallow: /pay/\n"
        "Disallow: /set-password/\n"
        "Disallow: /maintenance/\n"
        "Disallow: /api/\n"
        "Disallow: /sendgrid/\n"
        "Disallow: /ref/\n"
        "Disallow: /proposals/\n"
        "Disallow: /intelligence/\n"
        "Disallow: /nps/\n"
        "\n"
        "Sitemap: https://aspiredwebsites.com/sitemap.xml\n"
    )
    return HttpResponse(body, content_type='text/plain')


urlpatterns = [
    path('sitemap.xml', sitemap_view, {'sitemaps': SITEMAPS},
         name='django.contrib.sitemaps.views.sitemap'),
    path('robots.txt', robots_txt, name='robots_txt'),
    path('nps/<uuid:token>/<int:score>/', nps_response, name='nps_response'),
    # SendGrid Event Webhook — opens/clicks/bounces/spam reports.
    # Public endpoint, locked by ECDSA signature verification against
    # SENDGRID_WEBHOOK_PUBLIC_KEY (rejects ALL POSTs when unset).
    path('sendgrid/events/', sendgrid_events, name='sendgrid_events'),
    path('admin/', admin.site.urls),
    path('admin-dashboard/vault/', include('vault.urls')),
    # Phase 5a-pivot — Google Business Profile lives in reporting/ as
    # a maintenance-plan feature (tier ≥ Growth).
    path('admin-dashboard/gbp/',
         include('reporting.urls_gbp', namespace='gbp')),
    # Phase 5b/5c — Meta + LinkedIn social media manager.
    path('admin-dashboard/social/',
         include('social.urls', namespace='social')),
    path('admin-dashboard/', include('admin_dashboard.urls', namespace='admin_dashboard')),
    path('portal/', include('clients.urls')),
    path('onboarding/', include('onboarding.urls', namespace='onboarding')),
    path('', include('scheduler.urls', namespace='scheduler')),
    # Magic-link password setup — Phase 6
    path('set-password/<uuid:token>/',
         __import__('onboarding.password_views', fromlist=['set_password']).set_password,
         name='set_password'),
    path('portal/domains/', include('domains.urls')),
    path('billing/', include('billing.urls')),

    # Public payment pages — token-gated, no auth required. Mounted at
    # the root so URLs read /pay/<token>/ instead of
    # /billing/pay/<token>/ (shorter, friendlier to paste into email).
    path('pay/<uuid:token>/', pay_invoice, name='pay_invoice'),
    path('pay/<uuid:token>/success/',
         pay_success, name='pay_success'),
    path('maintenance/', include('sync.maintenance_urls')),
    path('api/sync/', include('sync.urls')),
    path('api/', include('reporting.urls')),

    # Phase 7 Part 2 — public referral + proposal tracking endpoints
    path('ref/<str:code>/', referral_click, name='referral_click'),
    path('proposals/view/<uuid:token>/', proposal_view_tracking,
         name='proposal_view_tracking'),

    # Phase 7 Part 3 — public intelligence approve / decline magic links
    path('intelligence/respond/<uuid:token>/approve/',
         intelligence_approve, name='intelligence_approve'),
    path('intelligence/respond/<uuid:token>/decline/',
         intelligence_decline, name='intelligence_decline'),

    # Onboarding setup (public — token authenticates) — Part 4
    path('onboarding/setup/<uuid:token>/',
         onboarding_setup, name='onboarding_setup'),

    # Legal pages — mounted before public.urls so the specific paths
    # /privacy-policy/ and /terms/ resolve via core.views rather than
    # being swallowed by anything generic in public.
    path('', include('core.urls', namespace='core')),

    path('', include('public.urls', namespace='public')),
]

# 301s for the old WordPress URLs (see public/legacy_redirects.py).
# Appended last so a legacy path can never shadow a live route — if a
# real page ever occupies one of these paths, the real page wins.
urlpatterns += legacy_redirect_patterns()

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
