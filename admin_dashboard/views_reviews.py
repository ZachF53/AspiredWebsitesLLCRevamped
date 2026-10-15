"""
Operator surface for the Review System.

This is where the manual half of the product lives. The app collects what
the client can tell us; a human then creates their GoHighLevel
sub-account, submits the A2P 10DLC brand and campaign, and builds the
workflow. These pages exist to make that work visible and to hold the
credentials it produces.

The queue is sorted so what is owed comes first, because this is a work
list, not a report.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from admin_dashboard.decorators import admin_required

logger = logging.getLogger(__name__)

# Most-urgent first. A program awaiting carrier registration is work
# sitting on the operator's desk; a live one needs nothing.
_STATUS_ORDER = {
    'awaiting_a2p': 0,
    'suspended': 1,
    'awaiting_onboarding': 2,
    'live': 3,
}


@admin_required
def reviews_list(request):
    """/admin-dashboard/reviews/ — the work queue."""
    from datetime import timedelta

    from django.db.models import Count, Q

    from reviews.models import ReviewProgram

    since = timezone.now() - timedelta(days=30)
    programs = list(
        ReviewProgram.objects
        .select_related('account')
        .annotate(
            sent_30=Count('events', filter=Q(
                events__event_type='sent', events__occurred_at__gte=since)),
            reviews_30=Count('events', filter=Q(
                events__event_type='review_left',
                events__occurred_at__gte=since)),
            needs_human=Count('events', filter=Q(
                events__needs_human=True, events__event_type='reply')),
        )
    )
    programs.sort(key=lambda p: (
        _STATUS_ORDER.get(p.setup_status, 9), p.display_name.lower()))

    return render(request, 'admin_dashboard/reviews_list.html', {
        'programs': programs,
        'awaiting_count': sum(
            1 for p in programs if p.setup_status == 'awaiting_a2p'),
        'unresolved_events': _unresolved_event_count(),
    })


def _unresolved_event_count():
    """Events that arrived with a GHL location we don't recognise.

    Always a configuration mistake — a location id that was never pasted
    in — and invisible unless something surfaces it.
    """
    from reviews.models import ReviewEvent
    return ReviewEvent.objects.filter(program__isnull=True).count()


@admin_required
def review_program_detail(request, program_id):
    """/admin-dashboard/reviews/<id>/ — everything about one program."""
    from reviews.models import ReviewProgram
    from reviews.redaction import redact_payload

    program = get_object_or_404(
        ReviewProgram.objects.select_related('account'), pk=program_id)

    events = list(program.events.all()[:40])
    for event in events:
        # Never render a raw payload: it carries the client's customers'
        # phone numbers and emails.
        event.safe_payload = redact_payload(event.payload)

    return render(request, 'admin_dashboard/review_program_detail.html', {
        'program': program,
        'answers': _onboarding_answers(program),
        'attestations': program.attestations.all(),
        'events': events,
        'blocking_reasons': program.blocking_reasons(),
        'can_go_live': not program.blocking_reasons(),
    })


def _onboarding_answers(program):
    """The client's wizard answers, label-for-value, for a read-only panel.

    Read through the registry so the labels match what the client saw,
    and the EIN is excluded entirely — it has its own audited reveal.
    """
    user = getattr(program.account, 'user', None)
    if user is None:
        return []
    try:
        from onboarding.models import Onboarding
        from onboarding.registry import visible_sections

        ob = (Onboarding.objects
              .filter(user=user, product_type='review_system')
              .order_by('-started_at').first())
        if ob is None:
            return []
        values = {r.question_key: r for r in ob.responses.all()}
        out = []
        for section in visible_sections(ob):
            rows = []
            for q in section.get('questions', []):
                if q['key'] == 'ein':
                    continue
                response = values.get(q['key'])
                rows.append({
                    'label': q['label'],
                    'value': ('(skipped)' if response and response.skipped
                              else (response.value if response else '')),
                })
            out.append({'title': section.get('title', ''), 'rows': rows})
        return out
    except Exception:
        logger.exception('reviews admin: answer read failed')
        return []


@admin_required
@require_POST
def save_credentials(request, program_id):
    """Store the GHL location id, token and sending number.

    The token is write-only: a blank submit leaves the stored value
    alone, and the value is never rendered back — only a hint, when it
    was set, and who set it. Same convention as the vault.
    """
    from reviews.models import ReviewProgram
    from vault.crypto import make_hint, server_encrypt

    program = get_object_or_404(ReviewProgram, pk=program_id)

    program.ghl_location_id = (
        request.POST.get('ghl_location_id') or '').strip()[:64]
    program.sending_number = (
        request.POST.get('sending_number') or '').strip()[:20]

    fields = ['ghl_location_id', 'sending_number', 'updated_at']

    token = (request.POST.get('ghl_token') or '').strip()
    if token:
        try:
            program.ghl_token_encrypted = server_encrypt(token)
        except RuntimeError as exc:
            messages.error(request, str(exc))
            return redirect('admin_dashboard:review_program_detail',
                            program_id=program.pk)
        program.ghl_token_hint = make_hint(token)
        program.ghl_token_set_at = timezone.now()
        program.ghl_token_set_by = request.user
        fields += ['ghl_token_encrypted', 'ghl_token_hint',
                   'ghl_token_set_at', 'ghl_token_set_by']

    program.save(update_fields=fields)
    messages.success(request, 'Connection details saved.')
    return redirect('admin_dashboard:review_program_detail',
                    program_id=program.pk)


@admin_required
@require_POST
def save_a2p(request, program_id):
    """Record where the carrier registration has got to."""
    from reviews.models import ReviewProgram

    program = get_object_or_404(ReviewProgram, pk=program_id)
    valid = {choice for choice, _ in ReviewProgram.A2P_CHOICES}

    brand = (request.POST.get('a2p_brand_status') or '').strip()
    campaign = (request.POST.get('a2p_campaign_status') or '').strip()
    if brand in valid:
        program.a2p_brand_status = brand
    if campaign in valid:
        program.a2p_campaign_status = campaign
    program.a2p_rejection_reason = (
        request.POST.get('a2p_rejection_reason') or '').strip()

    if program.a2p_brand_status in ('submitted', 'pending') \
            and program.a2p_submitted_at is None:
        program.a2p_submitted_at = timezone.now()
    if (program.a2p_brand_status == 'approved'
            and program.a2p_campaign_status == 'approved'
            and program.a2p_approved_at is None):
        program.a2p_approved_at = timezone.now()

    program.save(update_fields=[
        'a2p_brand_status', 'a2p_campaign_status', 'a2p_rejection_reason',
        'a2p_submitted_at', 'a2p_approved_at', 'updated_at'])
    messages.success(request, 'Carrier registration status saved.')
    return redirect('admin_dashboard:review_program_detail',
                    program_id=program.pk)


@admin_required
@require_POST
def mark_live(request, program_id):
    """Flip a program to live, or refuse and say exactly why.

    Refuses without a consent attestation on file. That record is the
    only thing standing between us and an unanswerable TCPA claim, and
    it can only be created by the client completing the wizard
    themselves — an operator cannot sign it for them.
    """
    from reviews.models import ReviewProgram

    program = get_object_or_404(ReviewProgram, pk=program_id)
    reasons = program.blocking_reasons()
    if reasons:
        for reason in reasons:
            messages.error(request, reason)
        return redirect('admin_dashboard:review_program_detail',
                        program_id=program.pk)

    program.setup_status = 'live'
    program.a2p_rejection_reason = ''
    program.save(update_fields=[
        'setup_status', 'a2p_rejection_reason', 'updated_at'])
    messages.success(
        request,
        f'{program.display_name} is live. Remember the GHL workflow must '
        f'call /api/reviews/status/{program.public_key}/ before sending, '
        'or the client\'s Pause button does nothing.')
    return redirect('admin_dashboard:review_program_detail',
                    program_id=program.pk)


@admin_required
@require_POST
def suspend(request, program_id):
    from reviews.models import ReviewProgram
    program = get_object_or_404(ReviewProgram, pk=program_id)
    program.setup_status = 'suspended'
    program.save(update_fields=['setup_status', 'updated_at'])
    messages.success(request, f'{program.display_name} suspended.')
    return redirect('admin_dashboard:review_program_detail',
                    program_id=program.pk)


@admin_required
@require_POST
def mark_wording_synced(request, program_id):
    """Confirm the GHL workflow now matches the approved wording."""
    from reviews.models import ReviewProgram
    program = get_object_or_404(ReviewProgram, pk=program_id)
    program.wording_synced_to_ghl = True
    program.save(update_fields=['wording_synced_to_ghl', 'updated_at'])
    messages.success(request, 'Marked as matching the GHL workflow.')
    return redirect('admin_dashboard:review_program_detail',
                    program_id=program.pk)


@admin_required
@require_POST
def reveal_ein(request, program_id):
    """Show the EIN once, and record that it was shown.

    A tax ID is not something to render on every page load. Reading it
    is a deliberate act with an audit trail, so a later question about
    who saw it has an answer.
    """
    from reviews.models import ReviewProgram

    program = get_object_or_404(ReviewProgram, pk=program_id)
    ein = program.ein()
    if not ein:
        messages.info(request, 'No EIN on file for this program.')
        return redirect('admin_dashboard:review_program_detail',
                        program_id=program.pk)

    try:
        from core.system_alerts import record_alert
        record_alert(
            severity='info',
            source='reviews.ein_revealed',
            message=f'EIN revealed for {program.display_name}',
            detail=(f'Revealed by {request.user.email or request.user} at '
                    f'{timezone.now():%Y-%m-%d %H:%M} UTC.'),
        )
    except Exception:
        logger.exception('reviews admin: EIN reveal alert failed')

    messages.warning(request, f'EIN for {program.display_name}: {ein}')
    return redirect('admin_dashboard:review_program_detail',
                    program_id=program.pk)
