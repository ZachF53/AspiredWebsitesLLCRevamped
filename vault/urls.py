"""Vault URL routes (mounted at /admin-dashboard/vault/)."""

from django.urls import path

from . import views

app_name = 'vault'

urlpatterns = [
    path('', views.vault_home, name='home'),
    path('new/', views.new_vault, name='new_vault'),
    path('log/', views.vault_access_log, name='access_log'),

    # Vault-level TOTP enrolment — runs once, right after PIN setup.
    path('totp-setup/', views.totp_setup, name='totp_setup'),

    path('<uuid:client_id>/', views.client_vault, name='client_vault'),
    path('<uuid:client_id>/add/', views.add_credential, name='add_credential'),
    path('<uuid:client_id>/edit/<uuid:cred_id>/', views.edit_credential, name='edit_credential'),
    path('<uuid:client_id>/delete/<uuid:cred_id>/', views.delete_credential, name='delete_credential'),
    path('<uuid:client_id>/reveal/<uuid:cred_id>/', views.reveal_credential, name='reveal_credential'),
    path('<uuid:client_id>/visibility/<uuid:cred_id>/', views.toggle_visibility, name='toggle_visibility'),

    # SSH terminal — vault PIN + vault-level TOTP gate it (no per-server step).
    path('<uuid:cred_id>/terminal/', views.terminal, name='terminal'),
    path('<uuid:cred_id>/commands/', views.command_library, name='command_library'),
    path('<uuid:cred_id>/commands/<uuid:cmd_id>/edit/', views.command_edit, name='command_edit'),
    path('<uuid:cred_id>/commands/<uuid:cmd_id>/row/', views.command_row, name='command_row'),
]
