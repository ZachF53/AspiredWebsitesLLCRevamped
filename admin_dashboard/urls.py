from django.urls import path

from . import views


app_name = 'admin_dashboard'

urlpatterns = [
    path('', views.home, name='home'),

    # Leads
    path('leads/', views.leads_table, name='leads_table'),
    path('leads/kanban/', views.leads_kanban, name='leads_kanban'),
    path('leads/add/', views.lead_add, name='lead_add'),
    path('leads/import/', views.lead_import, name='lead_import'),
    path('leads/scrape/', views.scrape, name='scrape'),
    path('leads/<int:pk>/', views.lead_detail, name='lead_detail'),
    path('leads/<int:pk>/edit/', views.lead_edit, name='lead_edit'),
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

    # Outreach automation config
    path('settings/', views.settings_view, name='settings'),

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
]
