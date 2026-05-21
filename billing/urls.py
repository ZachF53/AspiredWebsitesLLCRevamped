"""Billing URL routes (mounted at /billing/)."""

from django.urls import path

from . import webhooks

app_name = 'billing'

urlpatterns = [
    path('webhook/', webhooks.stripe_webhook, name='stripe_webhook'),
]
