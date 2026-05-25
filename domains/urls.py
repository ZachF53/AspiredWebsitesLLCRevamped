"""Domain portal URLs (mounted at /portal/domains/)."""

from django.urls import path

from . import views

app_name = 'domains'

urlpatterns = [
    path('', views.portal_domains, name='portal_domains'),
    path('search/', views.portal_domains_search,
         name='portal_domains_search'),
    path('register/<str:domain>/', views.portal_domain_register,
         name='portal_domain_register'),
    path('<uuid:pk>/', views.portal_domain_detail,
         name='portal_domain_detail'),
    path('<uuid:pk>/dns/', views.portal_domain_dns,
         name='portal_domain_dns'),
    path('<uuid:pk>/cancel/', views.portal_domain_cancel,
         name='portal_domain_cancel'),
    path('<uuid:pk>/resume/', views.portal_domain_resume,
         name='portal_domain_resume'),
    path('<uuid:pk>/delete/', views.portal_domain_delete,
         name='portal_domain_delete'),
]
