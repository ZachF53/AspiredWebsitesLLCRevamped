"""
v2 dashboard urls, included under /admin-dashboard/v2/ from
admin_dashboard/urls.py. The v1/v2 toggle routes
(/admin-dashboard/use-v2/, /admin-dashboard/use-v1/) are registered
directly in admin_dashboard/urls.py, not here, since they sit outside
the /v2/ prefix — see admin_dashboard/navigation.py for why the toggle
is a session flag rather than a URL prefix.
"""

from django.urls import path

from . import views_accounts, views_billing, views_dashboard, views_domains
from . import views_websites

# Deliberately NO app_name here. This module is include()'d from
# admin_dashboard/urls.py, which already carries app_name='admin_dashboard'
# (namespaced as 'admin_dashboard' in the project root urls.py). Leaving
# app_name unset means these patterns join that same namespace directly,
# so `reverse('admin_dashboard:v2_home')` resolves — a nested
# 'admin_dashboard:admin_dashboard_v2:...' namespace would break every
# {% url %} tag written against the flat name used throughout this build.

urlpatterns = [
    path('', views_dashboard.home, name='v2_home'),
    path('money-partial/', views_dashboard.money_partial,
         name='v2_money_partial'),

    path('accounts/', views_accounts.accounts_list, name='v2_accounts_list'),
    path('accounts/new/', views_accounts.account_create,
         name='v2_account_create'),
    path('accounts/<uuid:account_id>/', views_accounts.account_detail,
         name='v2_account_detail'),
    path('accounts/<uuid:account_id>/send-setup-email/',
         views_accounts.account_send_setup_email,
         name='v2_account_send_setup_email'),
    path('accounts/<uuid:account_id>/reset-password/',
         views_accounts.account_reset_password,
         name='v2_account_reset_password'),

    path('websites/', views_websites.websites_list, name='v2_websites_list'),
    path('websites/new/', views_websites.website_create,
         name='v2_website_create'),
    path('websites/<uuid:website_id>/', views_websites.website_detail,
         name='v2_website_detail'),
    path('websites/<uuid:website_id>/stage/', views_websites.website_stage,
         name='v2_website_stage'),
    path('websites/<uuid:website_id>/send-intake-reminder/',
         views_websites.website_send_intake_reminder,
         name='v2_website_send_intake_reminder'),
    path('websites/<uuid:website_id>/run-scan/',
         views_websites.website_run_scan, name='v2_website_run_scan'),
    path('websites/<uuid:website_id>/toggle-auto-send-scan/',
         views_websites.website_toggle_auto_send_scan,
         name='v2_website_toggle_auto_send_scan'),

    path('domains/', views_domains.domains_list, name='v2_domains_list'),

    path('billing/', views_billing.billing_list, name='v2_billing_list'),
]
