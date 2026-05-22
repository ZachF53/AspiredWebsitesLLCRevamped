"""URL configuration for AspiredWebsitesRevamped project."""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('admin-dashboard/vault/', include('vault.urls')),
    path('admin-dashboard/', include('admin_dashboard.urls', namespace='admin_dashboard')),
    path('portal/', include('clients.urls')),
    path('billing/', include('billing.urls')),
    path('maintenance/', include('sync.maintenance_urls')),
    path('api/sync/', include('sync.urls')),
    path('', include('public.urls', namespace='public')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
