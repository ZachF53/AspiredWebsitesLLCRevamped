"""
Client-portal surfaces for the Review System.

A separate module rather than more lines in ``clients/views.py`` (already
3,500+), but it follows that file's conventions exactly: every view is
``@client_required``, context comes from ``_portal_context`` so the
sidebar, badges and the staff view-as banner behave identically, and
messages are rendered centrally by ``clients/base.html``.

The nav link is always visible, so ``hub`` is a dispatcher: it is a
management page for a client who has the product, a setup page for one
who owes us their A2P details, a waiting page while the carrier
registers their number, and a sales page for everyone else.
"""

import logging

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from clients.decorators import client_required
from clients.views import _portal_context
from reviews.entitlement import program_for, review_entitlement

logger = logging.getLogger(__name__)


def _tier():
    """The live $97 tier row. Never hardcode the price (CLAUDE.md)."""
    from billing.pricing_models import ServiceTier
    from clients.contract_options import REVIEW_SYSTEM_SLUG
    return ServiceTier.objects.filter(
        slug=REVIEW_SYSTEM_SLUG, is_active=True).first()


def _full_plan_tier():
    from billing.pricing_models import ServiceTier
    from clients.contract_options import PLAN_PAID_IN_FULL_SLUG
    return ServiceTier.objects.filter(
        slug=PLAN_PAID_IN_FULL_SLUG, is_active=True).first()


def _require_program(request):
    """(entitlement, program) for a client who should have one.

    Returns ``(None, None)`` after queueing a redirect-worthy message
    when they do not — callers bounce to the hub, which explains the
    situation properly rather than 404ing.
    """
    ent = review_entitlement(request.account,
                             website=getattr(request, 'website', None))
    if not ent.has_access:
        return ent, None
    program = ent.program or program_for(request.account, create=True)
    return ent, program


@client_required
def hub(request):
    """/portal/reviews/ — the single entry point for every state."""
    ent = review_entitlement(request.account,
                             website=getattr(request, 'website', None))

    if ent.has_access:
        # Entitled but we have nothing to register with yet.
        if ent.program is None or not ent.onboarding_complete:
            return render(request, 'reviews/portal_setup.html', _portal_context(
                request, 'reviews',
                ent=ent,
                onboarding_url=_onboarding_url(request, ent),
            ))
        if ent.setup_status == 'live':
            return redirect('reviews:manage')
        if ent.setup_status == 'suspended':
            return render(
                request, 'reviews/portal_suspended.html', _portal_context(
                    request, 'reviews', ent=ent, tier=_tier(),
                    program=ent.program))
        # awaiting_a2p — the friendly waiting state.
        return render(request, 'reviews/portal_waiting.html', _portal_context(
            request, 'reviews', ent=ent, program=ent.program))

    # No access: sell it.
    return render(request, 'reviews/portal_signup.html', _portal_context(
        request, 'reviews',
        ent=ent,
        tier=_tier(),
        full_plan_tier=_full_plan_tier() if ent.can_upgrade_full_plan else None,
    ))


def _onboarding_url(request, ent):
    """Where "Finish setup" goes, creating the Onboarding row if needed.

    A Full Plan client never went through a Review System checkout, so
    nothing has created their wizard row — the hub has to.
    """
    from clients.contract_options import REVIEW_SYSTEM_SLUG
    from onboarding.models import Onboarding

    tier_slug = REVIEW_SYSTEM_SLUG
    if ent.plan is not None and ent.plan.tier_slug:
        tier_slug = ent.plan.tier_slug
    ob = ent.onboarding
    if ob is None:
        try:
            from clients.impersonation import is_read_only
            if is_read_only(request):
                # Never create rows for a client during a view-as session.
                return reverse('clients:dashboard')
            ob, _ = Onboarding.objects.get_or_create(
                user=request.user, product_type='review_system',
                tier_slug=tier_slug)
        except Exception:
            logger.exception('reviews.hub: could not create Onboarding')
            return reverse('clients:dashboard')
    return reverse('onboarding:welcome',
                   args=[ob.product_type, ob.tier_slug])


