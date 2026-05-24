"""
Public payment views — token-gated, no auth required.

The flow:
    Email (sent by us)
        → /pay/<token>/                 — payment page with Stripe Elements
        → Stripe processes the card
        → /pay/<token>/success/         — thank-you page + receipt info
    Stripe webhook → payment_intent.succeeded → onboarding kicks off
"""

from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render


def _get_invoice_or_404(token):
    """Lookup helper — fetches the OnboardingInvoice by its payment_token."""
    from clients.models import OnboardingInvoice
    return get_object_or_404(
        OnboardingInvoice.objects.select_related('client', 'client__user'),
        payment_token=token,
    )


def pay_invoice(request, token):
    """
    Public payment page — renders the invoice + Stripe Payment Element.

    The PaymentIntent was created at admin invoice-creation time. We just
    hand its `client_secret` to Stripe.js on this page; the card form
    submits straight to Stripe (we never touch card data).
    """
    invoice = _get_invoice_or_404(token)

    # Already paid → bounce to the success page (the success page
    # gracefully shows "already paid").
    if invoice.status == 'paid':
        return redirect('billing:pay_success', token=token)
    if invoice.status == 'canceled':
        return render(
            request,
            'billing/pay_invoice_canceled.html',
            {'invoice': invoice},
            status=410,
        )

    # JSON config the browser-side payment_page.js reads via
    # json_script. The client_secret is tied to the PaymentIntent so
    # re-using it is safe — Stripe won't accept it twice once paid.
    stripe_config = {
        'publishable_key': getattr(
            settings, 'STRIPE_PUBLISHABLE_KEY', ''),
        'client_secret': invoice.stripe_client_secret,
        'success_url': (
            f'{settings.SITE_BASE_URL}/pay/{token}/success/'),
    }

    return render(
        request,
        'billing/pay_invoice.html',
        {
            'invoice': invoice,
            'client': invoice.client,
            'stripe_config': stripe_config,
        },
    )


def pay_success(request, token):
    """
    Post-payment landing — shown after Stripe Elements confirms the
    card. The actual onboarding work (activate user, send setup link,
    generate receipt) happens server-side on the
    payment_intent.succeeded webhook; this page just confirms to the
    client that their payment landed and surfaces the account-setup
    link so they can flow straight into setup without waiting for the
    setup email.
    """
    invoice = _get_invoice_or_404(token)

    # Surface the setup URL if the webhook has already minted an
    # OnboardingToken for this client. Webhooks are usually <1s after
    # `stripe.confirmPayment`, but Stripe can occasionally delay them
    # — if the token isn't there yet, the template falls back to a
    # generic "we'll be in touch" message.
    setup_url = ''
    token_obj = getattr(invoice.client, 'onboarding_token', None)
    if token_obj and not token_obj.used:
        setup_url = token_obj.get_setup_url()

    return render(
        request,
        'billing/pay_success.html',
        {
            'invoice': invoice,
            'client': invoice.client,
            'setup_url': setup_url,
        },
    )
