"""
Billing URL routes (mounted at /billing/).

The public payment pages (`/pay/<token>/` and `/pay/<token>/success/`)
are wired directly in the project root urls.py — they live at the root
so the URLs are short and friendly to email recipients. See
`billing.views.pay_invoice` / `pay_success`.
"""

from django.urls import path

from . import webhooks

app_name = 'billing'

urlpatterns = [
    path('webhook/', webhooks.stripe_webhook, name='stripe_webhook'),
]
