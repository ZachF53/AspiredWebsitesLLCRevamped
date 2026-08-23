from django.urls import path

from . import views
from . import views_outreach_admin as vo


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
    # Live enrichment monitor — counters + recent activity, HTMX
    # auto-refreshes every 10s.
    path('leads/enrichment/', views.enrichment_status,
         name='enrichment_status'),
    path('leads/enrichment/partial/', views.enrichment_status_partial,
         name='enrichment_status_partial'),
    # Standing scrape recipes — run daily at 02:00 via Celery beat.
    path('leads/scrape-jobs/', views.scrape_jobs_list, name='scrape_jobs'),
    path('leads/scrape-jobs/new/', views.scrape_job_form,
         name='scrape_job_new'),
    path('leads/scrape-jobs/<int:pk>/edit/', views.scrape_job_form,
         name='scrape_job_edit'),
    path('leads/scrape-jobs/<int:pk>/delete/', views.scrape_job_delete,
         name='scrape_job_delete'),
    path('leads/scrape-jobs/<int:pk>/toggle/',
         views.scrape_job_toggle_active, name='scrape_job_toggle_active'),
    path('leads/scrape-jobs/<int:pk>/run/', views.scrape_job_run_now,
         name='scrape_job_run_now'),
    path('leads/<int:pk>/', views.lead_detail, name='lead_detail'),
    path('leads/<int:pk>/edit/', views.lead_edit, name='lead_edit'),
    path('leads/<int:pk>/delete/', views.lead_delete, name='lead_delete'),
    path('leads/<int:pk>/reenrich/', views.lead_reenrich,
         name='lead_reenrich'),
    # Per-lead on-demand cold-email generation — always queues for
    # approval, regardless of trust level.
    path('leads/<int:pk>/generate-email/', views.lead_generate_email,
         name='lead_generate_email'),
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

    # Sent — every dispatched outreach email with engagement chips.
    # Closest thing to a Gmail Sent folder for SendGrid-relayed mail.
    path('outreach/sent/', views.outreach_sent, name='outreach_sent'),
    # Approval queue — every email the cold sender and reply auto-drafter
    # produce that the current trust level says needs a human in the loop.
    path('outreach/approvals/', views.outreach_approvals,
         name='outreach_approvals'),
    path('outreach/approvals/<int:pk>/approve/',
         views.outreach_approval_approve,
         name='outreach_approval_approve'),
    path('outreach/approvals/<int:pk>/reject/',
         views.outreach_approval_reject,
         name='outreach_approval_reject'),
    path('outreach/approvals/bulk-approve/',
         views.outreach_approval_bulk_approve,
         name='outreach_approval_bulk_approve'),

    # Redis client monitor — counts by process category, last 24h
    # trend, recent snapshots. Snapshot task at every 5 min.
    path('redis/', views.redis_monitor, name='redis_monitor'),

    # Brief generator — Claude Code .md template builder
    path('briefs/', views.briefs_home, name='briefs_home'),
    path('briefs/master.md', views.briefs_master_download,
         name='briefs_master_download'),
    path('briefs/blank/', views.briefs_blank_builder,
         name='briefs_blank_builder'),

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
    # ── Outreach management ────────────────────────────────────────────
    # Full CRUD outside /admin/. Django admin shows every field with
    # equal weight, which is exactly wrong for a page where one field
    # (does this campaign have an Instantly id?) decides whether the
    # thing works at all.
    path('outreach/', vo.outreach_index, name='outreach_index'),
    path('outreach/offers/', vo.offer_list, name='outreach_offer_list'),
    path('outreach/offers/new/', vo.offer_edit, name='outreach_offer_new'),
    path('outreach/offers/<int:offer_id>/edit/', vo.offer_edit,
         name='outreach_offer_edit'),
    path('outreach/offers/<int:offer_id>/delete/', vo.offer_delete,
         name='outreach_offer_delete'),
    path('outreach/offers/<int:offer_id>/toggle/', vo.offer_toggle,
         name='outreach_offer_toggle'),

    path('outreach/campaigns/', vo.campaign_list,
         name='outreach_campaign_list'),
    path('outreach/campaigns/new/', vo.campaign_edit,
         name='outreach_campaign_new'),
    path('outreach/campaigns/<int:campaign_id>/edit/', vo.campaign_edit,
         name='outreach_campaign_edit'),
    path('outreach/campaigns/<int:campaign_id>/delete/', vo.campaign_delete,
         name='outreach_campaign_delete'),

    path('outreach/review/', vo.review_queue, name='outreach_review_queue'),
    path('outreach/review/<int:lead_id>/decide/', vo.review_decide,
         name='outreach_review_decide'),
    path('outreach/review/bulk/', vo.review_bulk,
         name='outreach_review_bulk'),

    path('pricing/', views.pricing_list, name='pricing_list'),
    path('pricing/<uuid:tier_id>/edit/', views.pricing_edit, name='pricing_edit'),
    path('pricing/<uuid:tier_id>/toggle/', views.pricing_toggle, name='pricing_toggle'),
    path('pricing/<uuid:tier_id>/feature/add/', views.pricing_feature_add, name='pricing_feature_add'),
    path('pricing/<uuid:tier_id>/feature/<uuid:fid>/delete/', views.pricing_feature_delete, name='pricing_feature_delete'),

    # Onboarding question manager (DB-backed intake builder)
    path('onboarding-questions/', views.onboarding_questions,
         name='onboarding_questions'),
    path('onboarding-questions/section/new/',
         views.onboarding_section_form, name='onboarding_section_new'),
    path('onboarding-questions/section/<int:section_id>/edit/',
         views.onboarding_section_form, name='onboarding_section_edit'),
    path('onboarding-questions/section/<int:section_id>/delete/',
         views.onboarding_section_delete, name='onboarding_section_delete'),
    path('onboarding-questions/question/new/',
         views.onboarding_question_form, name='onboarding_question_new'),
    path('onboarding-questions/question/<int:question_id>/edit/',
         views.onboarding_question_form, name='onboarding_question_edit'),
    path('onboarding-questions/question/<int:question_id>/delete/',
         views.onboarding_question_delete,
         name='onboarding_question_delete'),
    path('onboarding-questions/mark-complete/',
         views.onboarding_mark_complete, name='onboarding_mark_complete'),

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
    path('clients/<uuid:client_id>/stage/',
         views.client_change_stage,
         name='client_change_stage'),
    path('websites/<uuid:website_id>/changelog/', views.website_changelog, name='website_changelog'),
    path('websites/<uuid:website_id>/changelog/add/', views.changelog_add_website, name='changelog_add_website'),
    path('websites/<uuid:website_id>/uptime/', views.website_uptime, name='website_uptime'),
    path('websites/<uuid:website_id>/keywords/', views.website_keywords, name='website_keywords'),
    path('websites/<uuid:website_id>/keywords/add/', views.keyword_add, name='keyword_add'),
    path('websites/<uuid:website_id>/keywords/check/', views.keyword_run_check, name='keyword_run_check'),
    path('websites/<uuid:website_id>/conversions/', views.website_conversions, name='website_conversions'),
    path('clients/<uuid:client_id>/toggle-session-recording/',
         views.client_toggle_session_recording,
         name='client_toggle_session_recording'),

    # Tier 2 — session recordings (rrweb)
    path('websites/<uuid:website_id>/recordings/',
         views.recordings_list, name='recordings_list'),
    path('websites/<uuid:website_id>/recordings/<uuid:rec_id>/replay/',
         views.recording_replay, name='recording_replay'),
    path('websites/<uuid:website_id>/recordings/<uuid:rec_id>/download/',
         views.recording_download, name='recording_download'),
    path('websites/<uuid:website_id>/recordings/<uuid:rec_id>/delete/',
         views.recording_delete, name='recording_delete'),
    path('websites/<uuid:website_id>/recordings/delete-all/',
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
    path('websites/<uuid:website_id>/freshness/', views.website_freshness, name='website_freshness'),
    path('websites/<uuid:website_id>/freshness/generate/', views.freshness_generate, name='freshness_generate'),
    path('websites/<uuid:website_id>/freshness/flag/', views.freshness_flag, name='freshness_flag'),

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
    path('websites/<uuid:website_id>/chatbot/', views.website_chatbot, name='website_chatbot'),
    path('websites/<uuid:website_id>/chatbot/regenerate-prompt/', views.chatbot_regenerate_prompt, name='chatbot_regenerate_prompt'),
    path('websites/<uuid:website_id>/chatbot/conversations/<uuid:conv_id>/', views.chatbot_conversation, name='chatbot_conversation'),
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
    path('accounts/<uuid:account_id>/comp/', views.account_set_comp_tier,
         name='account_set_comp_tier'),
    path('accounts/<uuid:account_id>/send-contract/',
         views.account_send_contract, name='account_send_contract'),
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
    path('websites/<uuid:website_id>/send-contract/',
         views.website_send_contract, name='website_send_contract'),
    path('websites/<uuid:website_id>/add-plan/',
         views.website_add_plan, name='website_add_plan'),
    path('websites/<uuid:website_id>/intake-complete/',
         views.website_intake_mark_complete,
         name='website_intake_mark_complete'),
    path('domains/<uuid:reg_id>/move-account/',
         views.domain_move_account, name='domain_move_account'),

    # DMARC aggregate-report ingest + dashboard
    path('dmarc/', views.dmarc_dashboard, name='dmarc_dashboard'),
    path('dmarc/upload/', views.dmarc_upload, name='dmarc_upload'),

    # Schedule-a-Call Google Calendar connection (Path A4)
    path('schedule/connect/',
         __import__('scheduler.google_oauth_views',
                    fromlist=['connect_page']).connect_page,
         name='schedule_connect'),
    path('schedule/start-oauth/',
         __import__('scheduler.google_oauth_views',
                    fromlist=['start_oauth']).start_oauth,
         name='schedule_start_oauth'),
    path('schedule/google-callback/',
         __import__('scheduler.google_oauth_views',
                    fromlist=['oauth_callback']).oauth_callback,
         name='schedule_google_callback'),
    path('schedule/disconnect/',
         __import__('scheduler.google_oauth_views',
                    fromlist=['disconnect']).disconnect,
         name='schedule_disconnect'),

    # Schedule — availability windows management UI
    path('schedule/availability/',
         __import__('scheduler.admin_views',
                    fromlist=['availability_list']).availability_list,
         name='schedule_availability'),
    path('schedule/availability/add/',
         __import__('scheduler.admin_views',
                    fromlist=['availability_add']).availability_add,
         name='schedule_availability_add'),
    path('schedule/availability/<int:window_id>/edit/',
         __import__('scheduler.admin_views',
                    fromlist=['availability_edit']).availability_edit,
         name='schedule_availability_edit'),
    path('schedule/availability/<int:window_id>/toggle/',
         __import__('scheduler.admin_views',
                    fromlist=['availability_toggle']).availability_toggle,
         name='schedule_availability_toggle'),
    path('schedule/availability/<int:window_id>/delete/',
         __import__('scheduler.admin_views',
                    fromlist=['availability_delete']).availability_delete,
         name='schedule_availability_delete'),
    path('schedule/availability/seed-defaults/',
         __import__('scheduler.admin_views',
                    fromlist=['availability_seed_defaults']).availability_seed_defaults,
         name='schedule_availability_seed'),

    # Schedule — booked calls
    path('schedule/calls/',
         __import__('scheduler.admin_views',
                    fromlist=['calls_list']).calls_list,
         name='schedule_calls'),
    path('schedule/calls/<uuid:call_id>/cancel/',
         __import__('scheduler.admin_views',
                    fromlist=['call_cancel']).call_cancel,
         name='schedule_call_cancel'),
    path('schedule/calls/<uuid:call_id>/complete/',
         __import__('scheduler.admin_views',
                    fromlist=['call_mark_completed']).call_mark_completed,
         name='schedule_call_complete'),

    # Phase 4 — AI Assistant (natural-language admin command box)
    path('ai-assistant/', views.ai_assistant_page, name='ai_assistant'),
    path('ai-assistant/parse/', views.ai_assistant_parse,
         name='ai_assistant_parse'),
    path('ai-assistant/execute/', views.ai_assistant_execute,
         name='ai_assistant_execute'),

    # Data Health — parity, payment evidence, sync health and cutover
    # progress in one read-only page, so none of it needs an SSH session.
    path('data-health/',
         __import__('admin_dashboard.data_health_views',
                    fromlist=['data_health']).data_health,
         name='data_health'),

    # AI Employees — the agent cockpit (COLD_OUTREACH_AGENT.md §8.2).
    # Handlers live in the ai_employee_views module and are re-exported
    # from the views module, per the convention tests_navigation enforces.
    # (Do not write the dotted module filename in this file: that test
    # regexes `views\.(\w+)` over the raw source, comments included, so it
    # would read the extension as a missing view name.)
    path('ai-employees/', views.ai_employees, name='ai_employees'),
    path('ai-employees/<slug:slug>/', views.ai_employee_detail,
         name='ai_employee_detail'),
    path('ai-employees/<slug:slug>/toggle/', views.ai_employee_toggle_active,
         name='ai_employee_toggle_active'),
    path('ai-employees/<slug:slug>/task/', views.ai_employee_add_task,
         name='ai_employee_add_task'),
    path('ai-employees/<slug:slug>/wake/', views.ai_employee_wake,
         name='ai_employee_wake'),
    path('ai-employees/action/<int:action_id>/decide/',
         views.ai_action_decide, name='ai_action_decide'),

    # System Alerts — X3 error visibility
    path('alerts/',
         __import__('core.system_alerts_views',
                    fromlist=['alerts_list']).alerts_list,
         name='system_alerts'),
    path('alerts/<int:alert_id>/resolve/',
         __import__('core.system_alerts_views',
                    fromlist=['alert_resolve']).alert_resolve,
         name='system_alerts_resolve'),
    path('alerts/resolve-all/',
         __import__('core.system_alerts_views',
                    fromlist=['alert_resolve_all']).alert_resolve_all,
         name='system_alerts_resolve_all'),
]
