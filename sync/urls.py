"""Inbound sync API routes (mounted at /api/sync/)."""

from django.urls import path

from . import views

app_name = 'sync'

urlpatterns = [
    path('inbound/', views.sync_inbound, name='inbound'),
    path('file/<uuid:document_id>/', views.sync_file, name='file'),
]
