"""Reporting URLs — the public tracking API (mounted at /api/)."""

from django.urls import path

from . import views

app_name = 'reporting'

urlpatterns = [
    path('track/', views.track_conversion_event, name='track'),
    path('track/batch/', views.track_batch, name='track_batch'),
    path('track/recording/', views.track_recording, name='track_recording'),
    path('chat/', views.chatbot_api, name='chat'),
    path('chat/config/<uuid:client_id>/', views.chatbot_config, name='chat_config'),
]