@client_required
def manage(request):
    """/portal/reviews/manage/ — the live dashboard."""
    from reviews.reporting import chart_bars, monthly_series, window_stats

    ent, program = _require_program(request)
    if program is None:
        return redirect('reviews:hub')
    if not ent.onboarding_complete or program.setup_status in (
            'awaiting_onboarding', 'awaiting_a2p', 'suspended'):
        return redirect('reviews:hub')

    events = program.events.all()[:25]
    ctx = _portal_context(
        request, 'reviews',
        ent=ent,
        program=program,
        windows=window_stats(program),
        series=chart_bars(monthly_series(program)),
        events=events,
        replies_pending=program.events.filter(
            event_type='reply', needs_human=True).count(),
    )
    return render(request, 'reviews/portal_manage.html', ctx)


@client_required
def replies(request):
    """/portal/reviews/replies/ — what customers wrote back."""
    ent, program = _require_program(request)
    if program is None:
        return redirect('reviews:hub')
    ctx = _portal_context(
        request, 'reviews',
        ent=ent,
        program=program,
        replies=program.events.filter(event_type='reply')[:100],
    )
    return render(request, 'reviews/portal_replies.html', ctx)


@client_required
@require_POST
def pause(request):
    """Stop sending without cancelling the subscription.

    Touches ``ReviewProgram.sending_paused`` and nothing else — never
    ``MaintenancePlan.status``, never Stripe. Pausing is not cancelling,
    and conflating them would stop someone's billing because they wanted
    a quiet week.

    Only honoured because the client's GHL workflow checks
    ``/api/reviews/status/<public_key>/`` before sending.
    """
    ent, program = _require_program(request)
    if program is None:
        return redirect('reviews:hub')
    program.sending_paused = True
    program.paused_at = timezone.now()
    program.paused_by_client = True
    program.save(update_fields=[
        'sending_paused', 'paused_at', 'paused_by_client', 'updated_at'])
    messages.success(
        request,
        'Review requests are paused. Nothing will go out until you '
        'resume — your subscription is unchanged.')
    return redirect('reviews:hub')


@client_required
@require_POST
def resume(request):
    ent, program = _require_program(request)
    if program is None:
        return redirect('reviews:hub')
    program.sending_paused = False
    program.paused_at = None
    program.paused_by_client = False
    program.save(update_fields=[
        'sending_paused', 'paused_at', 'paused_by_client', 'updated_at'])
    messages.success(
        request,
        'Review requests are back on. Jobs completed from now on will '
        'trigger a request as usual.')
    return redirect('reviews:hub')


@client_required
@require_POST
def wording_save(request):
    """Save edited message wording, guardrails first.

    Problems are surfaced as messages and nothing is written — a partial
    save would leave the SMS compliant and the email not. On success the
    program is marked out-of-sync so the operator knows the GHL workflow
    needs updating.
    """
    from reviews.templating import (
        describe_template_problems, normalise_template,
    )

    ent, program = _require_program(request)
    if program is None:
        return redirect('reviews:hub')

    sms = (request.POST.get('sms_body_template') or '').strip()
    subject = (request.POST.get('email_subject_template') or '').strip()
    email = (request.POST.get('email_body_template') or '').strip()

    problems = []
    problems += describe_template_problems(sms, channel='sms')
    problems += describe_template_problems(
        email, channel='email', subject=subject)
    if problems:
        for problem in problems[:4]:
            messages.error(request, problem)
        return redirect('reviews:manage')

    program.sms_body_template = normalise_template(sms, channel='sms')
    program.email_subject_template = subject[:200]
    program.email_body_template = normalise_template(email, channel='email')
    program.wording_synced_to_ghl = False
    program.wording_updated_at = timezone.now()
    program.save(update_fields=[
        'sms_body_template', 'email_subject_template', 'email_body_template',
        'wording_synced_to_ghl', 'wording_updated_at', 'updated_at'])

    try:
        from core.system_alerts import record_alert
        record_alert(
            severity='info',
            source='reviews.wording_changed',
            message=f'Review wording changed — {program.display_name}',
            detail=('The client edited their review-request wording. '
                    'Update their GHL workflow to match: '
                    f'/admin-dashboard/reviews/{program.pk}/'),
        )
    except Exception:
        logger.exception('reviews.wording_save: alert failed')

    messages.success(
        request,
        'Saved. We will update the messages that actually go out within '
        'one business day and email you when it is live.')
    return redirect('reviews:manage')


