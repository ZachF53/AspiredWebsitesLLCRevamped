from django.urls import path

from . import views


app_name = 'admin_dashboard'

urlpatterns = [
    path('', views.home, name='home'),

    # Phase 7 Part 1 — Business Intelligence
    path('intelligence/', views.intelligence_dashboard,
         name='intelligence_dashboard'),

    # Leads
    path('leads/', views.leads_table, name='leads_table'),
    path('leads/kanban/', views.leads_kanban, name='leads_kanban'),
    path('leads/add/', views.lead_add, name='lead_add'),
    path('leads/import/', views.lead_import, name='lead_import'),
    path('leads/scrape/', views.scrape, name='scrape'),
    path('leads/<int:pk>/', views.lead_detail, name='lead_detail'),
    path('leads/<int:pk>/edit/', views.lead_edit, name='lead_edit'),
    path('leads/<int:pk>/delete/', views.lead_delete, name='lead_delete'),
    path('leads/<int:pk>/reenrich/', views.lead_reenrich,
         name='lead_reenrich'),
    path('leads/bulk-delete/', views.lead_bulk_delete,
         name='lead_bulk_delete'),
    # HTMX partials — fragment responses, not full pages
    path('leads/<int:pk>/htmx/status/', views.lead_update_status, name='lead_update_status'),
    path('leads/<int:pk>/htmx/notes/', views.lead_add_note, name='lead_add_note'),
    path('leads/<int:pk>/htmx/move/', views.lead_kanban_move, name='lead_kanban_move'),

    # Reply triage
    path('needs-you/', views.needs_you, name='needs_you'),
    path('needs-you/<int:pk>/draft/', views.needs_you_draft, name='needs_you_draft'),
    path('needs-you/<int:pk>/send/', views.needs_you_send, name='needs_you_send'),
    path('needs-you/<int:pk>/archive/', views.needs_you_archive, name='needs_you_archive'),
    path('needs-you/<int:pk>/unsubscribe/', views.needs_you_unsubscribe, name='needs_you_unsubscribe'),
    path('needs-you/intake-review/<uuid:client_id>/done/',
         views.intake_review_mark_done,
         name='intake_review_mark_done'),

    # Outreach automation config
    path('settings/', views.settings_view, name='settings'),

    # Stripe customer recovery — relink a client to a specific
    # Stripe customer ID when the linkage got broken (e.g. an
    # orphaned customer with their saved card).
    path('clients/<uuid:client_id>/stripe-customer/',
         views.admin_stripe_customer_recovery,
         name='admin_stripe_customer_recovery'),
    path('clients/<uuid:client_id>/stripe-customer/relink/',
         views.admin_stripe_customer_relink,
         name='admin_stripe_customer_relink'),

    # Domain registrations (Namecheap)
    path('domains/', views.admin_domain_list, name='admin_domain_list'),
    path('domains/config/', views.admin_domain_config,
         name='admin_domain_config'),
    path('domains/config/toggle-sandbox/',
         views.admin_domain_config_toggle,
         name='admin_domain_config_toggle'),
    path('domains/register/', views.admin_domain_register,
         name='admin_domain_register'),
    path('domains/register/check/', views.admin_domain_register_check,
         name='admin_domain_register_check'),
    path('domains/<uuid:reg_id>/', views.admin_domain_detail,
         name='admin_domain_detail'),
    path('domains/<uuid:reg_id>/sync/', views.admin_domain_sync,
         name='admin_domain_sync'),
    path('domains/<uuid:reg_id>/repoint/', views.admin_domain_repoint,
         name='admin_domain_repoint'),
    path('domains/<uuid:reg_id>/dns/', views.admin_domain_dns,
         name='admin_domain_dns'),
    path('domains/<uuid:reg_id>/transfer-out/',
         views.admin_domain_transfer_out,
         name='admin_domain_transfer_out'),
    path('domains/<uuid:reg_id>/resume/',
         views.admin_domain_resume,
         name='admin_domain_resume'),
    path('domains/<uuid:reg_id>/park/',
         views.admin_domain_park,
         name='admin_domain_park'),
    path('domains/<uuid:reg_id>/unpark/',
         views.admin_domain_unpark,
         name='admin_domain_unpark'),
    path('domains/<uuid:reg_id>/delete/',
         views.admin_domain_delete,
         name='admin_domain_delete'),

    # Pricing manager
    path('pricing/', views.pricing_list, name='pricing_list'),
    path('pricing/<uuid:tier_id>/edit/', views.pricing_edit, name='pricing_edit'),
    path('pricing/<uuid:tier_id>/toggle/', views.pricing_toggle, name='pricing_toggle'),
    path('pricing/<uuid:tier_id>/feature/add/', views.pricing_feature_add, name='pricing_feature_add'),
    path('pricing/<uuid:tier_id>/feature/<uuid:fid>/delete/', views.pricing_feature_delete, name='pricing_feature_delete'),

    # Deployment dashboard
    path('deploy/', views.deploy_home, name='deploy_home'),
    path('deploy/fresh/', views.deploy_fresh, name='deploy_fresh'),
    path('deploy/redeploy/', views.deploy_redeploy, name='deploy_redeploy'),
    path('deploy/client/<uuid:client_id>/', views.deploy_client, name='deploy_client'),
    path('deploy/history/', views.deploy_history, name='deploy_history'),
    path('deploy/log/', views.deploy_log_create, name='deploy_log_create'),

    # Site changelog
    path('changelog/', views.changelog_list, name='changelog_list'),
    path('changelog/add/', views.changelog_add, name='changelog_add'),
    path('changelog/import/', views.changelog_import, name='changelog_import'),
    path('changelog/<uuid:entry_id>/edit/', views.changelog_edit, name='changelog_edit'),
    path('changelog/<uuid:entry_id>/delete/', views.changelog_delete, name='changelog_delete'),

    # Clients — monitoring hub (Phase 5a)
    path('clients/', views.client_list, name='client_list'),
    path('clients/onboarding/', views.clients_onboarding, name='clients_onboarding'),
    path('clients/<uuid:client_id>/', views.client_detail, name='client_detail'),
    path('clients/<uuid:client_id>/edit/',
         views.client_edit, name='client_edit'),
    path('clients/<uuid:client_id>/quick-edit-field/',
         views.client_quick_edit_field,
         name='client_quick_edit_field'),
    path('clients/<uuid:client_id>/stage/',
         views.client_change_stage,
         name='client_change_stage'),
    path('clients/<uuid:client_id>/changelog/', views.client_changelog, name='client_changelog'),
    path('clients/<uuid:client_id>/changelog/add/', views.changelog_add, name='changelog_add_client'),
    path('clients/<uuid:client_id>/uptime/', views.client_uptime, name='client_uptime'),
    path('clients/<uuid:client_id>/keywords/', views.client_keywords, name='client_keywords'),
    path('clients/<uuid:client_id>/keywords/add/', views.keyword_add, name='keyword_add'),
    path('clients/<uuid:client_id>/keywords/check/', views.keyword_run_check, name='keyword_run_check'),
    path('clients/<uuid:client_id>/conversions/', views.client_conversions, name='client_conversions'),
    path('clients/<uuid:client_id>/tracker/', views.client_tracker, name='client_tracker'),
    path('clients/<uuid:client_id>/toggle-session-recording/',
         views.client_toggle_session_recording,
         name='client_toggle_session_recording'),

    # Tier 2 — session recordings (rrweb)
    path('clients/<uuid:client_id>/recordings/',
         views.recordings_list, name='recordings_list'),
    path('clients/<uuid:client_id>/recordings/<uuid:rec_id>/replay/',
         views.recording_replay, name='recording_replay'),
    path('clients/<uuid:client_id>/recordings/<uuid:rec_id>/download/',
         views.recording_download, name='recording_download'),
    path('clients/<uuid:client_id>/recordings/<uuid:rec_id>/delete/',
         views.recording_delete, name='recording_delete'),
    path('clients/<uuid:client_id>/recordings/delete-all/',
         views.recording_delete_all,
         name='recording_delete_all'),
    path('clients/<uuid:client_id>/gbp/<uuid:check_id>/flag/', views.gbp_flag, name='gbp_flag'),
    path('clients/<uuid:client_id>/gbp/<uuid:check_id>/resolve/', views.gbp_resolve, name='gbp_resolve'),

    # Phase 5b — monthly reports
    path('reports/', views.reports_list, name='reports_list'),
    path('reports/generate/', views.report_generate_now, name='report_generate_now'),
    path('reports/<uuid:report_id>/resend/', views.report_resend, name='report_resend'),
    path('reports/<uuid:report_id>/download/', views.report_download, name='report_download'),

    # Phase 5b — content freshness
    path('clients/<uuid:client_id>/freshness/', views.client_freshness, name='client_freshness'),
    path('clients/<uuid:client_id>/freshness/generate/', views.freshness_generate, name='freshness_generate'),
    path('clients/<uuid:client_id>/freshness/flag/', views.freshness_flag, name='freshness_flag'),

    # Phase 5b — NPS
    path('nps/', views.nps_list, name='nps_list'),

    # Phase 5b — AI blog generator
    path('blog/', views.blog_list, name='blog_list'),
    path('blog/generate/', views.blog_generate, name='blog_generate'),
    path('blog/<uuid:post_id>/', views.blog_detail, name='blog_detail'),

    # Phase 6b — Droplet dashboard
    path('droplets/', views.droplet_list, name='droplet_list'),
    path('droplets/new/', views.droplet_new, name='droplet_new'),
    path('droplets/table/', views.droplet_table, name='droplet_table'),
    path('droplets/<int:droplet_id>/power/',
         views.droplet_power, name='droplet_power'),
    path('droplets/<int:droplet_id>/destroy/',
         views.droplet_destroy, name='droplet_destroy'),
    path('droplets/<int:droplet_id>/metrics/',
         views.droplet_metrics, name='droplet_metrics'),
    path('droplets/<int:droplet_id>/link-to-website/',
         views.droplet_link_to_website,
         name='droplet_link_to_website'),

    # Phase 6c — vulnerability scans
    path('scans/', views.scans_list, name='scans_list'),
    path('scans/table/', views.scans_table, name='scans_table'),
    path('scans/run/', views.run_scan, name='scan_run'),
    path('scans/<uuid:scan_id>/', views.scan_detail, name='scan_detail'),
    path('scans/<uuid:scan_id>/cancel/',
         views.scan_cancel, name='scan_cancel'),
    path('scans/findings/<uuid:finding_id>/status/',
         views.update_finding_status, name='finding_status'),
    # Phase 6c Part 3 — PDF + send-to-client + auto-send toggle
    path('scans/<uuid:scan_id>/generate-pdf/',
         views.generate_scan_pdf_view, name='scan_generate_pdf'),
    path('scans/<uuid:scan_id>/download-pdf/',
         views.download_scan_pdf, name='scan_download_pdf'),
    path('scans/<uuid:scan_id>/send-to-client/',
         views.send_scan_report, name='scan_send_report'),
    path('clients/<uuid:client_id>/toggle-auto-send-scans/',
         views.toggle_auto_send_scans, name='toggle_auto_send_scans'),

    # Phase 5b — AI chatbot
    path('clients/<uuid:client_id>/chatbot/', views.client_chatbot, name='client_chatbot'),
    path('clients/<uuid:client_id>/chatbot/regenerate-prompt/', views.chatbot_regenerate_prompt, name='chatbot_regenerate_prompt'),
    path('clients/<uuid:client_id>/chatbot/conversations/<uuid:conv_id>/', views.chatbot_conversation, name='chatbot_conversation'),
    path('clients/<uuid:client_id>/testimonial/', views.testimonial_mark_received, name='testimonial_mark_received'),

    # Phase 7 Part 2 — referrals
    path('referrals/', views.referrals_list, name='referrals_list'),
    path('referrals/<uuid:link_id>/toggle/',
         views.referral_toggle_active, name='referral_toggle_active'),
    path('referrals/<uuid:link_id>/conversion/',
         views.referral_mark_conversion,
         name='referral_mark_conversion'),

    # Phase 7 Part 2 — proposals
    path('proposals/', views.proposals_list, name='proposals_list'),
    path('proposals/new/', views.proposal_new, name='proposal_new'),
    path('proposals/lead-autofill/',
         views.proposal_lead_autofill,
         name='proposal_lead_autofill'),
    path('proposals/<uuid:proposal_id>/', views.proposal_detail,
         name='proposal_detail'),
    path('proposals/<uuid:proposal_id>/generate/',
         views.proposal_generate, name='proposal_generate'),
    path('proposals/<uuid:proposal_id>/send/',
         views.proposal_send, name='proposal_send'),
    path('proposals/<uuid:proposal_id>/status/',
         views.proposal_set_status, name='proposal_set_status'),

    # Phase 7 Part 5 — Competitor Content Gap Tracker
    path('competitor-gaps/', views.competitor_gaps_list,
         name='competitor_gaps_list'),
    path('competitor-gaps/<uuid:report_id>/',
         views.competitor_gap_detail,
         name='competitor_gap_detail'),
    path('competitor-gaps/run/<uuid:client_id>/',
         views.competitor_gap_run_now,
         name='competitor_gap_run_now'),
    path('competitor-gaps/<uuid:report_id>/gaps/<int:gap_index>/'
         'create-suggestion/',
         views.gap_create_suggestion,
         name='gap_create_suggestion'),
    path('clients/<uuid:client_id>/competitors/add/',
         views.competitor_add, name='competitor_add'),
    path('clients/<uuid:client_id>/competitors/<uuid:comp_id>/edit/',
         views.competitor_edit, name='competitor_edit'),
    path('clients/<uuid:client_id>/competitors/<uuid:comp_id>/delete/',
         views.competitor_delete, name='competitor_delete'),

    # Phase 7 Part 4 — Annual Business Health Report
    path('annual-reports/', views.annual_reports_list,
         name='annual_reports_list'),
    path('annual-reports/generate/', views.annual_report_generate,
         name='annual_report_generate'),
    path('annual-reports/<uuid:report_id>/',
         views.annual_report_detail,
         name='annual_report_detail'),
    path('annual-reports/<uuid:report_id>/send/',
         views.annual_report_send,
         name='annual_report_send'),
    path('annual-reports/<uuid:report_id>/regenerate/',
         views.annual_report_regenerate,
         name='annual_report_regenerate'),
    path('annual-reports/<uuid:report_id>/download/',
         views.annual_report_download,
         name='annual_report_download'),

    # Phase 7 Part 3 — Website Intelligence & Upsell Engine
    path('intelligence/suggestions/',
         views.intelligence_suggestions,
         name='intelligence_suggestions'),
    path('intelligence/suggestions/<uuid:suggestion_id>/',
         views.intelligence_suggestion_detail,
         name='intelligence_suggestion_detail'),
    path('intelligence/suggestions/<uuid:suggestion_id>/status/',
         views.intelligence_suggestion_set_status,
         name='intelligence_suggestion_set_status'),
    path('intelligence/suggestions/<uuid:suggestion_id>/send/',
         views.intelligence_suggestion_send,
         name='intelligence_suggestion_send'),
    path('intelligence/suggestions/<uuid:suggestion_id>/invoice/',
         views.intelligence_suggestion_invoice,
         name='intelligence_suggestion_invoice'),
    path('intelligence/run/<uuid:client_id>/',
         views.intelligence_run_for_client,
         name='intelligence_run_for_client'),

    # Billing — admin-created onboarding invoices.
    path('billing/', views.billing_list, name='billing_list'),
    path('billing/new-invoice/', views.new_invoice, name='new_invoice'),
    path('billing/send-onboarding/',
         views.send_onboarding, name='send_onboarding'),
    path('billing/invoice/<uuid:invoice_id>/',
         views.invoice_detail, name='invoice_detail'),
    path('billing/invoice/<uuid:invoice_id>/resend-setup/',
         views.invoice_resend_setup, name='invoice_resend_setup'),
    path('billing/invoice/<uuid:invoice_id>/resend/',
         views.invoice_resend, name='invoice_resend'),
    path('billing/invoice/<uuid:invoice_id>/remind-intake/',
         views.invoice_send_intake_reminder,
         name='invoice_send_intake_reminder'),

    # Phase 7 Part 2 — case studies
    path('case-studies/', views.case_studies_list,
         name='case_studies_list'),
    path('case-studies/new/', views.case_study_new,
         name='case_study_new'),
    path('case-studies/ai-draft/', views.case_study_ai_draft,
         name='case_study_ai_draft'),
    path('case-studies/<uuid:cs_id>/edit/', views.case_study_edit,
         name='case_study_edit'),
    path('case-studies/<uuid:cs_id>/publish-toggle/',
         views.case_study_toggle_publish,
         name='case_study_toggle_publish'),

    # ── Phase C — Account + Website admin ──
    # New top-level entity. The legacy /clients/ list stays available
    # so existing bookmarks and tooling keep working.
    path('accounts/', views.accounts_list, name='accounts_list'),
    path('accounts/<uuid:account_id>/', views.account_detail,
         name='account_detail'),
    path('accounts/<uuid:account_id>/delete/', views.account_delete,
         name='account_delete'),
    path('accounts/<uuid:account_id>/send-password-reset/',
         views.account_send_password_reset,
         name='account_send_password_reset'),
    path('websites/', views.websites_list, name='websites_list'),
    path('websites/<uuid:website_id>/', views.website_detail,
         name='website_detail'),
    path('websites/<uuid:website_id>/move-account/',
         views.website_move_account, name='website_move_account'),
    path('websites/<uuid:website_id>/change-stage/',
         views.website_change_stage, name='website_change_stage'),
    path('websites/<uuid:website_id>/intake-complete/',
         views.website_intake_mark_complete,
         name='website_intake_mark_complete'),
    path('domains/<uuid:reg_id>/move-account/',
         views.domain_move_account, name='domain_move_account'),

    # DMARC aggregate-report ingest + dashboard
    path('dmarc/', views.dmarc_dashboard, name='dmarc_dashboard'),
    path('dmarc/upload/', views.dmarc_upload, name='dmarc_upload'),
]
