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
        OnboardingInvoice.objects.select_related(
            'client', 'client__user', 'account_new', 'account_new__user'),
        payment_token=token,
    )


def _invoice_owner(invoice):
    """Who paid: the legacy ClientProfile, else the Account.

    `invoice.client` is null on every invoice raised from a contract on
    the Website/Account pages, which is why the success page greeted
    people as "Thank you, ." — `client.firm_name` on None.
    """
    return invoice.client or invoice.account_new


def _owner_name(owner):
    """ClientProfile calls it firm_name; Account calls it name."""
    if owner is None:
        return ''
    return (getattr(owner, 'firm_name', '')
            or getattr(owner, 'name', '') or '')


def _setup_token(invoice):
    """The unused OnboardingToken for this buyer, or None.

    The token is a reverse OneToOne on BOTH the legacy profile
    (`onboarding_token`) and the Account (`onboarding_token_new`).
    Account-based buyers only ever have the second, so looking only at
    the first left setup_url empty and the client staring at "we'll be
    in touch" with no way to set up their account.
    """
    from django.core.exceptions import ObjectDoesNotExist

    for owner, attr in ((invoice.client, 'onboarding_token'),
                        (invoice.account_new, 'onboarding_token_new')):
        if owner is None:
            continue
        try:
            token_obj = getattr(owner, attr)
        except ObjectDoesNotExist:
            continue
        if token_obj is not None and not token_obj.used:
            return token_obj
    return None


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

    owner = _invoice_owner(invoice)
    return render(
        request,
        'billing/pay_invoice.html',
        {
            'invoice': invoice,
            'client': owner,
            'client_name': _owner_name(owner),
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
    token_obj = _setup_token(invoice)
    if token_obj is not None:
        setup_url = token_obj.get_setup_url()

    owner = _invoice_owner(invoice)

    # Nothing to set up means they already have an account — a final
    # balance, a second build. Send them to the portal rather than
    # leaving them on a page that says "we'll be in touch".
    portal_url = ''
    if not setup_url:
        user = getattr(owner, 'user', None)
        if (user is not None and user.is_active
                and user.has_usable_password()):
            portal_url = request.build_absolute_uri('/portal/')

    return render(
        request,
        'billing/pay_success.html',
        {
            'invoice': invoice,
            'client': owner,
            'client_name': _owner_name(owner),
            'setup_url': setup_url,
            'portal_url': portal_url,
        },
    )
