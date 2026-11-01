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
    # Free-trial only: called by the browser after it completes a 3DS
    # challenge on the SetupIntent, to create the trialing subscription.
    # The charge-now path has no equivalent because there the browser
    # confirms a PaymentIntent that is already attached to a
    # subscription.
    path('checkout/<slug:tier_slug>/finalize/',
         checkout_views.checkout_finalize, name='checkout_finalize'),
    path('checkout/<slug:tier_slug>/success/',
         checkout_views.checkout_success, name='checkout_success'),
    # Post-signup disclosure for a free trial: what gets charged, when,
    # and how to cancel first. Keyed on the TermsAcceptance UUID.
    path('trial-started/<slug:tier_slug>/',
         checkout_views.trial_started, name='trial_started'),
    # Manage or cancel a free trial, on our own domain and in our own
    # branding. There is deliberately NO route into Stripe's hosted
    # Customer Portal anywhere on this site (CLAUDE.md: custom UI, no
    # Customer Portal). Reachable with ?ref=<TermsAcceptance uuid> for
    # the length of the trial, so a buyer who has not set a password can
    # still cancel in one click.
    path('trial/<slug:tier_slug>/manage/',
         checkout_views.trial_manage, name='trial_manage'),
    # Phase 7 — portal billing management (custom UI)
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
