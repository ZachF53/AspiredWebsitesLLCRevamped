"""Maintenance handoff routes (mounted at /maintenance/)."""

from django.urls import path

from . import views

app_name = 'maintenance'

urlpatterns = [
    path('start/', views.maintenance_start, name='start'),
]
