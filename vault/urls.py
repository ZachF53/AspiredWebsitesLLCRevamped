"""Vault URL routes (mounted at /admin-dashboard/vault/)."""

from django.urls import path

from . import views

app_name = 'vault'

urlpatterns = [
    path('', views.vault_home, name='home'),
    path('log/', views.vault_access_log, name='access_log'),
    path('<uuid:client_id>/', views.client_vault, name='client_vault'),
    path('<uuid:client_id>/add/', views.add_credential, name='add_credential'),
    path('<uuid:client_id>/edit/<uuid:cred_id>/', views.edit_credential, name='edit_credential'),
    path('<uuid:client_id>/delete/<uuid:cred_id>/', views.delete_credential, name='delete_credential'),
    path('<uuid:client_id>/reveal/<uuid:cred_id>/', views.reveal_credential, name='reveal_credential'),
    path('<uuid:client_id>/visibility/<uuid:cred_id>/', views.toggle_visibility, name='toggle_visibility'),
]
