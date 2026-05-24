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
    path('seo/', views.portal_seo, name='portal_seo'),
    path('reports/', views.portal_reports, name='portal_reports'),
    path('reports/<uuid:report_id>/download/', views.portal_report_download, name='portal_report_download'),
    path('reports/annual/<uuid:report_id>/download/',
         views.portal_annual_report_download,
         name='portal_annual_report_download'),
    path('security/', views.portal_security, name='portal_security'),
    path('security/<uuid:scan_id>/download/',
         views.portal_scan_download, name='portal_scan_download'),
    path('settings/', views.settings_page, name='settings'),

    # Phase 7 Part 2 — refer-a-friend
    path('referral/', views.portal_referral, name='portal_referral'),

    # Phase 7 Part 3 — website intelligence recommendations
    path('suggestions/', views.portal_suggestions,
         name='portal_suggestions'),

    # Tier 2 — session recordings
    path('recordings/', views.portal_recordings,
         name='portal_recordings'),
    path('recordings/<uuid:rec_id>/replay/',
         views.portal_recording_replay,
         name='portal_recording_replay'),
    path('recordings/<uuid:rec_id>/download/',
         views.portal_recording_download,
         name='portal_recording_download'),

    # Contract signing — token-gated, no login required.
    path('contract/signed/', views.contract_signed, name='contract_signed'),
    path('contract/<uuid:contract_token>/', views.contract_sign, name='contract_sign'),
]
