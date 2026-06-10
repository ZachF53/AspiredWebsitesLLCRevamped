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
from public.sitemaps import SITEMAPS
from reporting.views import nps_response


def robots_txt(request):
    """Bare-bones robots.txt — allow crawl + point at sitemap."""
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
    # Phase 5a — Social Media Manager (mounted under /admin-dashboard/social/
    # so the URL prefix stays consistent with admin dashboard routes).
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

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
