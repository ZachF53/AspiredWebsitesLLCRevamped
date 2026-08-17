"""
Website intelligence and upsell suggestion engine.

Split out of admin_dashboard/views.py; re-exported from
`admin_dashboard.views` so urls.py keeps working unchanged.
"""

import datetime
import logging

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)
from .decorators import admin_required

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 3 — Website Intelligence & Upsell Engine
# ────────────────────────────────────────────────────────────────────────────

# All status transitions in one place — the views below just look up
# the target state and verify the transition is allowed.
_INTEL_STATUS_TRANSITIONS = {
    'pending_review':      {'approved_to_send', 'dismissed'},
    'approved_to_send':    {'sent_to_client', 'dismissed', 'pending_review'},
    'sent_to_client':      {'client_approved', 'client_declined',
                            'dismissed'},
    'client_approved':     {'in_scope', 'out_of_scope_offered',
                            'implemented'},
    'in_scope':            {'implemented'},
    'out_of_scope_offered':{'implemented', 'client_declined'},
    'client_declined':     set(),
    'implemented':         set(),
    'dismissed':           {'pending_review'},
}


@admin_required
def intelligence_suggestions(request):
    """
    Filterable suggestions table — default filter is the triage queue
    (status='pending_review'). Filters compose, no pagination needed
    until volume grows; we'll add it later if the table exceeds a
    screen on a 27" monitor.
    """
    from clients.models import (
        ClientProfile, IntelligenceSuggestion,
    )

    qs = (IntelligenceSuggestion.objects
          .select_related('client', 'report')
          .order_by('-generated_at'))

    status_filter = (request.GET.get('status') or 'pending_review').strip()
    type_filter = (request.GET.get('type') or '').strip()
    client_filter = (request.GET.get('client') or '').strip()
    month_filter = (request.GET.get('month') or '').strip()  # YYYY-MM

    if status_filter and status_filter != 'all':
        qs = qs.filter(status=status_filter)
    if type_filter:
        qs = qs.filter(suggestion_type=type_filter)
    if client_filter:
        try:
            qs = qs.filter(client_id=client_filter)
        except (ValueError, TypeError):
            pass
    if month_filter:
        try:
            year, mon = month_filter.split('-')
            qs = qs.filter(
                generated_at__year=int(year),
                generated_at__month=int(mon),
            )
        except (ValueError, AttributeError):
            pass

    # Rollups (unfiltered) — drive the BI summary cards at the top.
    base = IntelligenceSuggestion.objects.all()
    from django.db.models import Sum
    summary = {
        'pending': base.filter(status='pending_review').count(),
        'sent': base.filter(status='sent_to_client').count(),
        'approved': base.filter(
            status__in=['client_approved', 'in_scope',
                        'out_of_scope_offered']).count(),
        'implemented': base.filter(status='implemented').count(),
        'revenue': (
            base.filter(
                status__in=['client_approved',
                            'out_of_scope_offered',
                            'implemented'],
                is_in_maintenance_scope=False,
            ).aggregate(s=Sum('one_time_fee'))['s'] or 0
        ),
    }

    clients = (ClientProfile.objects
               .filter(intelligence_suggestions__isnull=False)
               .distinct().order_by('firm_name'))

    return render(request,
                  'admin_dashboard/intelligence_suggestions.html',
                  _admin_context(
                      'intelligence',
                      suggestions=qs,
                      summary=summary,
                      filter_status=status_filter,
                      filter_type=type_filter,
                      filter_client=client_filter,
                      filter_month=month_filter,
                      clients=clients,
                      type_choices=IntelligenceSuggestion
                          .SUGGESTION_TYPE_CHOICES,
                      status_choices=IntelligenceSuggestion
                          .STATUS_CHOICES,
                  ))


@admin_required
def intelligence_suggestion_detail(request, suggestion_id):
    """Single-suggestion detail page with action buttons."""
    from clients.models import IntelligenceSuggestion
    s = get_object_or_404(IntelligenceSuggestion, id=suggestion_id)
    return render(request,
                  'admin_dashboard/intelligence_suggestion_detail.html',
                  _admin_context(
                      'intelligence',
                      s=s,
                      allowed_transitions=_INTEL_STATUS_TRANSITIONS.get(
                          s.status, set()),
                  ))


