"""
Operator surface for the AI Voice Receptionist.

Same division of labour as the Review System: the app collects what the
client can tell us, a human then builds their GoHighLevel Voice AI agent,
attaches a phone number, and pastes the resulting ids back here. These
pages make that work visible and hold the credentials it produces.

Two things this queue has that the review queue does not, both because
this product bills by the minute:

* **Minutes and accrued overage per program**, so an operator can see a
  client heading for a $60 overage before the invoice does.
* **An unbilled-overage count**, which is the signal that the
  ``invoice.created`` webhook missed something and the nightly sweep has
  not caught up.

The queue is sorted most-urgent-first because it is a work list, not a
report.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from admin_dashboard.decorators import admin_required

logger = logging.getLogger(__name__)

# Most-urgent first. A program waiting on us for an agent is work on the
# operator's desk; a live one needs nothing.
_STATUS_ORDER = {
    'awaiting_number': 0,
    'suspended': 1,
    'awaiting_onboarding': 2,
    'live': 3,
}


@admin_required
def voice_list(request):
    """/admin-dashboard/voice/ — the work queue."""
    from datetime import timedelta

    from django.db.models import Count, Q, Sum

    from voice.models import VoiceProgram

    since = timezone.now() - timedelta(days=30)
    programs = list(
        VoiceProgram.objects
        .select_related('account', 'cohort')
        .annotate(
            calls_30=Count('calls', filter=Q(
                calls__trial_call=False, calls__started_at__gte=since)),
            seconds_30=Sum('calls__duration_seconds', filter=Q(
                calls__trial_call=False, calls__started_at__gte=since)),
            transferred_30=Count('calls', filter=Q(
                calls__trial_call=False, calls__transferred=True,
                calls__started_at__gte=since)),
        )
    )
    for program in programs:
        program.minutes_30 = round((program.seconds_30 or 0) / 60)
        program.usage = _current_usage(program)

    programs.sort(key=lambda p: (
        _STATUS_ORDER.get(p.setup_status, 9), p.display_name.lower()))

    return render(request, 'admin_dashboard/voice_list.html', {
        'programs': programs,
        'awaiting_count': sum(
            1 for p in programs if p.setup_status == 'awaiting_number'),
        'unsynced_count': sum(
            1 for p in programs if not p.config_synced_to_ghl),
        'over_count': sum(
            1 for p in programs if p.usage and p.usage.is_over),
        'unattributed_calls': _unattributed_call_count(),
        'unbilled_overage': _unbilled_overage_count(),
        'cohort': _cohort_summary(),
    })


def _current_usage(program):
    try:
        from voice.usage import period_for
        return period_for(program, create=False)
    except Exception:
        return None


def _unattributed_call_count():
    """Calls that arrived with a GHL location we do not recognise.

    Always a configuration mistake — a location id that was never pasted
    in — and here it also means minutes nobody is being billed for.
    """
    from voice.models import VoiceCallRecord
    return VoiceCallRecord.objects.filter(program__isnull=True).count()


def _unbilled_overage_count():
    """Closed periods with overage and no invoice item.

    The signal that invoice.created missed one. sweep_unbilled_overage
    should clear these within a day; a number that stays up means the
    sweep is failing.
    """
    from datetime import timedelta
    from voice.models import VoiceUsagePeriod
    return VoiceUsagePeriod.objects.filter(
        period_end__lt=timezone.now() - timedelta(days=1),
        stripe_invoice_item_id='',
        # Waived periods are a decision, not a backlog. Counting them
        # here would show a permanent non-zero "unbilled" figure that an
        # operator learns to ignore, which is how a real one gets missed.
        billing_waived=False,
        overage_minutes__gt=0,
    ).count()


def _cohort_summary():
    """Where the price ladder currently stands, for the queue header."""
    try:
        from voice.cohorts import STEP, current_cohort
        cohort = current_cohort(create=False)
        if cohort is None:
            return None
        return {
            'sequence': cohort.sequence,
            'price': cohort.price,
            'next_price': cohort.price + STEP,
            'filled': cohort.filled,
            'capacity': cohort.capacity,
            'slots_remaining': cohort.slots_remaining,
        }
    except Exception:
        logger.exception('voice admin: cohort summary failed')
        return None


@admin_required
def voice_program_detail(request, program_id):
    """/admin-dashboard/voice/<id>/ — everything about one program."""
    from voice.models import VoiceProgram
    from voice.reporting import lifetime_totals
    from voice.schedule import ghl_working_hours
    from voice.usage import usage_summary

    program = get_object_or_404(
        VoiceProgram.objects.select_related('account', 'cohort'),
        pk=program_id)

    return render(request, 'admin_dashboard/voice_program_detail.html', {
        'program': program,
        'answers': _onboarding_answers(program),
        'attestations': program.attestations.all(),
        'calls': program.calls.all()[:40],
        'hours': program.hours.all(),
        # What to paste into GHL's Phone & Availability tab. Computed
        # rather than shown raw, because 'after hours only' is the
        # INVERSE of the client's business hours and working that out by
        # hand at 11pm is how mistakes happen.
        'working_hours': ghl_working_hours(program),
        'usage': usage_summary(program),
        'usage_history': program.usage_periods.all()[:12],
        'totals': lifetime_totals(program),
        'blocking_reasons': program.blocking_reasons(),
        'can_go_live': not program.blocking_reasons(),
    })


def _onboarding_answers(program):
    """The client's wizard answers, label-for-value, read-only.

    Read through the registry so the labels match what the client
    actually saw, rather than restating them here and drifting.
    """
    from onboarding.models import Onboarding
    from onboarding.registry import visible_sections

    user = getattr(program.account, 'user', None)
    if user is None:
        return []
    ob = (Onboarding.objects
          .filter(user=user, product_type='voice_receptionist')
          .order_by('-started_at')
          .first())
    if ob is None:
        return []

    values = {r.question_key: r for r in ob.responses.all()}
    out = []
    try:
        for section in visible_sections(ob):
            rows = []
            for q in section.get('questions', []):
                response = values.get(q['key'])
                rows.append({
                    'label': q['label'],
                    'value': ('(skipped)' if response and response.skipped
                              else (response.value if response
                                    else q.get('default', ''))),
                })
            out.append({'title': section.get('title', ''), 'rows': rows})
    except Exception:
        logger.exception('voice admin: answer rendering failed')
    return out


@admin_required
@require_POST
def save_credentials(request, program_id):
    """Store the GHL location id, agent id and token.

    The token is write-only: a blank submit keeps the existing value and
    the value is never rendered back. Same pattern and the same
    ``vault.crypto`` helpers as the review credentials form — there is
    one implementation of "a secret this server reads back without a PIN
    session" and this is not a second one.
    """
    from vault.crypto import make_hint, server_encrypt
    from voice.models import VoiceProgram

    program = get_object_or_404(VoiceProgram, pk=program_id)
    fields = []

    for field in ('ghl_location_id', 'ghl_agent_id', 'answering_number'):
        if field in request.POST:
            setattr(program, field, (request.POST.get(field) or '').strip())
            fields.append(field)

    token = (request.POST.get('ghl_token') or '').strip()
    if token:
        try:
            program.ghl_token_encrypted = server_encrypt(token)
        except RuntimeError as exc:
            messages.error(request, str(exc))
            return redirect('admin_dashboard:voice_program_detail',
                            program_id=program.id)
        program.ghl_token_hint = make_hint(token)
        program.ghl_token_set_at = timezone.now()
        program.ghl_token_set_by = request.user
        fields += ['ghl_token_encrypted', 'ghl_token_hint',
                   'ghl_token_set_at', 'ghl_token_set_by']

    if fields:
        program.save(update_fields=fields + ['updated_at'])
        messages.success(request, 'Saved.')
    return redirect('admin_dashboard:voice_program_detail',
                    program_id=program.id)


@admin_required
@require_POST
def mark_config_synced(request, program_id):
    """Operator confirms they have pushed the config into GHL."""
    from voice.models import VoiceProgram

    program = get_object_or_404(VoiceProgram, pk=program_id)
    program.config_synced_to_ghl = True
    program.save(update_fields=['config_synced_to_ghl', 'updated_at'])
    messages.success(request, 'Marked as synced to GHL.')
    return redirect('admin_dashboard:voice_program_detail',
                    program_id=program.id)


@admin_required
@require_POST
def mark_live(request, program_id):
    """Turn the receptionist on.

    Refuses while anything is outstanding. The checks live on the model
    (``blocking_reasons``) so this view and the detail page can never
    disagree about whether a program is ready — and one of those checks
    is the recording attestation, which is why this cannot be a simple
    status flip.
    """
    from voice.models import VoiceProgram

    program = get_object_or_404(VoiceProgram, pk=program_id)
    reasons = program.blocking_reasons()
    if reasons:
        messages.error(
            request, 'Not ready yet: ' + ' '.join(reasons))
        return redirect('admin_dashboard:voice_program_detail',
                        program_id=program.id)

    program.setup_status = 'live'
    program.save(update_fields=['setup_status', 'updated_at'])
    messages.success(
        request,
        f'{program.display_name} is live. Tell them to forward their line '
        f'to {program.answering_number}.')
    return redirect('admin_dashboard:voice_program_detail',
                    program_id=program.id)


@admin_required
@require_POST
def suspend(request, program_id):
    """Switch a receptionist off from our side.

    Distinct from the client's own Pause: this one the client cannot
    undo, and the portal shows a page telling them to get in touch.
    """
    from voice.models import VoiceProgram

    program = get_object_or_404(VoiceProgram, pk=program_id)
    program.setup_status = 'suspended'
    program.save(update_fields=['setup_status', 'updated_at'])
    messages.success(request, f'Suspended {program.display_name}.')
    return redirect('admin_dashboard:voice_program_detail',
                    program_id=program.id)


@admin_required
@require_POST
def recompute_usage(request, program_id):
    """Force a reconciliation for one program, now.

    The nightly sweep does this for everyone. This exists for the
    specific moment an operator is on the phone to a client disputing an
    overage line and needs the number to be provably current.
    """
    from voice.models import VoiceProgram
    from voice.tasks import reconcile_program

    program = get_object_or_404(VoiceProgram, pk=program_id)
    try:
        inserted, touched = reconcile_program(program)
    except Exception:
        logger.exception('voice admin: manual reconcile failed for %s',
                         program.pk)
        messages.error(
            request, 'Could not reach GHL. Minutes are unchanged.')
        return redirect('admin_dashboard:voice_program_detail',
                        program_id=program.id)

    if inserted:
        messages.success(
            request,
            f'Found {inserted} call(s) the webhook had missed and '
            f'recounted {touched} period(s).')
    else:
        messages.success(
            request,
            f'GHL agrees with us — nothing was missing. Recounted '
            f'{touched} period(s).')
    return redirect('admin_dashboard:voice_program_detail',
                    program_id=program.id)
