"""
Client-portal surfaces for the AI Voice Receptionist.

Follows ``reviews/views_portal.py`` exactly: every view is
``@client_required``, context comes from ``clients.views._portal_context``
so the sidebar, badges and the staff view-as banner behave identically,
and messages are rendered centrally by ``clients/base.html``.

The nav link is always visible, so ``hub`` is a dispatcher — a management
page for a client who has the product, a setup page for one who has not
finished the wizard, a waiting page while we build their agent, and a
sales page for everyone else.
"""

import logging

from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from clients.decorators import client_required
from clients.views import _portal_context
from voice.entitlement import program_for, voice_entitlement

logger = logging.getLogger(__name__)


def _tier():
    """The live tier row. Never hardcode the price (CLAUDE.md).

    Also the only correct source for the CURRENT cohort price — a client
    buying today pays what this says, not what the first cohort paid.
    """
    from billing.pricing_models import ServiceTier
    from clients.contract_options import VOICE_RECEPTIONIST_SLUG
    return ServiceTier.objects.filter(
        slug=VOICE_RECEPTIONIST_SLUG, is_active=True).first()


def _cohort_context():
    try:
        from voice.cohorts import cohort_context
        return cohort_context()
    except Exception:
        logger.exception('voice.portal: cohort context failed')
        return {}


def _require_program(request):
    """(entitlement, program) for a client who should have one.

    Returns ``(ent, None)`` when they are not entitled — callers bounce
    to the hub, which explains the situation rather than 404ing.
    """
    ent = voice_entitlement(request.account,
                            website=getattr(request, 'website', None))
    if not ent.has_access:
        return ent, None
    program = ent.program or program_for(request.account, create=True)
    return ent, program


def _onboarding_url(request, ent):
    """Where "Finish setup" goes, creating the Onboarding row if needed.

    A comped client never went through checkout, so nothing has created
    their wizard row — the hub has to. Never during a staff view-as
    session: creating rows (and later signing a recording attestation)
    on a client's behalf is not something an operator may do.
    """
    from clients.contract_options import VOICE_RECEPTIONIST_SLUG
    from onboarding.models import Onboarding

    tier_slug = VOICE_RECEPTIONIST_SLUG
    if ent.plan is not None and ent.plan.tier_slug:
        tier_slug = ent.plan.tier_slug
    ob = ent.onboarding
    if ob is None:
        try:
            from clients.impersonation import is_read_only
            if is_read_only(request):
                return reverse('clients:dashboard')
            ob, _ = Onboarding.objects.get_or_create(
                user=request.user, product_type='voice_receptionist',
                tier_slug=tier_slug)
        except Exception:
            logger.exception('voice.hub: could not create Onboarding')
            return reverse('clients:dashboard')
    return reverse('onboarding:welcome',
                   args=[ob.product_type, ob.tier_slug])


@client_required
def hub(request):
    """/portal/voice/ — the single entry point for every state."""
    ent = voice_entitlement(request.account,
                            website=getattr(request, 'website', None))

    if ent.has_access:
        if ent.program is None or not ent.onboarding_complete:
            return render(request, 'voice/portal_setup.html', _portal_context(
                request, 'voice',
                ent=ent,
                onboarding_url=_onboarding_url(request, ent),
            ))
        if ent.setup_status == 'live':
            return redirect('voice:manage')
        if ent.setup_status == 'suspended':
            return render(
                request, 'voice/portal_suspended.html', _portal_context(
                    request, 'voice', ent=ent, tier=_tier(),
                    program=ent.program))
        # awaiting_number — we owe them an agent and a phone number.
        return render(request, 'voice/portal_waiting.html', _portal_context(
            request, 'voice', ent=ent, program=ent.program))

    # No access: sell it.
    return render(request, 'voice/portal_signup.html', _portal_context(
        request, 'voice',
        ent=ent,
        tier=_tier(),
        voice_cohort=_cohort_context(),
    ))


@client_required
def manage(request):
    """/portal/voice/manage/ — the live dashboard."""
    from voice.reporting import (
        chart_bars, monthly_series, recent_calls, window_stats,
    )
    from voice.usage import usage_summary

    ent, program = _require_program(request)
    if program is None:
        return redirect('voice:hub')
    if not ent.onboarding_complete or program.setup_status in (
            'awaiting_onboarding', 'awaiting_number', 'suspended'):
        return redirect('voice:hub')

    ctx = _portal_context(
        request, 'voice',
        ent=ent,
        program=program,
        windows=window_stats(program),
        series=chart_bars(monthly_series(program)),
        calls=recent_calls(program),
        usage=usage_summary(program),
        next_transition=_next_transition(program),
    )
    return render(request, 'voice/portal_manage.html', ctx)