@client_required
@require_POST
def settings_save(request):
    """Reminder toggle. Channel and timing live in the GHL workflow."""
    ent, program = _require_program(request)
    if program is None:
        return redirect('reviews:hub')
    program.reminder_enabled = bool(request.POST.get('reminder_enabled'))
    program.save(update_fields=['reminder_enabled', 'updated_at'])
    messages.success(
        request,
        'Reminder setting saved.' if program.reminder_enabled else
        'Reminders are off. Customers will get one request and nothing more.')
    return redirect('reviews:manage')


@client_required
def signup(request):
    """/portal/reviews/signup/ — add the $97 tier to an existing client.

    Deliberately NOT a redirect to /billing/checkout/<slug>/: that is the
    anonymous self-checkout flow and it would create a second Stripe
    customer and a second Django user for someone who is already logged
    in. Mirrors ``clients.views.portal_maintenance_start`` instead —
    confirm against the card on file, then charge the existing customer.
    """
    from billing.stripe_helpers import (
        StripeNotConfigured, create_addon_subscription,
    )

    ent = review_entitlement(request.account)
    if ent.has_access:
        messages.info(
            request, 'You already have the Review System — no need to buy it '
                     'again.')
        return redirect('reviews:hub')

    tier = _tier()
    if tier is None:
        messages.error(request, 'The Review System is not available to buy '
                                'online right now. Give us a call.')
        return redirect('reviews:hub')

    default_card = _default_card(request.account)

    if request.method == 'POST':
        if default_card is None:
            messages.error(
                request,
                'Add a payment method first, then come back to this page.')
            return redirect('clients:portal_subscriptions')
        try:
            create_addon_subscription(request.account, tier.slug)
        except StripeNotConfigured:
            messages.error(request, 'Payments are not configured. Please '
                                    'contact support.')
            return redirect('reviews:hub')
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('reviews:hub')
        except Exception:
            logger.exception('reviews.signup: subscription failed')
            messages.error(
                request,
                'We could not start the subscription. Nothing has been '
                'charged — please try again or contact support.')
            return redirect('reviews:hub')

        program_for(request.account, create=True)
        messages.success(
            request,
            'You are subscribed. Next: a few setup questions so we can '
            'register your texting number.')
        return redirect('reviews:hub')

    ctx = _portal_context(
        request, 'reviews',
        ent=ent, tier=tier, default_card=default_card,
        confirm=True,
    )
    return render(request, 'reviews/portal_signup.html', ctx)


def _default_card(account):
    """The card Stripe will charge, or None. Never raises."""
    if not getattr(account, 'stripe_customer_id', ''):
        return None
    try:
        from billing.stripe_helpers import (
            get_customer_default_payment_method, list_customer_payment_methods,
        )
        pm_id = get_customer_default_payment_method(account.stripe_customer_id)
        if not pm_id:
            return None
        for method in list_customer_payment_methods(
                account.stripe_customer_id):
            if getattr(method, 'id', '') == pm_id:
                return {
                    'brand': getattr(method.card, 'brand', '').upper(),
                    'last4': getattr(method.card, 'last4', ''),
                    'exp_month': getattr(method.card, 'exp_month', ''),
                    'exp_year': getattr(method.card, 'exp_year', ''),
                }
    except Exception:
        logger.exception('reviews.signup: card lookup failed')
    return None
