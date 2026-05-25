"""Client portal URL routes (mounted at /portal/)."""

from django.urls import path

from . import views

app_name = 'clients'

urlpatterns = [
    # ── Phase C — Website chooser (account → pick which site to enter) ──
    # Login redirects here on every fresh sign-in; the chooser
    # auto-skips to the dashboard when the account has exactly one
    # website so single-site users don't see an interstitial.
    path('chooser/', views.chooser, name='chooser'),
    path('chooser/pick/<slug:slug>/', views.chooser_pick,
         name='chooser_pick'),

    path('', views.dashboard, name='dashboard'),
    path('project/', views.project_detail, name='project'),
    path('intake/', views.intake, name='intake'),
    path('intake/save/', views.intake_save, name='intake_save'),
    path('intake/photos/upload/',
         views.intake_photo_upload, name='intake_photo_upload'),
    path('intake/photos/<uuid:photo_id>/delete/',
         views.intake_photo_delete, name='intake_photo_delete'),
    path('files/', views.files, name='files'),
    path('files/upload/', views.file_upload, name='file_upload'),
    path('revisions/', views.revisions, name='revisions'),
    path('revisions/new/', views.revision_new, name='revision_new'),
    path('support/', views.support, name='support'),
    path('support/new/', views.support_new, name='support_new'),
    path('invoices/', views.invoices, name='invoices'),

    # Subscriptions + saved payment methods (Stripe Elements card add).
    path('subscriptions/', views.portal_subscriptions,
         name='portal_subscriptions'),
    path('subscriptions/payment-methods/add/',
         views.portal_payment_method_add,
         name='portal_payment_method_add'),
    path('subscriptions/payment-methods/<str:pm_id>/remove/',
         views.portal_payment_method_remove,
         name='portal_payment_method_remove'),
    path('subscriptions/payment-methods/<str:pm_id>/default/',
         views.portal_payment_method_default,
         name='portal_payment_method_default'),
    # Phase C4 — per-subscription default card override.
    path('subscriptions/<str:sub_id>/payment-method/',
         views.portal_subscription_payment_method,
         name='portal_subscription_payment_method'),

    # Maintenance-plan upsell + signup
    path('maintenance/', views.portal_maintenance,
         name='portal_maintenance'),
    path('maintenance/success/', views.portal_maintenance_success,
         name='portal_maintenance_success'),
    path('maintenance/start/<slug:slug>/',
         views.portal_maintenance_start,
         name='portal_maintenance_start'),
    path('maintenance/cancel/', views.portal_maintenance_cancel,
         name='portal_maintenance_cancel'),
    path('maintenance/resume/', views.portal_maintenance_resume,
         name='portal_maintenance_resume'),
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
