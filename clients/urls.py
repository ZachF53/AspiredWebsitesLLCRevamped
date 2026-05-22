"""Client portal URL routes (mounted at /portal/)."""

from django.urls import path

from . import views

app_name = 'clients'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('project/', views.project_detail, name='project'),
    path('intake/', views.intake, name='intake'),
    path('intake/save/', views.intake_save, name='intake_save'),
    path('files/', views.files, name='files'),
    path('files/upload/', views.file_upload, name='file_upload'),
    path('revisions/', views.revisions, name='revisions'),
    path('revisions/new/', views.revision_new, name='revision_new'),
    path('support/', views.support, name='support'),
    path('support/new/', views.support_new, name='support_new'),
    path('invoices/', views.invoices, name='invoices'),
    path('credentials/', views.portal_credentials, name='credentials'),
    path('credentials/reauth/', views.portal_credentials_reauth,
         name='credentials_reauth'),
    path('changelog/', views.portal_changelog, name='portal_changelog'),
    path('settings/', views.settings_page, name='settings'),

    # Contract signing — token-gated, no login required.
    path('contract/signed/', views.contract_signed, name='contract_signed'),
    path('contract/<uuid:contract_token>/', views.contract_sign, name='contract_sign'),
]
