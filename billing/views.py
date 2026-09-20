"""
Public payment views — token-gated, no auth required.

The flow:
    Email (sent by us)
        → /pay/<token>/                 — payment page with Stripe Elements
        → Stripe processes the card
        → /pay/<token>/success/         — thank-you page + receipt info
    Stripe webhook → payment_intent.succeeded → onboarding kicks off

Also: /plan-pay/<plan_id>/ — same idea, for a maintenance/social plan
billing.plan_billing.start_website_plan created with no card on file.
Keyed on the plan's own UUID pk rather than a separate token field.
"""

import json
import logging

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)


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


# ── Plan pay page — maintenance/social plan added with no card on file ──

def _get_plan_or_404(plan_id):
    """A plan's UUID pk is unique across both tables in practice, but the
    URL alone doesn't say which model — check both."""
    from clients.service_models import MaintenancePlan, SocialMediaPlan

    plan = (MaintenancePlan.objects.filter(id=plan_id)
            .select_related('account', 'account__user', 'website').first())
    if plan is not None:
        return plan
    plan = (SocialMediaPlan.objects.filter(id=plan_id)
            .select_related('account', 'account__user', 'website').first())
    if plan is not None:
        return plan
    from django.http import Http404
    raise Http404('No plan matches this id')


def pay_plan(request, plan_id):
    """Public payment page for a plan awaiting its first card. Renders
    the tier/price/discount + a Stripe card Element; the browser posts
    the resulting payment_method_id to pay_plan_confirm."""
    from billing.pricing_models import ServiceTier
    from clients.service_models import MaintenancePlan

    plan = _get_plan_or_404(plan_id)

    if plan.status == 'active':
        return redirect('pay_plan_success', plan_id=plan_id)

    category = 'maintenance' if isinstance(plan, MaintenancePlan) else 'social_media'
    tier = ServiceTier.objects.filter(slug=plan.tier_slug, category=category).first()
    price = tier.price if tier else None
    discounted = price
    if price is not None and plan.discount_percent:
        from decimal import ROUND_HALF_UP, Decimal
        discounted = (price * (Decimal(100 - plan.discount_percent) / Decimal(100))
                      ).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    owner = plan.account
    stripe_config = {
        'publishable_key': getattr(settings, 'STRIPE_PUBLISHABLE_KEY', ''),
        'confirm_url': request.build_absolute_uri(
            f'/plan-pay/{plan_id}/confirm/'),
        'success_url': request.build_absolute_uri(
            f'/plan-pay/{plan_id}/success/'),
    }
    return render(request, 'billing/pay_plan.html', {
        'plan': plan,
        'tier': tier,
        'tier_name': plan.get_tier_slug_display(),
        'price': price,
        'discounted': discounted,
        'client_name': getattr(owner, 'name', '') or '',
        'stripe_config': stripe_config,
    })


@csrf_exempt
@require_POST
def pay_plan_confirm(request, plan_id):
    """AJAX confirm endpoint — attaches the given payment_method_id and
    creates the real Stripe subscription via
    billing.plan_billing.complete_awaiting_plan_payment. JSON in/out,
    mirrors billing.checkout_views.checkout_confirm's response shape
    (requires_action / client_secret for SCA, or ok)."""
    from billing.plan_billing import complete_awaiting_plan_payment

    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'bad json'}, status=400)

    payment_method_id = (payload.get('payment_method_id') or '').strip()
    if not payment_method_id:
        return JsonResponse({'error': 'payment_method_id required'}, status=400)

    plan = _get_plan_or_404(plan_id)
    if plan.status == 'active':
        return JsonResponse({'ok': True})

    result = complete_awaiting_plan_payment(plan, payment_method_id)
    status = 400 if 'error' in result else 200
    return JsonResponse(result, status=status)


def pay_plan_success(request, plan_id):
    """Post-payment landing for a plan pay page."""
    plan = _get_plan_or_404(plan_id)
    owner = plan.account
    return render(request, 'billing/pay_plan_success.html', {
        'plan': plan,
        'tier_name': plan.get_tier_slug_display(),
        'client_name': getattr(owner, 'name', '') or '',
    })