def _next_transition(program):
    """"Answering until 8:00am" for the status line, or None for 24/7."""
    try:
        from voice.schedule import next_transition
        return next_transition(program, timezone.now())
    except Exception:
        logger.exception('voice.portal: next transition failed')
        return None


@client_required
def calls(request):
    """/portal/voice/calls/ — the full call log with transcripts."""
    ent, program = _require_program(request)
    if program is None:
        return redirect('voice:hub')
    ctx = _portal_context(
        request, 'voice',
        ent=ent,
        program=program,
        calls=program.calls.filter(trial_call=False)[:100],
    )
    return render(request, 'voice/portal_calls.html', ctx)


@client_required
def signup(request):
    """/portal/voice/signup/ — add the receptionist to an existing client.

    Deliberately NOT a redirect to ``/billing/checkout/<slug>/``: that is
    the anonymous self-checkout flow and it would create a second Stripe
    customer and a second Django user for someone who is already logged
    in. Mirrors ``clients.views.portal_maintenance_start`` instead —
    confirm against the card on file, then charge the existing customer.
    """
    ent = voice_entitlement(request.account)
    if ent.has_access:
        messages.info(request, 'You already have the AI receptionist.')
        return redirect('voice:hub')

    tier = _tier()
    if tier is None:
        messages.error(
            request,
            'The AI receptionist is not available to add right now. '
            'Email zacherylong@aspiredwebsites.com and we will sort it.')
        return redirect('voice:hub')

    if request.method != 'POST':
        return render(request, 'voice/portal_signup.html', _portal_context(
            request, 'voice', ent=ent, tier=tier, confirm=True,
            voice_cohort=_cohort_context(),
        ))

    try:
        from clients.impersonation import is_read_only
        if is_read_only(request):
            messages.error(
                request, 'Read-only session: cannot subscribe for a client.')
            return redirect('voice:hub')
    except Exception:
        pass

    try:
        from billing.stripe_helpers import create_addon_subscription
        create_addon_subscription(request.account, tier.slug)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('voice:hub')
    except Exception:
        logger.exception('voice.signup: subscription failed for %s',
                         request.account.pk)
        messages.error(
            request,
            'Something went wrong starting your subscription. Nothing was '
            'charged. Email zacherylong@aspiredwebsites.com and we will '
            'get it sorted.')
        return redirect('voice:hub')

    program = program_for(request.account, create=True)
    # Comps and purchases both consume a cohort slot, so the ladder has
    # to be checked here as well as in the comp view.
    try:
        from voice.cohorts import maybe_escalate
        maybe_escalate()
    except Exception:
        logger.exception('voice.signup: cohort escalation failed')

    messages.success(
        request,
        'You are in. Next step is about ten minutes of questions so we '
        'know how to answer your phone.')
    if program is not None and not program.greeting:
        return redirect(_onboarding_url(
            request, voice_entitlement(request.account)))
    return redirect('voice:hub')


@client_required
@require_POST
def pause(request):
    """Stop the agent answering, without cancelling.

    Real because the agent asks ``/api/voice/status/<public_key>/`` at the
    start of every call — see ``voice/views_public.py``. A pause button
    that only set a flag nothing read would be a lie.
    """
    ent, program = _require_program(request)
    if program is None:
        return redirect('voice:hub')
    program.paused = True
    program.paused_at = timezone.now()
    program.paused_by_client = True
    program.save(update_fields=[
        'paused', 'paused_at', 'paused_by_client', 'updated_at'])
    messages.success(
        request,
        'Paused. Your receptionist will stop answering within a minute. '
        'Calls will ring through to you as they did before.')
    return redirect('voice:hub')


@client_required
@require_POST
def resume(request):
    ent, program = _require_program(request)
    if program is None:
        return redirect('voice:hub')
    program.paused = False
    program.paused_at = None
    program.paused_by_client = False
    program.save(update_fields=[
        'paused', 'paused_at', 'paused_by_client', 'updated_at'])
    messages.success(request, 'Your receptionist is answering again.')
    return redirect('voice:hub')


