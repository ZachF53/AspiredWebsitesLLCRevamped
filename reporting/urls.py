"""Reporting URLs — the public tracking API (mounted at /api/)."""

from django.urls import path

from . import views

app_name = 'reporting'

urlpatterns = [
    path('track/', views.track_conversion_event, name='track'),
]