def _intel_transition(suggestion, new_status):
    """Validate a transition; return (ok, error_msg)."""
    allowed = _INTEL_STATUS_TRANSITIONS.get(suggestion.status, set())
    if new_status not in allowed:
        return False, (f'Cannot move from {suggestion.status} '
                       f'to {new_status}.')
    return True, ''


@admin_required
@require_POST
def intelligence_suggestion_set_status(request, suggestion_id):
    """
    Generic status transition used by every admin button that doesn't
    have richer side-effects: approve_to_send, dismissed, in_scope,
    implemented, client_approved (manual), client_declined (manual).
    Status-with-side-effects (send email, generate Stripe invoice)
    has its own endpoint below.
    """
    from clients.models import IntelligenceSuggestion

    s = get_object_or_404(IntelligenceSuggestion, id=suggestion_id)
    new_status = (request.POST.get('status') or '').strip()

    ok, err = _intel_transition(s, new_status)
    if not ok:
        return HttpResponseBadRequest(err)

    update_fields = ['status', 'updated_at']
    s.status = new_status

    if new_status == 'dismissed':
        s.dismissal_reason = (
            request.POST.get('reason') or '').strip()[:300]
        update_fields.append('dismissal_reason')
    elif new_status == 'implemented':
        s.implemented_at = timezone.now()
        update_fields.append('implemented_at')
    elif new_status == 'client_approved':
        # Manual override — usually used when the client replied via
        # email rather than clicking the magic link.
        if s.client_responded_at is None:
            s.client_responded_at = timezone.now()
            update_fields.append('client_responded_at')

    s.save(update_fields=update_fields)
    return redirect('admin_dashboard:intelligence_suggestion_detail',
                    suggestion_id=s.id)


@admin_required
@require_POST
def intelligence_suggestion_send(request, suggestion_id):
    """
    Send the suggestion email to the client via SendGrid. Embeds the
    two magic-link CTAs (Approve / Not Now) so the client never needs
    to log in to respond.
    """
    from clients.models import IntelligenceSuggestion

    s = get_object_or_404(IntelligenceSuggestion, id=suggestion_id)
    ok, err = _intel_transition(s, 'sent_to_client')
    if not ok:
        return HttpResponseBadRequest(err)

    client = s.client
    to_email = client.user.email if client.user else ''
    if not to_email:
        return HttpResponseBadRequest(
            'Client has no email on file — cannot send.')

    contact_name = (client.contact_name
                    or (client.user.get_full_name() if client.user else '')
                    or 'there')

    # Plan-context paragraph — mirrors the spec's three branches.
    on_maint = client.maintenance_active
    plan_label = (client.get_package_display()
                  if client.package else 'maintenance')
    if not on_maint:
        plan_para = (
            'As a one-time build client, this would be billed '
            'separately. Clients on our maintenance plans often have '
            'items like this handled automatically.'
        )
    elif s.is_in_maintenance_scope:
        plan_para = (
            f"This is included in your {plan_label} plan. I'll handle "
            f"this in your next maintenance cycle. Just reply to "
            f"approve."
        )
    else:
        plan_para = (
            f'This falls outside your current {plan_label} plan scope, '
            f'but I can implement it as a one-time add-on.'
        )

    approve_url = s.get_response_url('approve')
    decline_url = s.get_response_url('decline')

    investment_line = (
        f'Investment: ${s.one_time_fee:.0f} one-time'
        if (not s.is_in_maintenance_scope and s.one_time_fee)
        else 'Investment: included in your maintenance plan'
    )

    first_name = (contact_name or '').split(' ')[0] or 'there'
    text_body = (
        f"Hi {first_name},\n\n"
        f"While reviewing {client.firm_name}'s website performance "
        f"this month, I identified an improvement that could benefit "
        f"your business.\n\n"
        f"{s.title.upper()}\n\n"
        f"{s.description}\n\n"
        f"Expected impact: {s.expected_impact}\n\n"
        f"{investment_line}\n\n"
        f"{plan_para}\n\n"
        f"Approve: {approve_url}\n"
        f"Not Now: {decline_url}\n\n"
        f"Reply to this email if you'd like to discuss.\n\n"
        f"— Zachery Long\nAspired Websites LLC\n"
    )

    from clients.emails import send_branded
    try:
        send_branded(
            subject=(f'Website Improvement Opportunity — '
                     f'{client.firm_name}'),
            template='intelligence_suggestion',
            context={
                'name': first_name,
                'suggestion': s,
                'investment_line': investment_line,
                'plan_para': plan_para,
                'approve_url': approve_url,
                'decline_url': decline_url,
                'preheader': (
                    f'{s.title[:100]}'
                    f'{"…" if len(s.title) > 100 else ""}'),
            },
            recipient_list=[to_email],
            text_body=text_body,
            secure=True,        # approve/decline magic links
            fail_silently=False,
        )
    except Exception as exc:  # noqa: BLE001
        return HttpResponse(f'Email send failed: {str(exc)[:200]}',
                            status=500)

    s.status = 'sent_to_client'
    s.sent_to_client_at = timezone.now()
    s.save(update_fields=[
        'status', 'sent_to_client_at', 'updated_at'])

    return redirect('admin_dashboard:intelligence_suggestion_detail',
                    suggestion_id=s.id)


