"""URL configuration for AspiredWebsitesRevamped project."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from billing.views import pay_invoice, pay_success
from clients.views import (
    intelligence_approve, intelligence_decline, onboarding_setup,
    proposal_view_tracking, referral_click,
)
from outreach.sendgrid_webhook import receive as sendgrid_events
from reporting.views import nps_response

urlpatterns = [
    path('nps/<uuid:token>/<int:score>/', nps_response, name='nps_response'),
    # SendGrid Event Webhook — opens/clicks/bounces/spam reports.
    # Public endpoint, locked by ECDSA signature verification against
    # SENDGRID_WEBHOOK_PUBLIC_KEY (rejects ALL POSTs when unset).
    path('sendgrid/events/', sendgrid_events, name='sendgrid_events'),
    path('admin/', admin.site.urls),
    path('admin-dashboard/vault/', include('vault.urls')),
    path('admin-dashboard/', include('admin_dashboard.urls', namespace='admin_dashboard')),
    path('portal/', include('clients.urls')),
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
