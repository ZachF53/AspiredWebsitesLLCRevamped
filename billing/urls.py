"""
Billing URL routes (mounted at /billing/).

The public payment pages (`/pay/<token>/` and `/pay/<token>/success/`)
are wired directly in the project root urls.py — they live at the root
so the URLs are short and friendly to email recipients. See
`billing.views.pay_invoice` / `pay_success`.
"""

from django.urls import path

from . import webhooks, checkout_views, portal_views

app_name = 'billing'

urlpatterns = [
    path('webhook/', webhooks.stripe_webhook, name='stripe_webhook'),
    # Custom Stripe Elements checkout — Phase 5 onboarding refactor
    path('checkout/<slug:tier_slug>/',
         checkout_views.checkout_page, name='checkout_page'),
    path('checkout/<slug:tier_slug>/email-check/',
         checkout_views.checkout_email_check, name='checkout_email_check'),
    path('checkout/<slug:tier_slug>/confirm/',
         checkout_views.checkout_confirm, name='checkout_confirm'),
    path('checkout/<slug:tier_slug>/success/',
         checkout_views.checkout_success, name='checkout_success'),
    # Phase 7 — portal billing management (custom UI, no Customer Portal)
    path('portal/', portal_views.billing_home, name='portal_home'),
    path('portal/subs/<str:sub_id>/cancel/',
         portal_views.subscription_cancel, name='subscription_cancel'),
    path('portal/subs/<str:sub_id>/change/',
         portal_views.subscription_change, name='subscription_change'),
    path('portal/cards/add/',
         portal_views.add_card, name='add_card'),
    path('portal/cards/<str:payment_method_id>/default/',
         portal_views.card_set_default, name='card_set_default'),
    path('portal/cards/<str:payment_method_id>/remove/',
         portal_views.card_remove, name='card_remove'),
    path('portal/invoices/<str:invoice_id>/pdf/',
         portal_views.invoice_pdf, name='invoice_pdf'),
]