@admin_required
@require_POST
def intelligence_suggestion_invoice(request, suggestion_id):
    """
    Create a one-off Stripe invoice for the suggestion's one_time_fee,
    then move the suggestion to `out_of_scope_offered`. Only valid
    after the client has approved an out-of-scope suggestion.
    """
    from clients.models import IntelligenceSuggestion

    s = get_object_or_404(IntelligenceSuggestion, id=suggestion_id)
    if s.status != 'client_approved':
        return HttpResponseBadRequest(
            'Invoice only after status=client_approved.')
    if s.is_in_maintenance_scope:
        return HttpResponseBadRequest(
            'Suggestion is in maintenance scope — no invoice needed.')
    if not s.one_time_fee or float(s.one_time_fee) <= 0:
        return HttpResponseBadRequest(
            'one_time_fee must be > 0 to invoice.')
    if not getattr(settings, 'STRIPE_SECRET_KEY', ''):
        return HttpResponseBadRequest(
            'STRIPE_SECRET_KEY is not configured.')

    client = s.client
    if not client.stripe_customer_id:
        return HttpResponseBadRequest(
            'Client has no Stripe customer ID. Create one via the '
            'billing tools first.')

    try:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY

        # InvoiceItem first, then the Invoice that wraps it. Setting
        # `pending_invoice_items_behavior='include'` would let us
        # bundle multiple pending items, but for one-off upsells the
        # simple flow is cleaner.
        stripe.InvoiceItem.create(
            customer=client.stripe_customer_id,
            amount=int(float(s.one_time_fee) * 100),
            currency='usd',
            description=s.title[:250],
        )
        invoice = stripe.Invoice.create(
            customer=client.stripe_customer_id,
            collection_method='send_invoice',
            days_until_due=14,
            description=(
                f'Website improvement: {s.title[:200]}'),
            auto_advance=True,
        )
        # `finalize_invoice` makes the hosted URL available.
        invoice = stripe.Invoice.finalize_invoice(invoice.id)
        # Email the invoice to the customer.
        try:
            stripe.Invoice.send_invoice(invoice.id)
        except Exception:
            # send_invoice can race finalize_invoice; ignore — the
            # auto_advance flag will retry.
            logger.exception(
                'stripe.Invoice.send_invoice failed for %s', invoice.id)
    except Exception as exc:  # noqa: BLE001
        return HttpResponse(
            f'Stripe error: {str(exc)[:300]}', status=500)

    s.stripe_invoice_id = invoice.id
    s.stripe_invoice_url = invoice.hosted_invoice_url or ''
    s.status = 'out_of_scope_offered'
    s.save(update_fields=[
        'stripe_invoice_id', 'stripe_invoice_url',
        'status', 'updated_at'])

    return redirect('admin_dashboard:intelligence_suggestion_detail',
                    suggestion_id=s.id)


@admin_required
@require_POST
def intelligence_run_for_client(request, client_id):
    """
    "Run Analysis Now" button on the client detail page. Fires the
    Celery task asynchronously so the operator gets immediate feedback
    instead of staring at a 30-second Claude call.
    """
    from clients.models import ClientProfile
    from clients.tasks import run_intelligence_for_client

    client = get_object_or_404(ClientProfile, id=client_id)
    run_intelligence_for_client.apply_async(args=[str(client.id)])

    if request.headers.get('HX-Request') == 'true':
        return HttpResponse(
            '<div class="banner banner--info">'
            'Analysis queued. Suggestions will appear here when the '
            'worker finishes (usually under a minute).'
            '</div>')
    return redirect('admin_dashboard:client_detail',
                    client_id=client.id)