# Fields the client may change themselves, and the ones they may not.
# Hours, coverage and answer mode are all client-editable because they
# are operational preferences. The GHL ids, the phone number and the
# cohort price are not: those are ours, or the carrier's, or a
# commercial term.
_CLIENT_EDITABLE_SETTINGS = (
    'coverage_mode', 'answer_mode', 'ring_seconds', 'transfer_number',
    'after_transfer_fail', 'max_call_minutes', 'recording_enabled',
)
_CLIENT_EDITABLE_KNOWLEDGE = (
    'greeting', 'services_offered', 'service_area', 'faq_text',
    'emergency_handling', 'transfer_conditions',
)


@client_required
@require_POST
def settings_save(request):
    """How it answers: coverage, answer mode, transfer, recording."""
    from voice.models import VoiceProgram

    ent, program = _require_program(request)
    if program is None:
        return redirect('voice:hub')

    changed = []
    coverage_choices = {c[0] for c in VoiceProgram.COVERAGE_CHOICES}
    answer_choices = {c[0] for c in VoiceProgram.ANSWER_CHOICES}
    fail_choices = {c[0] for c in VoiceProgram.TRANSFER_FAIL_CHOICES}

    posted_coverage = request.POST.get('coverage_mode', '')
    if posted_coverage in coverage_choices:
        program.coverage_mode = posted_coverage
        changed.append('coverage_mode')

    posted_answer = request.POST.get('answer_mode', '')
    if posted_answer in answer_choices:
        program.answer_mode = posted_answer
        changed.append('answer_mode')

    posted_fail = request.POST.get('after_transfer_fail', '')
    if posted_fail in fail_choices:
        program.after_transfer_fail = posted_fail
        changed.append('after_transfer_fail')

    for field, lo, hi in (('ring_seconds', 5, 60),
                          ('max_call_minutes', 2, 30)):
        raw = request.POST.get(field)
        if raw:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if lo <= value <= hi:
                setattr(program, field, value)
                changed.append(field)

    number = (request.POST.get('transfer_number') or '').strip()
    if number:
        from onboarding.validators import describe_answer_problems
        problems = describe_answer_problems('transfer_number', number)
        if problems:
            messages.error(request, problems[0])
            return redirect('voice:manage')
        program.transfer_number = number
        changed.append('transfer_number')

    if 'recording_enabled' in request.POST:
        program.recording_enabled = bool(
            request.POST.get('recording_enabled'))
        changed.append('recording_enabled')

    if not changed:
        messages.info(request, 'Nothing changed.')
        return redirect('voice:manage')

    # Anything the client changes here is config GHL does not have yet.
    program.config_synced_to_ghl = False
    program.config_updated_at = timezone.now()
    program.save(update_fields=changed + [
        'config_synced_to_ghl', 'config_updated_at', 'updated_at'])

    _warn_if_screening_lost(request, program)
    messages.success(
        request,
        'Saved. We will push this to your receptionist within one '
        'business day and email you when it is live.')
    return redirect('voice:manage')


def _warn_if_screening_lost(request, program):
    """Tell a client who just switched to ring-first what they gave up.

    Not a block — it is their phone and their choice. But spam screening
    silently stopping working is exactly the kind of thing that turns
    into "the filtering doesn't work" three weeks later.
    """
    if program.answer_mode == 'ring_you_first':
        messages.warning(
            request,
            'Heads up: with your phone ringing first, spam calls reach '
            'you before the receptionist is involved, so it can no '
            'longer screen them. Switch back to "it answers first" any '
            'time to turn screening back on.')


@client_required
@require_POST
def knowledge_save(request):
    """What it says and knows: greeting, services, area, FAQs."""
    ent, program = _require_program(request)
    if program is None:
        return redirect('voice:hub')

    changed = []
    for field in _CLIENT_EDITABLE_KNOWLEDGE:
        if field not in request.POST:
            continue
        value = (request.POST.get(field) or '').strip()
        setattr(program, field, value)
        changed.append(field)

    if not changed:
        messages.info(request, 'Nothing changed.')
        return redirect('voice:manage')

    # Re-apply the recording disclosure: a client editing their greeting
    # can easily delete it, and it is what keeps them right in
    # two-party-consent states. Idempotent, so an edit that kept the
    # line does not end up with two.
    if 'greeting' in changed:
        from voice.materialise import _greeting_with_disclosure
        program.greeting = _greeting_with_disclosure(
            program.greeting, program.recording_enabled)

    program.config_synced_to_ghl = False
    program.config_updated_at = timezone.now()
    program.save(update_fields=changed + [
        'config_synced_to_ghl', 'config_updated_at', 'updated_at'])

    messages.success(
        request,
        'Saved. We will update your receptionist within one business day.')
    return redirect('voice:manage')
