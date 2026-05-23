"""URL configuration for AspiredWebsitesRevamped project."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from clients.views import proposal_view_tracking, referral_click
from reporting.views import nps_response

urlpatterns = [
    path('nps/<uuid:token>/<int:score>/', nps_response, name='nps_response'),
    path('admin/', admin.site.urls),
    path('admin-dashboard/vault/', include('vault.urls')),
    path('admin-dashboard/', include('admin_dashboard.urls', namespace='admin_dashboard')),
    path('portal/', include('clients.urls')),
    path('billing/', include('billing.urls')),
    path('maintenance/', include('sync.maintenance_urls')),
    path('api/sync/', include('sync.urls')),
    path('api/', include('reporting.urls')),

    # Phase 7 Part 2 — public referral + proposal tracking endpoints
    path('ref/<str:code>/', referral_click, name='referral_click'),
    path('proposals/view/<uuid:token>/', proposal_view_tracking,
         name='proposal_view_tracking'),

    path('', include('public.urls', namespace='public')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
