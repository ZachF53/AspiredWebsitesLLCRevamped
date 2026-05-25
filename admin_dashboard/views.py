"""
Admin dashboard views. Every view is gated by Django's `staff_member_required`
(redirects to /admin/login/ for unauthenticated users, 403s logged-in
non-staff users). Lead data comes from outreach.Lead.
"""

import datetime
import json
import logging
import re
import uuid

from django.conf import settings
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db.models import Avg, Count, Q
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from outreach.models import (
    EmailReply,
    EmailSent,
    Lead,
    LeadNote,
    OutreachSettings,
    SuppressionList,
)
from outreach.pipeline import import_leads
from outreach.scoring import score_lead
from outreach.scraper import (
    scrape_georgia_bar_sync,
    scrape_google_maps_sync,
    scrape_texas_bar_sync,
)

logger = logging.getLogger(__name__)

from .decorators import admin_required
from .forms import (
    DeploymentLogForm,
    LeadAddForm,
    LeadNoteForm,
    ScrapeForm,
    ServiceTierForm,
)


# ────────────────────────────────────────────────────────────────────────────
# Shared context
# ────────────────────────────────────────────────────────────────────────────

def _admin_context(active=None, **extra):
    """
    Base context every admin view should merge in. Provides:
      - active: which top-nav item to highlight
      - needs_you_count: badge number for the Needs You nav item
      - critical_health_count: badge number for the Intelligence nav
        item (Phase 7 — cheap today-only count, single query)
    """
    needs_you_count = EmailReply.objects.filter(
        needs_human=True, handled=False
    ).count()
    # Intake reviews (admin task generated when a client submits intake)
    # count toward the same Needs You badge.
    try:
        from clients.models import ClientProfile as _ClientProfile
        needs_you_count += _ClientProfile.objects.filter(
            needs_admin_review_at__isnull=False,
            admin_reviewed_at__isnull=True,
        ).count()
    except Exception:
        pass
    try:
        critical_health_count = _critical_health_count()
    except Exception:
        # ClientHealthScore migration may not have run yet on a fresh
        # checkout — never break the chrome over a missing table.
        critical_health_count = 0
    try:
        active_proposals_count = _active_proposals_count()
    except Exception:
        # Proposal table may not exist on a fresh checkout — never
        # break the chrome over a missing table.
        active_proposals_count = 0
    try:
        intel_pending_count = _intel_pending_count()
    except Exception:
        intel_pending_count = 0
    try:
        gap_high_count = _high_priority_gaps_count()
    except Exception:
        gap_high_count = 0
    ctx = {
        'active': active,
        'needs_you_count': needs_you_count,
        'critical_health_count': critical_health_count,
        'active_proposals_count': active_proposals_count,
        'intel_pending_count': intel_pending_count,
        'gap_high_count': gap_high_count,
    }
    ctx.update(extra)
    return ctx


# ────────────────────────────────────────────────────────────────────────────
# Dashboard home
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def home(request):
    today = timezone.localdate()

    # Quick stats
    total_leads = Lead.objects.count()
    hot_leads = Lead.objects.filter(score__gte=7).count()
    needs_you_count = EmailReply.objects.filter(
        needs_human=True, handled=False
    ).count()
    try:
        from clients.models import ClientProfile as _ClientProfile
        needs_you_count += _ClientProfile.objects.filter(
            needs_admin_review_at__isnull=False,
            admin_reviewed_at__isnull=True,
        ).count()
    except Exception:
        pass
    emails_sent_today = EmailSent.objects.filter(sent_at__date=today).count()

    stats = [
        {'label': 'Total Leads',        'value': total_leads,        'href_name': 'admin_dashboard:leads_table'},
        {'label': 'Hot Leads',          'value': hot_leads,          'href_name': 'admin_dashboard:leads_table', 'href_qs': '?temperature=hot'},
        {'label': 'Needs You',          'value': needs_you_count,    'href_name': 'admin_dashboard:needs_you', 'urgent': needs_you_count > 0},
        {'label': 'Emails Sent Today',  'value': emails_sent_today,  'href_name': 'admin_dashboard:leads_table'},
    ]

    # Pipeline counts — count per status
    counts_by_status = dict(
        Lead.objects.values('status').annotate(n=Count('id')).values_list('status', 'n')
    )
    pipeline = [
        {'status': status, 'label': label, 'count': counts_by_status.get(status, 0)}
        for status, label in Lead.STATUS_CHOICES
        if status not in ('archived',)  # surface only active pipeline
    ]

    # Recent activity
    recent_leads = Lead.objects.order_by('-created_at')[:10]
    recent_emails = EmailSent.objects.select_related('lead').order_by('-sent_at')[:5]
    unhandled_replies = (
        EmailReply.objects.select_related('lead')
        .filter(needs_human=True, handled=False)
        .order_by('-received_at')[:5]
    )

    # Phase 7 Part 1 — Today's Focus widget. `get_daily_focus` is
    # defined further down in this same file; Python resolves the
    # name at call time so the forward reference is fine.
    return render(request, 'admin_dashboard/home.html', _admin_context(
        active='home',
        stats=stats,
        pipeline=pipeline,
        recent_leads=recent_leads,
        recent_emails=recent_emails,
        unhandled_replies=unhandled_replies,
        daily_focus=get_daily_focus(),
    ))


# ────────────────────────────────────────────────────────────────────────────
# Leads — table view
# ────────────────────────────────────────────────────────────────────────────

VALID_SORT = {
    'score':             '-score',
    'score_asc':         'score',
    'newest':            '-created_at',
    'oldest':            'created_at',
    'last_contacted':    '-last_contacted_at',
    'firm':              'firm_name',
    'firm_desc':         '-firm_name',
}
DEFAULT_SORT = 'score'
PAGE_SIZE = 50


@admin_required
def leads_table(request):
    qs = Lead.objects.all()

    # Free-text search
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(firm_name__icontains=q)
            | Q(attorney_name__icontains=q)
            | Q(city__icontains=q)
            | Q(email__icontains=q)
            | Q(phone__icontains=q)
        )

    # Filters
    status_filter = request.GET.get('status') or ''
    temperature_filter = request.GET.get('temperature') or ''
    state_filter = (request.GET.get('state') or '').strip()
    practice_filter = (request.GET.get('practice_area') or '').strip()
    source_filter = request.GET.get('source') or ''

    created_filter = request.GET.get('created') or ''

    if status_filter:
        qs = qs.filter(status=status_filter)
    if temperature_filter:
        qs = qs.filter(temperature=temperature_filter)
    if state_filter:
        qs = qs.filter(state__iexact=state_filter)
    if practice_filter:
        qs = qs.filter(practice_area__iexact=practice_filter)
    if source_filter:
        qs = qs.filter(source=source_filter)
    if created_filter == 'today':
        qs = qs.filter(created_at__date=timezone.localdate())

    # Sort
    sort_key = request.GET.get('sort') or DEFAULT_SORT
    sort_field = VALID_SORT.get(sort_key, VALID_SORT[DEFAULT_SORT])
    qs = qs.order_by(sort_field, '-created_at')

    # Pagination
    paginator = Paginator(qs, PAGE_SIZE)
    page_number = request.GET.get('page') or 1
    page = paginator.get_page(page_number)

    # Filter dropdown options
    practice_areas = (
        Lead.objects.exclude(practice_area='')
        .values_list('practice_area', flat=True)
        .distinct()
        .order_by('practice_area')
    )
    states = (
        Lead.objects.exclude(state='')
        .values_list('state', flat=True)
        .distinct()
        .order_by('state')
    )

    # Build a "current filters as querystring" string for pagination links
    keep = ['q', 'status', 'temperature', 'state', 'practice_area', 'source', 'sort', 'created']
    qs_parts = [f'{k}={request.GET.get(k)}' for k in keep if request.GET.get(k)]
    filter_qs = ('&' + '&'.join(qs_parts)) if qs_parts else ''

    return render(request, 'admin_dashboard/leads_table.html', _admin_context(
        active='leads',
        page=page,
        total=paginator.count,
        q=q,
        status_filter=status_filter,
        temperature_filter=temperature_filter,
        state_filter=state_filter,
        practice_filter=practice_filter,
        source_filter=source_filter,
        created_filter=created_filter,
        sort_key=sort_key,
        status_choices=Lead.STATUS_CHOICES,
        temperature_choices=Lead.TEMPERATURE_CHOICES,
        source_choices=Lead.SOURCE_CHOICES,
        states=states,
        practice_areas=practice_areas,
        filter_qs=filter_qs,
    ))


# ────────────────────────────────────────────────────────────────────────────
# Lead detail (basic — HTMX interactions land in next iteration)
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def lead_detail(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    return render(request, 'admin_dashboard/lead_detail.html', _admin_context(
        active='leads',
        lead=lead,
        notes=lead.lead_notes.all(),
        emails=lead.emails_sent.all(),
        replies=lead.replies.all(),
        note_form=LeadNoteForm(),
        status_choices=Lead.STATUS_CHOICES,
    ))


@admin_required
@require_POST
def lead_update_status(request, pk):
    """HTMX endpoint — update Lead.status, return refreshed status editor
    (+ OOB-swap of the header badge)."""
    lead = get_object_or_404(Lead, pk=pk)
    new_status = request.POST.get('status', '')
    valid = {value for value, _ in Lead.STATUS_CHOICES}
    if new_status not in valid:
        return HttpResponseBadRequest('Invalid status.')
    lead.status = new_status
    lead.save(update_fields=['status', 'updated_at'])
    return render(request, 'admin_dashboard/_status_editor.html', {
        'lead': lead,
        'status_choices': Lead.STATUS_CHOICES,
    })


@admin_required
@require_POST
def lead_add_note(request, pk):
    """HTMX endpoint — create a LeadNote, return the new note item HTML
    + OOB-swap of the textarea to clear it."""
    lead = get_object_or_404(Lead, pk=pk)
    form = LeadNoteForm(request.POST)
    if not form.is_valid():
        # Empty/invalid note — just return the empty form (no change to list)
        return render(request, 'admin_dashboard/_note_create.html', {
            'lead': lead,
            'new_note': None,
            'note_form': form,
        })
    note = form.save(commit=False)
    note.lead = lead
    note.save()
    return render(request, 'admin_dashboard/_note_create.html', {
        'lead': lead,
        'new_note': note,
        'note_form': LeadNoteForm(),
    })


# ────────────────────────────────────────────────────────────────────────────
# Stub views — return placeholder pages so all nav links resolve.
# Full implementations land in follow-up iterations.
# ────────────────────────────────────────────────────────────────────────────

# Kanban surfaces only active-pipeline statuses (skips 'unsubscribed' and 'archived',
# which clutter the visual board but still appear in the table view + filters).
KANBAN_STATUSES = (
    'new', 'contacted', 'replied', 'call_booked',
    'proposal_sent', 'won', 'lost',
)


def _kanban_columns():
    """Return the kanban board's column dicts in display order."""
    columns = []
    for status, label in Lead.STATUS_CHOICES:
        if status not in KANBAN_STATUSES:
            continue
        leads = list(
            Lead.objects.filter(status=status).order_by('-score', '-created_at')
        )
        columns.append({
            'status': status,
            'label': label,
            'leads': leads,
            'count': len(leads),
        })
    return columns


@admin_required
def leads_kanban(request):
    return render(request, 'admin_dashboard/leads_kanban.html', _admin_context(
        active='kanban',
        columns=_kanban_columns(),
        status_choices=Lead.STATUS_CHOICES,
    ))


@admin_required
@require_POST
def lead_kanban_move(request, pk):
    """HTMX endpoint — set a lead's status from the kanban view.
    Returns the refreshed full board (#kanban-board) so the moved card
    appears in its new column without a full page reload."""
    lead = get_object_or_404(Lead, pk=pk)
    new_status = request.POST.get('status', '')
    valid = {value for value, _ in Lead.STATUS_CHOICES}
    if new_status not in valid:
        return HttpResponseBadRequest('Invalid status.')
    if lead.status != new_status:
        lead.status = new_status
        lead.save(update_fields=['status', 'updated_at'])
    return render(request, 'admin_dashboard/_kanban_board.html', {
        'columns': _kanban_columns(),
        'status_choices': Lead.STATUS_CHOICES,
    })


@admin_required
def lead_add(request):
    if request.method == 'POST':
        form = LeadAddForm(request.POST)
        if form.is_valid():
            lead = form.save(commit=False)
            lead.source = 'manual'
            # Auto-score from the same signals scrapers feed
            score, temperature = score_lead({
                'website': lead.website,
                'website_performance_score': lead.website_performance_score,
                'has_google_business': lead.has_google_business,
                'google_review_count': lead.google_review_count,
            })
            lead.score = score
            lead.temperature = temperature
            lead.save()
            return redirect('admin_dashboard:lead_detail', pk=lead.pk)
    else:
        form = LeadAddForm()
    return render(request, 'admin_dashboard/lead_add.html', _admin_context(
        active='leads',
        form=form,
    ))


@admin_required
def lead_edit(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    return _stub(request, active='leads', title=f'Edit Lead — {lead.firm_name}',
                 blurb='Inline editing on the lead detail page will cover most of this. Building next.')


@admin_required
def lead_import(request):
    return _stub(request, active='leads', title='CSV Import',
                 blurb='Upload a CSV of leads. Building next.')


@admin_required
def scrape(request):
    """
    Run a scrape synchronously and show the import summary.

    NOTE: this blocks the request for 1-3 minutes. Fine on the dev server
    (multi-threaded). For production, move the scrape body into a Celery
    task — CLAUDE.md already lists "Lead scraper — Daily 2am" as a Celery
    job, and the admin-triggered run should share that task.
    """
    form = ScrapeForm(request.POST or None)
    results = None
    error = None

    if request.method == 'POST' and form.is_valid():
        source = form.cleaned_data['source']
        practice_area = form.cleaned_data['practice_area']
        city = form.cleaned_data['city']
        state = form.cleaned_data['state']
        max_results = int(form.cleaned_data['max_results'])

        api_calls = None
        try:
            if source == 'google_maps':
                state_full = 'Texas' if state == 'TX' else 'Georgia'
                niche = f'{practice_area} lawyer'
                raw, api_calls = scrape_google_maps_sync(
                    niche, city, state_full, max_results
                )
                import_source = 'google_maps'
            elif source == 'texas_bar':
                raw = scrape_texas_bar_sync(
                    city=city, practice_area=practice_area, max_results=max_results
                )
                import_source = 'state_bar'
            else:  # georgia_bar
                raw = scrape_georgia_bar_sync(
                    city=city, practice_area=practice_area, max_results=max_results
                )
                import_source = 'state_bar'

            results = import_leads(raw, source=import_source)
            if api_calls is not None:
                results['api_calls'] = api_calls
        except Exception as exc:
            error = f'Scrape failed: {exc}'

    return render(request, 'admin_dashboard/scrape.html', _admin_context(
        active='scrape',
        form=form,
        results=results,
        error=error,
    ))


def _needs_you_replies():
    """Unhandled, human-flagged replies — newest first."""
    return (
        EmailReply.objects
        .filter(needs_human=True, handled=False)
        .select_related('lead', 'email_sent')
        .order_by('-received_at')
    )


def _pending_intake_reviews():
    """
    Clients who submitted intake and are awaiting human review. Set by
    `_on_intake_submitted` (clients/views.py); cleared by the Mark
    Reviewed button on this page.
    """
    from clients.models import ClientProfile
    return (
        ClientProfile.objects
        .filter(
            needs_admin_review_at__isnull=False,
            admin_reviewed_at__isnull=True,
        )
        .select_related('user', 'intake')
        .order_by('-needs_admin_review_at')
    )


def _render_needs_you_list(request):
    """Render the queue list partial (used as the HTMX response after an
    action). Includes an OOB swap that keeps the nav badge in sync."""
    replies = list(_needs_you_replies())
    intake_reviews = list(_pending_intake_reviews())
    total = len(replies) + len(intake_reviews)
    return render(request, 'admin_dashboard/_needs_you_list.html', {
        'replies': replies,
        'intake_reviews': intake_reviews,
        'needs_you_count': total,
    })


@admin_required
def needs_you(request):
    replies = list(_needs_you_replies())
    intake_reviews = list(_pending_intake_reviews())
    total = len(replies) + len(intake_reviews)
    return render(request, 'admin_dashboard/needs_you.html', _admin_context(
        active='needs_you',
        replies=replies,
        intake_reviews=intake_reviews,
        needs_you_count=total,
    ))


@admin_required
@require_POST
def intake_review_mark_done(request, client_id):
    """
    Clear the intake-review flag on a client and return the refreshed
    Needs You list partial (HTMX swap).
    """
    from clients.models import ClientProfile
    client = get_object_or_404(ClientProfile, id=client_id)
    if client.needs_admin_review_at and not client.admin_reviewed_at:
        client.admin_reviewed_at = timezone.now()
        client.save(update_fields=[
            'admin_reviewed_at', 'updated_at'])
    return _render_needs_you_list(request)


@admin_required
@require_POST
def needs_you_draft(request, pk):
    """HTMX — generate an AI-drafted reply, persist it, return the textarea."""
    reply = get_object_or_404(
        EmailReply, pk=pk, needs_human=True, handled=False
    )
    draft, error = '', ''
    if not settings.ANTHROPIC_API_KEY:
        error = 'AI drafting unavailable — no API key set. Write your reply manually.'
    else:
        try:
            draft = _generate_reply_draft(reply)
            reply.ai_suggested_reply = draft
            reply.save(update_fields=['ai_suggested_reply'])
        except Exception:
            error = 'AI draft failed — write your reply manually, or try again.'
    return render(request, 'admin_dashboard/_reply_textarea.html', {
        'reply': reply,
        'draft': draft or reply.ai_suggested_reply,
        'error': error,
    })


@admin_required
@require_POST
def needs_you_send(request, pk):
    """HTMX — send the (edited) reply via SendGrid, log it, mark handled."""
    reply = get_object_or_404(
        EmailReply, pk=pk, needs_human=True, handled=False
    )
    lead = reply.lead
    body = (request.POST.get('reply_body') or '').strip()
    if not body or not lead.email:
        # Nothing to send / no address — leave the reply in the queue.
        return _render_needs_you_list(request)

    subject = f'RE: {reply.subject}' if reply.subject else 'RE: your message'
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.EMAIL_FROM_MAIN,
        recipient_list=[lead.email],
        fail_silently=True,
    )
    # Log to the lead's timeline. sequence_step=0 marks a manual reply.
    EmailSent.objects.create(
        lead=lead,
        subject=subject,
        body=body,
        from_email=settings.EMAIL_FROM_MAIN,
        sequence_step=0,
    )
    now = timezone.now()
    reply.handled = True
    reply.handled_at = now
    reply.save(update_fields=['handled', 'handled_at'])
    lead.last_contacted_at = now
    lead.save(update_fields=['last_contacted_at', 'updated_at'])
    return _render_needs_you_list(request)


@admin_required
@require_POST
def needs_you_archive(request, pk):
    """HTMX — mark the reply handled without sending anything."""
    reply = get_object_or_404(
        EmailReply, pk=pk, needs_human=True, handled=False
    )
    reply.handled = True
    reply.handled_at = timezone.now()
    reply.save(update_fields=['handled', 'handled_at'])
    return _render_needs_you_list(request)


@admin_required
@require_POST
def needs_you_unsubscribe(request, pk):
    """HTMX — permanent suppression. Per CLAUDE.md, unsubscribes are forever:
    add to SuppressionList, flag the lead, pause sequences, mark handled."""
    reply = get_object_or_404(
        EmailReply, pk=pk, needs_human=True, handled=False
    )
    lead = reply.lead
    now = timezone.now()

    if lead.email:
        domain = lead.email.split('@')[-1] if '@' in lead.email else ''
        SuppressionList.objects.get_or_create(
            email=lead.email.lower(),
            defaults={'domain': domain, 'reason': 'Unsubscribe request'},
        )

    lead.unsubscribed = True
    lead.unsubscribed_at = now
    lead.sequence_paused = True
    lead.status = 'unsubscribed'
    lead.save(update_fields=[
        'unsubscribed', 'unsubscribed_at', 'sequence_paused',
        'status', 'updated_at',
    ])

    reply.classification = 'unsubscribe'
    reply.handled = True
    reply.handled_at = now
    reply.save(update_fields=['classification', 'handled', 'handled_at'])
    return _render_needs_you_list(request)


def _generate_reply_draft(reply):
    """Use Claude (Haiku 4.5) to draft a reply to an inbound EmailReply."""
    from anthropic import Anthropic

    lead = reply.lead
    if reply.email_sent:
        original = f'Subject: {reply.email_sent.subject}\n\n{reply.email_sent.body}'
    else:
        original = '(original outreach email not on file)'

    contact = f', {lead.attorney_name}' if lead.attorney_name else ''
    prompt = f"""You are Zachery Long, founder of Aspired Websites LLC, a web
design agency for law firms and small businesses. A prospect replied to your
outreach email. Draft a brief, warm, professional reply.

PROSPECT: {lead.firm_name}{contact} — {lead.business_type}
REPLY WAS FLAGGED AS: {reply.get_classification_display() or 'needs review'}

THE EMAIL YOU SENT:
{original}

THEIR REPLY:
{reply.body}

Draft a reply that:
- Directly answers what they asked or raised
- Is warm, concise, and genuinely human — never salesy or templated
- Moves toward a short phone call when that makes sense
- Signs off simply as "Zachery"
- Is plain text — no markdown, no subject line, just the message body

Write the reply now."""

    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=700,
        messages=[{'role': 'user', 'content': prompt}],
    )
    return message.content[0].text.strip()


# Domain warming schedule — fixed calendar dates per CLAUDE.md → Domain Warming.
WARMING_START = datetime.date(2026, 5, 20)
WARMING_TIER_2 = datetime.date(2026, 6, 3)    # weeks 3-4 begin
WARMING_TIER_3 = datetime.date(2026, 6, 17)   # weeks 5-6 begin
OUTREACH_ELIGIBLE = datetime.date(2026, 7, 1)  # cold outreach can begin


def _warming_status():
    """Compute current domain-warming phase, cap, and eligibility."""
    today = timezone.localdate()
    days_in = (today - WARMING_START).days
    week_number = max(1, (days_in // 7) + 1)

    if today < WARMING_TIER_2:
        phase, cap = 'Weeks 1-2', 10
    elif today < WARMING_TIER_3:
        phase, cap = 'Weeks 3-4', 25
    elif today < OUTREACH_ELIGIBLE:
        phase, cap = 'Weeks 5-6', 50
    else:
        phase, cap = 'Warming complete', None

    return {
        'start': WARMING_START,
        'eligible': OUTREACH_ELIGIBLE,
        'today': today,
        'week_number': week_number,
        'phase': phase,
        'current_cap': cap,
        'eligible_now': today >= OUTREACH_ELIGIBLE,
        'days_until_eligible': max(0, (OUTREACH_ELIGIBLE - today).days),
    }


@admin_required
def settings_view(request):
    config = OutreachSettings.load()

    # Anchor the warming start date on the singleton if not yet set.
    if config.warming_start_date is None:
        config.warming_start_date = WARMING_START
        config.save(update_fields=['warming_start_date'])

    if request.method == 'POST':
        # Trust level — validate against model choices
        try:
            tl = int(request.POST.get('trust_level', config.trust_level))
        except (TypeError, ValueError):
            tl = config.trust_level
        if tl in dict(OutreachSettings.TRUST_LEVEL_CHOICES):
            config.trust_level = tl

        # Daily send cap — clamp to a sane range
        try:
            cap = int(request.POST.get('daily_send_cap', config.daily_send_cap))
            config.daily_send_cap = max(1, min(cap, 500))
        except (TypeError, ValueError):
            pass

        # Outreach active — an unchecked checkbox is simply absent from POST
        config.outreach_active = 'outreach_active' in request.POST

        config.save()
        return redirect(reverse('admin_dashboard:settings') + '?saved=1')

    return render(request, 'admin_dashboard/settings.html', _admin_context(
        active='settings',
        config=config,
        warming=_warming_status(),
        saved=request.GET.get('saved') == '1',
    ))


def _stub(request, *, active, title, blurb):
    return render(request, 'admin_dashboard/_stub.html', _admin_context(
        active=active,
        page_title=title,
        blurb=blurb,
    ))


# ────────────────────────────────────────────────────────────────────────────
# Pricing manager
# ────────────────────────────────────────────────────────────────────────────

_PRICING_CATEGORIES = [
    ('website_build', 'Website Builds'),
    ('maintenance', 'Maintenance Plans'),
    ('social_media', 'Social Media'),
    ('hosting', 'Hosting'),
]


@admin_required
def pricing_list(request):
    """Pricing manager — tiers grouped by category, plus add-ons."""
    from billing.pricing_models import AddonPricing, ServiceTier

    groups = [
        {
            'label': label,
            'category': key,
            'tiers': list(ServiceTier.objects.filter(category=key)),
        }
        for key, label in _PRICING_CATEGORIES
    ]
    missing_stripe = list(
        ServiceTier.objects.filter(
            is_active=True, is_recurring=True, stripe_price_id=''
        )
    )
    return render(request, 'admin_dashboard/pricing_list.html', _admin_context(
        'pricing',
        groups=groups,
        addons=list(AddonPricing.objects.all()),
        missing_stripe=missing_stripe,
        saved=request.GET.get('saved'),
    ))


@admin_required
def pricing_edit(request, tier_id):
    """Edit a single tier — its fields and its feature bullets."""
    from billing.pricing_models import ServiceTier

    tier = get_object_or_404(ServiceTier, id=tier_id)

    if request.method == 'POST':
        form = ServiceTierForm(request.POST, instance=tier)
        if form.is_valid():
            form.save()
            # Persist inline feature edits (text + sort order).
            for feature in tier.features.all():
                text = request.POST.get(f'feature_text_{feature.id}')
                if text is None:
                    continue
                feature.text = text.strip()
                raw_order = request.POST.get(f'feature_order_{feature.id}', '')
                if raw_order.strip().lstrip('-').isdigit():
                    feature.sort_order = int(raw_order)
                feature.save()
            return redirect(
                f"{reverse('admin_dashboard:pricing_list')}?saved={tier.name}"
            )
    else:
        form = ServiceTierForm(instance=tier)

    return render(request, 'admin_dashboard/pricing_edit.html', _admin_context(
        'pricing',
        tier=tier,
        form=form,
        features=list(tier.features.all()),
        is_build=tier.category == 'website_build',
    ))


@admin_required
@require_POST
def pricing_toggle(request, tier_id):
    """HTMX — flip is_active / is_featured on a tier."""
    from billing.pricing_models import ServiceTier

    tier = get_object_or_404(ServiceTier, id=tier_id)
    field = request.POST.get('field')
    if field not in ('is_active', 'is_featured'):
        return HttpResponseBadRequest('Unknown field')
    setattr(tier, field, not getattr(tier, field))
    tier.save(update_fields=[field, 'updated_at'])
    return render(request, 'admin_dashboard/_pricing_toggle.html', {
        'tier': tier,
        'field': field,
        'value': getattr(tier, field),
    })


@admin_required
@require_POST
def pricing_feature_add(request, tier_id):
    """HTMX — append a new (blank) feature row to a tier."""
    from billing.pricing_models import ServiceTier, TierFeature

    tier = get_object_or_404(ServiceTier, id=tier_id)
    feature = TierFeature.objects.create(
        tier=tier, text='', sort_order=tier.features.count() + 1,
    )
    return render(request, 'admin_dashboard/_pricing_feature_row.html', {
        'feature': feature,
    })


@admin_required
@require_POST
def pricing_feature_delete(request, tier_id, fid):
    """HTMX — delete a feature row."""
    from billing.pricing_models import TierFeature

    TierFeature.objects.filter(id=fid, tier_id=tier_id).delete()
    return HttpResponse('')


# ────────────────────────────────────────────────────────────────────────────
# Deployment dashboard
# ────────────────────────────────────────────────────────────────────────────

GITHUB_REPO_DEFAULT = 'https://github.com/ZachF53/AspiredWebsitesLLCRevamped.git'


def _domain_from_url(url):
    """Extract a bare domain (no scheme, no www., no path) from a URL."""
    from urllib.parse import urlparse
    if not url:
        return ''
    netloc = urlparse(url).netloc or url
    netloc = netloc.split('/')[0]
    return netloc[4:] if netloc.startswith('www.') else netloc


@admin_required
def deploy_home(request):
    """Deploy landing page — 3 deploy-type cards + recent deployments."""
    from .models import DeploymentLog
    from clients.models import ClientProfile
    return render(request, 'admin_dashboard/deploy_home.html', _admin_context(
        'deploy',
        recent=DeploymentLog.objects.select_related('client')[:10],
        clients=ClientProfile.objects.order_by('firm_name'),
    ))


@admin_required
def deploy_fresh(request):
    """Fresh-server deploy runbook with live-fill command blocks."""
    from django.utils.text import slugify
    from clients.models import ClientProfile
    options = []
    for client in ClientProfile.objects.filter(do_droplet_ip__isnull=False):
        project = client    # ← alias post-2026-05-25 refactor (project fields on ClientProfile)
        options.append({
            'id': client.id,
            'name': client.firm_name,
            'slug': slugify(client.firm_name),
            'ip': client.do_droplet_ip or '',
            'domain': _domain_from_url(project.live_url) if project else '',
        })
    return render(request, 'admin_dashboard/deploy_fresh.html', _admin_context(
        'deploy',
        client_options=options,
        github_default=GITHUB_REPO_DEFAULT,
    ))


@admin_required
def deploy_redeploy(request):
    """Re-deploy runbook — push + run deploy.sh."""
    return render(request, 'admin_dashboard/deploy_redeploy.html',
                  _admin_context('deploy'))


@admin_required
def deploy_client(request, client_id):
    """Client-site deploy runbook, pre-filled from the ClientProfile."""
    from django.utils.text import slugify
    from clients.models import ClientProfile
    client = get_object_or_404(ClientProfile, id=client_id)
    project = client    # ← alias post-2026-05-25 refactor (project fields on ClientProfile)
    return render(request, 'admin_dashboard/deploy_client.html', _admin_context(
        'deploy',
        deploy_client=client,
        prefill_ip=client.do_droplet_ip or '',
        prefill_domain=_domain_from_url(project.live_url) if project else '',
        prefill_client=slugify(client.firm_name),
        github_default=GITHUB_REPO_DEFAULT,
    ))


@admin_required
def deploy_history(request):
    """Table of all DeploymentLog records + a manual log form."""
    from .models import DeploymentLog
    return render(request, 'admin_dashboard/deploy_history.html', _admin_context(
        'deploy',
        logs=DeploymentLog.objects.select_related('client'),
        form=DeploymentLogForm(),
        logged=request.GET.get('logged'),
    ))


@admin_required
@require_POST
def deploy_log_create(request):
    """Create a DeploymentLog from the manual log form."""
    from .models import DeploymentLog
    form = DeploymentLogForm(request.POST)
    if form.is_valid():
        form.save()
        return redirect(
            f"{reverse('admin_dashboard:deploy_history')}?logged=1"
        )
    return render(request, 'admin_dashboard/deploy_history.html', _admin_context(
        'deploy',
        logs=DeploymentLog.objects.select_related('client'),
        form=form,
    ))


# ────────────────────────────────────────────────────────────────────────────
# Site changelog — per-client website change log
# ────────────────────────────────────────────────────────────────────────────

# Matches a deploy.sh step line, e.g. "[3/7] Running migrations..."
_DEPLOY_STEP_RE = re.compile(r'^\s*\[(\d+)/(\d+)\]\s*(.+?)\s*$')


def _is_uuid(value):
    """True if `value` parses as a UUID — guards filters against bad params."""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _parse_deploy_log(text):
    """Pull the '[n/n] description' step lines out of raw deploy.sh output."""
    steps = []
    for line in (text or '').splitlines():
        match = _DEPLOY_STEP_RE.match(line)
        if match:
            desc = match.group(3).strip()
            if desc:
                steps.append(desc)
    return steps


@admin_required
def changelog_list(request):
    """All changelog entries across every client, with filters."""
    from clients.models import ClientProfile, SiteChangelogEntry
    from django.utils.dateparse import parse_date

    entries = SiteChangelogEntry.objects.select_related('client')

    client_filter = request.GET.get('client', '')
    type_filter = request.GET.get('change_type', '')
    visible_filter = request.GET.get('visible', '')
    date_from = request.GET.get('from', '')
    date_to = request.GET.get('to', '')

    if client_filter and _is_uuid(client_filter):
        entries = entries.filter(client_id=client_filter)
    if type_filter:
        entries = entries.filter(change_type=type_filter)
    if visible_filter == 'yes':
        entries = entries.filter(is_client_visible=True)
    elif visible_filter == 'no':
        entries = entries.filter(is_client_visible=False)
    if parse_date(date_from):
        entries = entries.filter(date_of_change__gte=date_from)
    if parse_date(date_to):
        entries = entries.filter(date_of_change__lte=date_to)

    return render(request, 'admin_dashboard/changelog_list.html', _admin_context(
        'changelog',
        entries=entries,
        clients=ClientProfile.objects.order_by('firm_name'),
        change_type_choices=SiteChangelogEntry.CHANGE_TYPE_CHOICES,
        client_filter=client_filter,
        type_filter=type_filter,
        visible_filter=visible_filter,
        date_from=date_from,
        date_to=date_to,
    ))


@admin_required
def client_changelog(request, client_id):
    """Changelog entries for a single client."""
    from clients.models import ClientProfile, SiteChangelogEntry
    client = get_object_or_404(ClientProfile, id=client_id)
    return render(request, 'admin_dashboard/changelog_list.html', _admin_context(
        'changelog',
        entries=SiteChangelogEntry.objects.filter(client=client),
        single_client=client,
        change_type_choices=SiteChangelogEntry.CHANGE_TYPE_CHOICES,
    ))


@admin_required
def changelog_add(request, client_id=None):
    """Add a changelog entry — pre-fills the client when client-scoped."""
    from clients.models import ClientProfile
    from .forms import SiteChangelogForm

    preset_client = (
        get_object_or_404(ClientProfile, id=client_id) if client_id else None
    )

    if request.method == 'POST':
        form = SiteChangelogForm(request.POST)
        if form.is_valid():
            entry = form.save()
            if client_id:
                return redirect('admin_dashboard:client_changelog',
                                client_id=entry.client_id)
            return redirect('admin_dashboard:changelog_list')
    else:
        form = SiteChangelogForm(
            initial={'client': preset_client} if preset_client else None
        )

    if client_id:
        form_action = reverse('admin_dashboard:changelog_add_client',
                              args=[client_id])
    else:
        form_action = reverse('admin_dashboard:changelog_add')

    return render(request, 'admin_dashboard/changelog_add.html', _admin_context(
        'changelog',
        form=form,
        mode='add',
        preset_client=preset_client,
        form_action=form_action,
        clients=ClientProfile.objects.order_by('firm_name'),
    ))


@admin_required
def changelog_edit(request, entry_id):
    """Edit an existing changelog entry."""
    from clients.models import SiteChangelogEntry
    from .forms import SiteChangelogForm

    entry = get_object_or_404(SiteChangelogEntry, id=entry_id)

    if request.method == 'POST':
        form = SiteChangelogForm(request.POST, instance=entry)
        if form.is_valid():
            form.save()
            if request.POST.get('next') == 'client':
                return redirect('admin_dashboard:client_changelog',
                                client_id=entry.client_id)
            return redirect('admin_dashboard:changelog_list')
    else:
        form = SiteChangelogForm(instance=entry)

    return render(request, 'admin_dashboard/changelog_add.html', _admin_context(
        'changelog',
        form=form,
        mode='edit',
        entry=entry,
        form_action=reverse('admin_dashboard:changelog_edit', args=[entry.id]),
    ))


@admin_required
@require_POST
def changelog_delete(request, entry_id):
    """Delete a changelog entry (POST + CSRF only)."""
    from clients.models import SiteChangelogEntry
    entry = get_object_or_404(SiteChangelogEntry, id=entry_id)
    client_id = entry.client_id
    came_from_client = request.POST.get('next') == 'client'
    entry.delete()
    if came_from_client:
        return redirect('admin_dashboard:client_changelog', client_id=client_id)
    return redirect('admin_dashboard:changelog_list')


@admin_required
@require_POST
def changelog_import(request):
    """
    Parse pasted deploy.sh output into deployment changelog entries.

    Two-step: `step=preview` parses + shows a preview; `step=save` re-parses
    the same text and creates one entry per [n/n] step.
    """
    from clients.models import ClientProfile, SiteChangelogEntry
    from .forms import SiteChangelogForm

    raw_log = request.POST.get('raw_log', '')
    client_id = request.POST.get('import_client', '')
    step = request.POST.get('step', 'preview')

    client = None
    if client_id and _is_uuid(client_id):
        client = ClientProfile.objects.filter(id=client_id).first()

    parsed = _parse_deploy_log(raw_log)

    if step == 'save' and client and parsed:
        today = timezone.localdate()
        for title in parsed:
            # Imported deploy steps land as an internal audit trail — staff
            # flip individual entries visible to surface them to the client.
            SiteChangelogEntry.objects.create(
                client=client,
                change_type='deployment',
                title=title,
                is_client_visible=False,
                date_of_change=today,
            )
        return redirect('admin_dashboard:client_changelog', client_id=client.id)

    import_error = None
    if not parsed:
        import_error = 'No "[n/n]" deploy steps were found in that text.'
    elif not client:
        import_error = 'Choose a client to import these steps into.'

    return render(request, 'admin_dashboard/changelog_add.html', _admin_context(
        'changelog',
        form=SiteChangelogForm(initial={'client': client} if client else None),
        mode='add',
        preset_client=client,
        form_action=reverse('admin_dashboard:changelog_add'),
        clients=ClientProfile.objects.order_by('firm_name'),
        import_preview=parsed,
        import_raw=raw_log,
        import_client=client,
        import_error=import_error,
    ))


# ────────────────────────────────────────────────────────────────────────────
# Clients — list, detail hub, and the Phase 5a monitoring pages
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def client_list(request):
    """All clients — entry point to the per-client monitoring tools."""
    from clients.models import ClientProfile
    from reporting.models import VulnerabilityFinding, VulnerabilityScan

    # Real clients first, testers at the bottom. `is_tester` is False=0 /
    # True=1 so a plain ascending sort puts non-testers first.
    clients = ClientProfile.objects.order_by('is_tester', 'firm_name')
    query = (request.GET.get('q') or '').strip()
    if query:
        clients = clients.filter(firm_name__icontains=query)
    clients = list(clients)

    # Last completed scan per client — single query, indexed by client id.
    last_scan_by_client = {}
    for s in (VulnerabilityScan.objects
              .filter(status='complete', client__in=clients)
              .order_by('client_id', '-completed_at')):
        last_scan_by_client.setdefault(s.client_id, s)

    # Has-open-critical / has-open-high lookups for the severity dot —
    # one count() per scan would be N+1, so pre-aggregate in two queries.
    open_critical_by_scan = set(
        VulnerabilityFinding.objects
        .filter(scan__in=last_scan_by_client.values(),
                status='open', severity='critical')
        .values_list('scan_id', flat=True).distinct()
    )
    open_high_by_scan = set(
        VulnerabilityFinding.objects
        .filter(scan__in=last_scan_by_client.values(),
                status='open', severity='high')
        .values_list('scan_id', flat=True).distinct()
    )

    rows = []
    for c in clients:
        scan = last_scan_by_client.get(c.id)
        if scan is None:
            dot = 'never'  # ⚪
        elif scan.id in open_critical_by_scan:
            dot = 'critical'  # 🔴
        elif scan.id in open_high_by_scan:
            dot = 'high'  # 🟠
        else:
            dot = 'clean'  # 🟢
        rows.append({
            'client': c,
            'last_scan': scan,
            'scan_dot': dot,
        })

    return render(request, 'admin_dashboard/client_list.html', _admin_context(
        'clients', clients=clients, query=query, rows=rows,
    ))


@admin_required
def client_detail(request, client_id):
    """Per-client hub — uptime, GBP, NPS, testimonial, links to each tool."""
    from clients.models import ClientProfile, PROJECT_STAGES
    from reporting.models import GBPSyncCheck, NPSSurvey
    from reporting.uptime_helpers import (
        get_avg_response_time, get_current_status, get_uptime_percentage,
    )

    client = get_object_or_404(ClientProfile, id=client_id)
    # Post-2026-05-25 refactor: project fields live on ClientProfile.
    # `project` alias preserved so existing reads (project.stage,
    # project.intake, project.stage_logs, etc.) keep working.
    project = client

    # Latest GBP check per field.
    gbp_checks, seen = [], set()
    for check in GBPSyncCheck.objects.filter(client=client):  # -checked_at
        if check.field_name not in seen:
            seen.add(check.field_name)
            gbp_checks.append(check)

    nps_surveys = list(NPSSurvey.objects.filter(client=client)[:4])
    latest_nps = next((s for s in nps_surveys if s.score is not None), None)
    nps_avg = NPSSurvey.objects.filter(
        client=client, score__isnull=False).aggregate(a=Avg('score'))['a']

    # Phase 6c — security scan summary for this client.
    # Show the latest COMPLETED scan only (consistency with the client
    # list dot — both surfaces report on known results, not a run in
    # flight). A separate flag flags any scan currently in progress so
    # we can surface a non-misleading "scan in progress" banner.
    from reporting.models import VulnerabilityScan
    last_scan = (VulnerabilityScan.objects
                 .filter(client=client, status='complete')
                 .order_by('-completed_at').first())
    scan_in_progress = VulnerabilityScan.objects.filter(
        client=client, status__in=('pending', 'running')).exists()
    top_open_findings = []
    if last_scan:
        top_open_findings = list(
            last_scan.findings
            .filter(status='open', severity__in=('critical', 'high'))
            .order_by('severity', 'tool', 'title')[:3])

    # Phase 7 Part 3 — Website Intelligence summary for this client.
    latest_intel_report = client.intelligence_reports.first()
    open_intel_suggestions = client.intelligence_suggestions.exclude(
        status__in=['implemented', 'dismissed', 'client_declined']
    ).order_by('-generated_at')[:5]
    intel_pending_count = client.intelligence_suggestions.filter(
        status='pending_review').count()
    intel_sent_count = client.intelligence_suggestions.filter(
        status='sent_to_client').count()

    # Resolved live URL — canonical = client.website. Legacy
    # project.live_url data was backfilled into client.website on
    # 2026-05-25.
    resolved_live_url = client.website or ''

    return render(request, 'admin_dashboard/client_detail.html', _admin_context(
        'clients',
        client=client,
        project=project,
        live_url=resolved_live_url,
        uptime_status=get_current_status(client),
        uptime_30=get_uptime_percentage(client, 30),
        avg_response=get_avg_response_time(client, 30),
        gbp_checks=gbp_checks,
        nps_surveys=nps_surveys,
        latest_nps=latest_nps,
        nps_avg=round(nps_avg, 1) if nps_avg is not None else None,
        freshness_report=client.freshness_reports.first(),
        last_scan=last_scan,
        scan_in_progress=scan_in_progress,
        top_open_findings=top_open_findings,
        latest_intel_report=latest_intel_report,
        open_intel_suggestions=open_intel_suggestions,
        intel_pending_count=intel_pending_count,
        intel_sent_count=intel_sent_count,
        # Project stage controls — list of (slug, label) and the
        # next-stage slug for the "Move to next →" quick button.
        project_stages=list(PROJECT_STAGES),
        next_stage_slug=_compute_next_stage(project),
        recent_stage_logs=list(
            project.stage_logs.all()[:5]) if project else [],
    ))


def _compute_next_stage(project):
    """Return the slug of the stage following `project.stage`, or '' if
    the project is already live (no further stage)."""
    if project is None:
        return ''
    from clients.models import PROJECT_STAGES
    slugs = [k for k, _ in PROJECT_STAGES]
    try:
        idx = slugs.index(project.stage)
    except ValueError:
        return ''
    return slugs[idx + 1] if idx + 1 < len(slugs) else ''


@admin_required
def clients_onboarding(request):
    """
    Legacy-client onboarding status board — every pre-platform client and
    what still needs finishing on each (user, live URL, SSH vault key,
    uptime monitoring, email). Cards are colour-coded by completeness so
    the most-stale row jumps out first.
    """
    from clients.models import ClientProfile, UptimeRecord
    from vault.models import ClientVault, VaultCredential

    legacy = (
        ClientProfile.objects
        .filter(internal_notes__contains='Legacy client')
        .order_by('firm_name')
    )

    # Cheap lookups so we don't do N+1 queries inside the template.
    vault_ids_by_client = {}
    for cred in VaultCredential.objects.filter(
            is_ssh_credential=True,
            vault__client__in=legacy).select_related('vault'):
        # First SSH credential wins — link straight into it from the card.
        vault_ids_by_client.setdefault(cred.vault.client_id, cred.id)
    has_uptime = set(UptimeRecord.objects.filter(
        client__in=legacy).values_list('client_id', flat=True).distinct())

    cards = []
    any_missing_key = False
    any_missing_url = False
    for client in legacy:
        # client.website is the canonical live URL (post-2026-05-25).
        live_url = client.website or ''
        # A "real" user account is one that can log in — the seed command
        # creates inactive placeholder users with unusable passwords for
        # legacy clients we don't have an email for yet.
        has_user = bool(
            client.user
            and client.user.is_active
            and client.user.has_usable_password())
        has_email = bool(client.user and client.user.email)
        has_live_url = bool(live_url)
        cred_id = vault_ids_by_client.get(client.id)
        has_vault_key = cred_id is not None
        has_uptime_data = has_live_url and (client.id in has_uptime)
        # Read the real boolean now that it's backfilled — leave the
        # internal_notes string lookup as a fallback for any rows that
        # haven't been re-saved since the backfill (belt + suspenders).
        is_tester = bool(client.is_tester) or (
            'Tester: True' in (client.internal_notes or ''))

        # Testers only need a vault key + working email + live URL if you
        # actually plan to use them externally. For the colour-coded card
        # border, only count the checks that genuinely matter.
        if is_tester:
            checks = [has_user, has_vault_key]
        else:
            checks = [has_user, has_live_url, has_vault_key,
                      has_uptime_data, has_email]
        done = sum(1 for c in checks if c)
        if done == len(checks):
            border = 'teal'
        elif done == 0:
            border = 'red'
        else:
            border = 'orange'

        # Don't drive top-of-page warnings off tester clients — they're
        # internal-only by definition.
        if not is_tester and not has_vault_key:
            any_missing_key = True
        if not is_tester and not has_live_url:
            any_missing_url = True

        cards.append({
            'client': client,
            'live_url': live_url,
            'has_user': has_user,
            'has_email': has_email,
            'has_live_url': has_live_url,
            'has_vault_key': has_vault_key,
            'has_uptime_data': has_uptime_data,
            'is_tester': is_tester,
            'cred_id': cred_id,
            'border': border,
            'done': done,
            'total': len(checks),
        })

    fully_green = sum(1 for c in cards if c['border'] == 'teal')
    return render(
        request,
        'admin_dashboard/clients_onboarding.html',
        _admin_context(
            'clients',
            cards=cards,
            any_missing_key=any_missing_key,
            any_missing_url=any_missing_url,
            total=len(cards),
            fully_green=fully_green,
            need_attention=len(cards) - fully_green,
        ),
    )


@admin_required
def client_uptime(request, client_id):
    """Uptime detail — 30/60/90-day stats, open alerts, last 50 checks, chart."""
    from clients.models import ClientProfile, UptimeAlert, UptimeRecord
    from reporting.uptime_helpers import (
        get_avg_response_time, get_current_status, get_uptime_chart_data,
        get_uptime_percentage,
    )

    client = get_object_or_404(ClientProfile, id=client_id)

    chart = get_uptime_chart_data(client, 30)
    max_ms = max((d['avg_response_ms'] or 0 for d in chart), default=0) or 1
    for day in chart:
        day['bar_h'] = round((day['avg_response_ms'] or 0) / max_ms * 100)

    return render(request, 'admin_dashboard/client_uptime.html', _admin_context(
        'clients',
        client=client,
        uptime_status=get_current_status(client),
        uptime_30=get_uptime_percentage(client, 30),
        uptime_60=get_uptime_percentage(client, 60),
        uptime_90=get_uptime_percentage(client, 90),
        avg_response=get_avg_response_time(client, 30),
        open_alerts=UptimeAlert.objects.filter(client=client, is_resolved=False),
        records=UptimeRecord.objects.filter(client=client)[:50],
        chart=chart,
    ))


@admin_required
def client_keywords(request, client_id):
    """Keyword rank tracker for one client + add-keyword form."""
    from clients.models import ClientProfile
    from reporting.keyword_helpers import build_keyword_rows

    from .forms import KeywordForm

    client = get_object_or_404(ClientProfile, id=client_id)
    return render(request, 'admin_dashboard/client_keywords.html', _admin_context(
        'clients',
        client=client,
        keyword_rows=build_keyword_rows(client),
        form=KeywordForm(),
        checked=request.GET.get('checked', ''),
    ))


@admin_required
@require_POST
def keyword_add(request, client_id):
    """Add a tracked keyword for a client."""
    from clients.models import ClientProfile
    from reporting.keyword_helpers import build_keyword_rows

    from .forms import KeywordForm

    client = get_object_or_404(ClientProfile, id=client_id)
    form = KeywordForm(request.POST)
    form.instance.client = client
    if form.is_valid():
        # client isn't a form field, so the (client, keyword) unique_together
        # check is skipped by ModelForm — verify it explicitly here.
        if client.tracked_keywords.filter(
                keyword=form.cleaned_data['keyword']).exists():
            form.add_error(
                'keyword', 'This keyword is already tracked for this client.')
        else:
            form.save()
            return redirect('admin_dashboard:client_keywords',
                            client_id=client.id)
    return render(request, 'admin_dashboard/client_keywords.html', _admin_context(
        'clients',
        client=client,
        keyword_rows=build_keyword_rows(client),
        form=form,
    ))


@admin_required
@require_POST
def keyword_run_check(request, client_id):
    """
    Manual 'Run Check Now'. Live ranks need Google Search Console OAuth
    (Phase 4) — until then this reports the gap rather than failing.
    """
    from clients.models import ClientProfile
    get_object_or_404(ClientProfile, id=client_id)
    return redirect(
        f"{reverse('admin_dashboard:client_keywords', args=[client_id])}"
        f"?checked=gsc_unavailable"
    )


@admin_required
def client_conversions(request, client_id):
    """
    Tier 1 analytics dashboard — overview cards, conversion funnel,
    top pages, scroll-depth distribution, click-density grid, and
    recent session list. Backed by `PageSession` (v2 tracker).

    Falls back gracefully when no PageSession data exists yet (e.g.
    legacy clients still on v1 tracker) — every helper returns an
    empty/zeroed shape.
    """
    from django.core.serializers.json import DjangoJSONEncoder

    from clients.models import ClientProfile
    from reporting.analytics_helpers import (
        click_breakdown, conversion_funnel, overview_stats,
        recent_sessions, scroll_distribution, top_pages,
    )
    from reporting.conversion_helpers import conversion_counts
    from reporting.models import ConversionEvent

    client = get_object_or_404(ClientProfile, id=client_id)
    breakdown = click_breakdown(client)
    return render(request, 'admin_dashboard/client_conversions.html',
                  _admin_context(
                      'clients',
                      client=client,
                      counts=conversion_counts(client),
                      overview=overview_stats(client),
                      funnel=conversion_funnel(client),
                      top_pages=top_pages(client, limit=10),
                      scroll_dist=scroll_distribution(client),
                      click_sections=breakdown['sections'],
                      click_overlay_json=json.dumps(
                          breakdown['overlay_clicks'],
                          cls=DjangoJSONEncoder),
                      click_top_elements=breakdown['top_elements'],
                      click_total=breakdown['total_clicks'],
                      sessions=recent_sessions(client, limit=50),
                      events=ConversionEvent.objects.filter(
                          client=client)[:20],
                  ))


@admin_required
def client_tracker(request, client_id):
    """
    Snippet generator — shows Tier 1 (always available) and Tier 2
    (gated by ClientProfile.session_recording_enabled, which is set
    by the operator either manually or because the client is on a
    Growth/Dominant maintenance plan that includes it).
    """
    from clients.models import ClientProfile
    from reporting.models import ConversionEvent, PageSession

    client = get_object_or_404(ClientProfile, id=client_id)

    base = settings.SITE_BASE_URL
    # ONE snippet, forever. Session recording (Tier 2) is gated
    # by a server-side flag the tracker fetches from
    # /api/tracker-config/<id>/ at runtime — no attribute change
    # needed on the client's site to flip it on or off.
    snippet = (
        f'<script src="{base}/static/js/aspired-tracker.js" '
        f'data-aspired-client="{client.id}" defer></script>'
    )

    # Session recording is "included via plan" when the client's
    # package is in the Session Recording addon's
    # `included_in_plans` list.
    included_via_plan = False
    try:
        from billing.pricing_models import AddonPricing
        recording_addon = AddonPricing.objects.filter(
            slug='addon-session-recording').first()
        if recording_addon:
            included_via_plan = recording_addon.is_included_for(
                client.package)
    except Exception:
        included_via_plan = False

    recording_active = bool(client.session_recording_enabled)

    return render(request, 'admin_dashboard/client_tracker.html',
                  _admin_context(
                      'clients',
                      client=client,
                      snippet=snippet,
                      recording_active=recording_active,
                      recording_included_via_plan=included_via_plan,
                      last_event=ConversionEvent.objects.filter(
                          client=client).first(),
                      last_session=PageSession.objects.filter(
                          client=client).first(),
                  ))


@admin_required
@require_POST
def client_toggle_session_recording(request, client_id):
    """Operator toggle — flip ClientProfile.session_recording_enabled."""
    from clients.models import ClientProfile
    client = get_object_or_404(ClientProfile, id=client_id)
    client.session_recording_enabled = (
        not client.session_recording_enabled)
    client.save(update_fields=[
        'session_recording_enabled', 'updated_at'])
    return redirect('admin_dashboard:client_tracker',
                    client_id=client.id)


@admin_required
@require_POST
def gbp_flag(request, client_id, check_id):
    """Flag a GBP mismatch for fixing — logs an internal changelog note."""
    from clients.models import SiteChangelogEntry
    from reporting.models import GBPSyncCheck

    check = get_object_or_404(GBPSyncCheck, id=check_id, client_id=client_id)
    check.flagged_for_fix = True
    check.save(update_fields=['flagged_for_fix', 'updated_at'])
    SiteChangelogEntry.objects.create(
        client=check.client,
        change_type='other',
        title=f'GBP mismatch flagged: {check.get_field_name_display()}',
        description=(f'Website: {check.website_value}\n'
                     f'GBP: {check.gbp_value}'),
        is_client_visible=False,
    )
    return redirect('admin_dashboard:client_detail', client_id=client_id)


@admin_required
@require_POST
def gbp_resolve(request, client_id, check_id):
    """Mark a GBP mismatch resolved."""
    from reporting.models import GBPSyncCheck
    check = get_object_or_404(GBPSyncCheck, id=check_id, client_id=client_id)
    check.resolved = True
    check.resolved_at = timezone.now()
    check.save(update_fields=['resolved', 'resolved_at', 'updated_at'])
    return redirect('admin_dashboard:client_detail', client_id=client_id)


# ────────────────────────────────────────────────────────────────────────────
# Phase 5b — monthly reports, freshness, NPS, blog, chatbot
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def reports_list(request):
    """All monthly reports, with client/status filters + a generate form."""
    from clients.models import ClientProfile
    from reporting.models import MonthlyReport

    reports = MonthlyReport.objects.select_related('client')
    client_filter = request.GET.get('client', '')
    status_filter = request.GET.get('status', '')
    if client_filter and _is_uuid(client_filter):
        reports = reports.filter(client_id=client_filter)
    if status_filter:
        reports = reports.filter(status=status_filter)

    return render(request, 'admin_dashboard/reports_list.html', _admin_context(
        'reports',
        reports=reports,
        clients=ClientProfile.objects.order_by('firm_name'),
        statuses=MonthlyReport.STATUS_CHOICES,
        client_filter=client_filter,
        status_filter=status_filter,
        done=request.GET.get('done', ''),
    ))


@admin_required
@require_POST
def report_generate_now(request):
    """Generate (and send) one client's monthly report immediately."""
    from datetime import date

    from clients.models import ClientProfile
    from reporting.tasks import generate_monthly_report

    client_id = request.POST.get('client', '')
    if not _is_uuid(client_id) or not ClientProfile.objects.filter(
            id=client_id).exists():
        return redirect('admin_dashboard:reports_list')
    try:
        month = date.fromisoformat(
            request.POST.get('report_month', '')).replace(day=1)
    except (ValueError, TypeError):
        today = timezone.localdate()
        month = (date(today.year - 1, 12, 1) if today.month == 1
                 else date(today.year, today.month - 1, 1))
    generate_monthly_report(client_id, month.isoformat())
    return redirect(f"{reverse('admin_dashboard:reports_list')}?done=1")


@admin_required
@require_POST
def report_resend(request, report_id):
    """Re-send an already-generated monthly report."""
    from reporting.models import MonthlyReport
    from reporting.tasks import send_monthly_report_email
    report = get_object_or_404(MonthlyReport, id=report_id)
    send_monthly_report_email(report)
    return redirect(f"{reverse('admin_dashboard:reports_list')}?done=1")


@admin_required
def report_download(request, report_id):
    """Download a monthly report's generated file."""
    import os

    from django.http import FileResponse, Http404

    from reporting.models import MonthlyReport
    report = get_object_or_404(MonthlyReport, id=report_id)
    abs_path = os.path.join(settings.MEDIA_ROOT, report.pdf_path or '')
    if not report.pdf_path or not os.path.exists(abs_path):
        raise Http404('Report file not found.')
    return FileResponse(
        open(abs_path, 'rb'), as_attachment=True,
        filename=os.path.basename(abs_path))


@admin_required
def client_freshness(request, client_id):
    """Content-freshness report for one client."""
    from clients.models import ClientProfile
    from reporting.models import ContentFreshnessReport

    client = get_object_or_404(ClientProfile, id=client_id)
    reports = ContentFreshnessReport.objects.filter(client=client)
    report_id = request.GET.get('report', '')
    report = (reports.filter(id=report_id).first() if _is_uuid(report_id)
              else reports.first())
    return render(request, 'admin_dashboard/client_freshness.html',
                  _admin_context(
                      'clients', client=client, report=report,
                      previous_reports=list(reports[:12])))


@admin_required
@require_POST
def freshness_generate(request, client_id):
    """Run a freshness crawl for one client on demand."""
    from clients.models import ClientProfile
    from reporting.tasks import generate_freshness_report
    client = get_object_or_404(ClientProfile, id=client_id)
    generate_freshness_report(str(client.id))
    return redirect('admin_dashboard:client_freshness', client_id=client.id)


@admin_required
@require_POST
def freshness_flag(request, client_id):
    """Flag a stale page — logs an internal-only changelog entry."""
    from clients.models import ClientProfile, SiteChangelogEntry
    client = get_object_or_404(ClientProfile, id=client_id)
    url = (request.POST.get('url') or '').strip()
    title = (request.POST.get('title') or '').strip()
    SiteChangelogEntry.objects.create(
        client=client,
        change_type='content_update',
        title=f'Content flagged for update: {title or url}'[:200],
        description=f'Flagged from the content freshness report.\n{url}',
        is_client_visible=False,
        url_changed=url[:200],
    )
    return redirect('admin_dashboard:client_freshness', client_id=client.id)


@admin_required
def nps_list(request):
    """All NPS responses across clients, with a score-band filter."""
    from reporting.models import NPSSurvey

    surveys = NPSSurvey.objects.select_related('client')
    band = request.GET.get('band', '')
    if band == 'promoter':
        surveys = surveys.filter(score__gte=9)
    elif band == 'passive':
        surveys = surveys.filter(score__gte=7, score__lte=8)
    elif band == 'detractor':
        surveys = surveys.filter(score__lte=6, score__isnull=False)
    elif band == 'no_response':
        surveys = surveys.filter(score__isnull=True)

    responded = NPSSurvey.objects.exclude(score__isnull=True)
    avg = responded.aggregate(a=Avg('score'))['a']
    return render(request, 'admin_dashboard/nps_list.html', _admin_context(
        'nps',
        surveys=list(surveys[:200]),
        band=band,
        avg_score=round(avg, 1) if avg is not None else None,
        response_count=responded.count(),
    ))


# ── AI blog generator ───────────────────────────────────────────────────────

_BLOG_WORD_TARGETS = {'short': 500, 'medium': 800, 'long': 1200}


def _blog_system_prompt(client, topic, keyword, length, tone):
    """The system prompt for AI blog generation."""
    from reporting.ai import client_location_phrase

    words = _BLOG_WORD_TARGETS.get(length, 800)
    biz = client.business_type or 'business'
    keyword_line = (
        f'- Naturally include the target keyword "{keyword}" 3-5 times\n'
        if keyword else '')
    return (
        f'You are an expert content writer specializing in {biz} SEO. Write a '
        f'blog post for {client.firm_name}, a {biz}'
        f'{client_location_phrase(client)}.\n\n'
        f'Topic: {topic}\n'
        f'Target keyword: {keyword or "(none specified)"}\n'
        f'Length: approximately {words} words\n'
        f'Tone: {tone}\n\n'
        'The post should:\n'
        '- Be informative and helpful to potential clients\n'
        f'{keyword_line}'
        f'- Include a clear call to action at the end mentioning '
        f'{client.firm_name}\n'
        '- Be formatted as clean HTML with proper heading tags (h2, h3), '
        'paragraph tags, and a bulleted list where appropriate\n'
        '- Start with an engaging introduction\n'
        f'- End with: contact {client.firm_name} at '
        f'{client.phone or "our office"} for a free consultation\n\n'
        'Return ONLY the HTML content — no explanations, no markdown fences.'
    )


def _generate_blog_content(post, length, tone):
    """Run AI generation, populating post.title / content / meta_description."""
    import re as _re

    from django.utils.html import strip_tags

    from reporting.ai import MODEL_CONTENT, claude_complete

    content = claude_complete(
        [{'role': 'user',
          'content': f'Write the blog post about: {post.topic}'}],
        system=_blog_system_prompt(
            post.client, post.topic, post.target_keyword, length, tone),
        model=MODEL_CONTENT, max_tokens=4000,
    )
    content = content.replace('```html', '').replace('```', '').strip()

    meta = claude_complete(
        [{'role': 'user', 'content': (
            f'Write a 155-character meta description for a blog post. '
            f'Topic: {post.topic}. '
            + (f'Include the keyword: {post.target_keyword}. '
               if post.target_keyword else '')
            + 'Return only the meta description text, nothing else.')}],
        model=MODEL_CONTENT, max_tokens=120,
    )

    post.content = content
    post.meta_description = meta.strip()[:160]
    post.word_count = len(strip_tags(content).split())
    heading = _re.search(r'<h[12][^>]*>(.*?)</h[12]>', content,
                         _re.IGNORECASE | _re.DOTALL)
    post.title = (strip_tags(heading.group(1)).strip()[:300]
                  if heading else post.topic[:300])


@admin_required
def blog_list(request):
    """All AI blog posts across clients, with client/status filters."""
    from clients.models import ClientProfile
    from reporting.models import BlogPost

    posts = BlogPost.objects.select_related('client')
    client_filter = request.GET.get('client', '')
    status_filter = request.GET.get('status', '')
    if client_filter and _is_uuid(client_filter):
        posts = posts.filter(client_id=client_filter)
    if status_filter:
        posts = posts.filter(status=status_filter)
    return render(request, 'admin_dashboard/blog_list.html', _admin_context(
        'blog',
        posts=posts,
        clients=ClientProfile.objects.order_by('firm_name'),
        statuses=BlogPost.STATUS_CHOICES,
        client_filter=client_filter,
        status_filter=status_filter,
    ))


@admin_required
def blog_generate(request):
    """The AI blog post generator form."""
    from reporting.ai import AIError, AINotConfigured, is_configured
    from reporting.models import BlogPost

    from .forms import BlogGenerateForm

    if request.method == 'POST':
        form = BlogGenerateForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            post = BlogPost(
                client=cd['client'], topic=cd['topic'],
                target_keyword=cd['target_keyword'],
                requested_length=cd['length'], requested_tone=cd['tone'],
                status='review', generated_by_ai=True)
            try:
                _generate_blog_content(post, cd['length'], cd['tone'])
            except AINotConfigured:
                form.add_error(None, 'ANTHROPIC_API_KEY is not configured — '
                                     'set it before generating posts.')
            except AIError as exc:
                form.add_error(None, f'AI generation failed: {exc}')
            else:
                post.save()
                return redirect('admin_dashboard:blog_detail', post_id=post.id)
    else:
        form = BlogGenerateForm()
    return render(request, 'admin_dashboard/blog_generate.html', _admin_context(
        'blog', form=form, ai_ready=is_configured(),
    ))


@admin_required
def blog_detail(request, post_id):
    """Review / edit one blog post and run its workflow actions."""
    from django.utils.html import strip_tags

    from reporting.ai import AIError
    from reporting.models import BlogPost

    post = get_object_or_404(BlogPost, id=post_id)
    error = None

    if request.method == 'POST':
        action = request.POST.get('action', 'save')
        post.title = (request.POST.get('title') or post.title)[:300]
        post.meta_description = (request.POST.get('meta_description') or '')[:160]
        post.content = request.POST.get('content') or post.content
        post.word_count = len(strip_tags(post.content).split())

        if action == 'approve':
            post.status = 'approved'
            post.reviewed_by = request.user.get_username()
            post.reviewed_at = timezone.now()
        elif action == 'reject':
            post.status = 'rejected'
        elif action == 'publish':
            post.published_url = (request.POST.get('published_url') or '')[:200]
            post.status = 'published'
            post.published_at = timezone.now()
        elif action == 'regenerate':
            try:
                _generate_blog_content(
                    post, post.requested_length or 'medium',
                    post.requested_tone or 'professional')
                post.status = 'review'
            except AIError as exc:
                error = f'Regeneration failed: {exc}'

        post.save()
        if not error:
            return redirect('admin_dashboard:blog_detail', post_id=post.id)

    return render(request, 'admin_dashboard/blog_detail.html', _admin_context(
        'blog', post=post, error=error,
    ))


# ── AI chatbot configuration ────────────────────────────────────────────────

@admin_required
def client_chatbot(request, client_id):
    """Configure a client's AI chatbot."""
    from clients.models import ClientProfile
    from reporting.models import ClientChatbot

    from .forms import ChatbotConfigForm

    client = get_object_or_404(ClientProfile, id=client_id)
    chatbot, _ = ClientChatbot.objects.get_or_create(client=client)

    if request.method == 'POST':
        form = ChatbotConfigForm(request.POST, instance=chatbot)
        if form.is_valid():
            form.save()
            return redirect('admin_dashboard:client_chatbot', client_id=client.id)
    else:
        form = ChatbotConfigForm(instance=chatbot)

    snippet = (
        f'<script src="{settings.SITE_BASE_URL}/static/js/aspired-chat.js" '
        f'data-aspired-client="{client.id}" defer></script>'
    )
    return render(request, 'admin_dashboard/client_chatbot.html', _admin_context(
        'clients',
        client=client,
        chatbot=chatbot,
        form=form,
        snippet=snippet,
        conversations=list(chatbot.conversations.all()[:20]),
    ))


@admin_required
@require_POST
def chatbot_regenerate_prompt(request, client_id):
    """Use Claude to write a system prompt from the client's info + FAQs."""
    from clients.models import ClientProfile
    from reporting.ai import MODEL_CONTENT, AIError, claude_complete
    from reporting.models import ClientChatbot

    client = get_object_or_404(ClientProfile, id=client_id)
    chatbot, _ = ClientChatbot.objects.get_or_create(client=client)

    project = client    # ← alias post-2026-05-25 refactor (project fields on ClientProfile)
    intake = getattr(project, 'intake', None) if project else None
    practice_areas = getattr(intake, 'practice_areas', '') or ''
    raw = (
        f'Business: {client.firm_name}\n'
        f'Type: {client.business_type or "law firm"}\n'
        f'Phone: {client.phone or "(not set)"}\n'
        f'Practice areas / services: {practice_areas or "(not provided)"}\n'
        f'FAQ notes:\n{chatbot.faq_text or "(none provided)"}'
    )
    try:
        prompt = claude_complete(
            [{'role': 'user', 'content': (
                'Write a concise, professional system prompt (3-6 sentences) '
                'for an AI website chatbot, based on the business info below. '
                'Describe what the bot helps visitors with and the key facts '
                'it should know. Return only the prompt text.\n\n' + raw)}],
            model=MODEL_CONTENT, max_tokens=500,
        )
        chatbot.system_prompt = prompt
        chatbot.save(update_fields=['system_prompt', 'updated_at'])
    except AIError:
        logger.exception('Chatbot prompt regeneration failed')
    return redirect('admin_dashboard:client_chatbot', client_id=client.id)


@admin_required
def chatbot_conversation(request, client_id, conv_id):
    """Full transcript of one chatbot conversation."""
    from clients.models import ClientProfile
    from reporting.models import ChatbotConversation

    client = get_object_or_404(ClientProfile, id=client_id)
    conversation = get_object_or_404(
        ChatbotConversation, id=conv_id, chatbot__client=client)
    return render(request, 'admin_dashboard/chatbot_conversation.html',
                  _admin_context(
                      'clients', client=client, conversation=conversation))


@admin_required
@require_POST
def testimonial_mark_received(request, client_id):
    """Record a received video testimonial against a client."""
    from clients.models import ClientProfile
    client = get_object_or_404(ClientProfile, id=client_id)
    client.testimonial_received = True
    client.testimonial_url = (request.POST.get('testimonial_url') or '')[:200]
    client.save(update_fields=[
        'testimonial_received', 'testimonial_url', 'updated_at'])
    return redirect('admin_dashboard:client_detail', client_id=client.id)


# ────────────────────────────────────────────────────────────────────────────
# Phase 6b — Droplet dashboard
# ────────────────────────────────────────────────────────────────────────────

DROPLET_REGIONS = [
    ('nyc1', 'NYC1 — New York'),
    ('nyc3', 'NYC3 — New York 3'),
    ('sfo3', 'SFO3 — San Francisco'),
    ('ams3', 'AMS3 — Amsterdam'),
    ('sgp1', 'SGP1 — Singapore'),
    ('lon1', 'LON1 — London'),
    ('fra1', 'FRA1 — Frankfurt'),
    ('tor1', 'TOR1 — Toronto'),
    ('blr1', 'BLR1 — Bangalore'),
]

DROPLET_SIZES = [
    {'slug': 's-1vcpu-1gb', 'vcpus': 1, 'memory_gb': 1,
     'disk_gb': 25, 'price': 6},
    {'slug': 's-1vcpu-2gb', 'vcpus': 1, 'memory_gb': 2,
     'disk_gb': 50, 'price': 12},
    {'slug': 's-2vcpu-2gb', 'vcpus': 2, 'memory_gb': 2,
     'disk_gb': 60, 'price': 18},
    {'slug': 's-2vcpu-4gb', 'vcpus': 2, 'memory_gb': 4,
     'disk_gb': 80, 'price': 24},
]


def _droplet_rows(droplets, clients_by_ip):
    """
    Decorate raw DO droplet dicts with the dashboard display fields.

    `is_client_droplet` is True if EITHER the DO tag list contains
    'client' OR the IP matches a ClientProfile.do_droplet_ip. This
    second arm matters for legacy Droplets that pre-date the tagging
    convention — without it the Destroy button would arm for every
    real client server (footgun).
    """
    rows = []
    for d in droplets:
        linked_client = clients_by_ip.get(d['ip'])
        tag_says_client = 'client' in (d.get('tags') or [])
        is_client = bool(tag_says_client or linked_client)
        is_manual = (not is_client) or 'manual' in (d.get('tags') or [])
        if d['status'] == 'active':
            border = 'green'
        elif d['status'] in ('off', 'archive'):
            border = 'orange'
        else:
            border = 'red'
        rows.append({
            **d,
            'client': linked_client,
            'is_client_droplet': is_client,
            'is_manual_droplet': is_manual,
            'monthly_cost_str': f"${d['monthly_cost']:.0f}/mo",
            'border': border,
        })
    return rows


def _load_droplet_dashboard():
    """Pull DO droplets + match to ClientProfile rows by IP. Pure read."""
    from billing.do_helpers import get_all_droplets
    from clients.models import ClientProfile

    droplets = get_all_droplets()
    clients_by_ip = {
        c.do_droplet_ip: c
        for c in ClientProfile.objects.filter(do_droplet_ip__isnull=False)
        if c.do_droplet_ip
    }
    rows = _droplet_rows(droplets, clients_by_ip)
    return {
        'rows': rows,
        'total_count': len(rows),
        'active_count': sum(1 for r in rows if r['status'] == 'active'),
        'total_cost': sum(r['monthly_cost'] for r in rows),
    }


@admin_required
def droplet_list(request):
    """Full Droplet dashboard — stats + table."""
    data = _load_droplet_dashboard()
    return render(request, 'admin_dashboard/droplets_list.html',
                  _admin_context(
                      'droplets',
                      rows=data['rows'],
                      total_count=data['total_count'],
                      active_count=data['active_count'],
                      total_cost=data['total_cost'],
                      base_snapshot_id=getattr(
                          settings, 'DO_BASE_SNAPSHOT_ID', ''),
                  ))


@admin_required
def droplet_table(request):
    """HTMX partial — just the table rows, polled every 30s on the list page."""
    data = _load_droplet_dashboard()
    return render(request, 'admin_dashboard/_droplet_rows.html', {
        'rows': data['rows'],
    })


@admin_required
def droplet_new(request):
    """
    Render the spin-up form on GET; on POST enqueue the Celery provisioning
    task and redirect back to the list with a notice. The list page then
    HTMX-polls until the new Droplet shows up active.
    """
    from billing.do_helpers import next_droplet_name
    from clients.models import ClientProfile

    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()
        region = (request.POST.get('region') or 'nyc1').strip()
        size = (request.POST.get('size') or 's-1vcpu-1gb').strip()
        client_id = (request.POST.get('client_id') or '').strip() or None

        # Tag based on client linkage — display-time logic mirrors this.
        tags = ['aspired-websites', 'client' if client_id else 'manual']

        from billing.tasks import provision_manual_droplet_task
        provision_manual_droplet_task.delay(
            name=name or next_droplet_name('manual'),
            region=region,
            size=size,
            snapshot_id=int(settings.DO_BASE_SNAPSHOT_ID)
            if settings.DO_BASE_SNAPSHOT_ID else None,
            tags=tags,
            client_id=client_id,
        )
        return redirect(
            f"{reverse('admin_dashboard:droplet_list')}?provisioning={name}")

    # GET — render the form. next_droplet_name() is a live API call, so
    # protect the form against API outages.
    try:
        suggested_name = next_droplet_name('manual')
    except Exception:  # noqa: BLE001 — never block the page
        suggested_name = 'manual-001'

    clients = ClientProfile.objects.order_by('firm_name')

    return render(request, 'admin_dashboard/droplets_new.html', _admin_context(
        'droplets',
        suggested_name=suggested_name,
        regions=DROPLET_REGIONS,
        sizes=DROPLET_SIZES,
        clients=clients,
        base_snapshot_id=getattr(settings, 'DO_BASE_SNAPSHOT_ID', ''),
    ))


@admin_required
@require_POST
def droplet_power(request, droplet_id):
    """
    Power a Droplet on or off. Body: action=on | off. Returns the refreshed
    row partial so the dashboard updates inline via HTMX.
    """
    from billing.do_helpers import (
        get_droplet, power_off_droplet, power_on_droplet,
    )
    from clients.models import ClientProfile

    action = (request.POST.get('action') or '').strip()
    if action == 'on':
        ok = power_on_droplet(droplet_id)
    elif action == 'off':
        ok = power_off_droplet(droplet_id)
    else:
        return HttpResponseBadRequest('action must be "on" or "off"')

    if not ok:
        return HttpResponseBadRequest('DO action failed')

    # Re-fetch for the row refresh (DO is async, so status may still be
    # transitioning — that's fine, the table will keep polling).
    d = get_droplet(droplet_id)
    if d is None:
        return HttpResponseBadRequest('Droplet not found')

    clients_by_ip = {
        c.do_droplet_ip: c
        for c in ClientProfile.objects.filter(do_droplet_ip=d['ip'])
        if c.do_droplet_ip
    }
    rows = _droplet_rows([d], clients_by_ip)
    return render(request, 'admin_dashboard/_droplet_rows.html', {
        'rows': rows, 'single_row': True,
    })


@admin_required
def droplet_destroy(request, droplet_id):
    """
    Destroy a Droplet. GET shows the confirm modal; POST validates the
    typed-name match + refuses if client-tagged + clears the linked
    ClientProfile IP if any.
    """
    from billing.do_helpers import destroy_droplet, get_droplet
    from clients.models import ClientProfile
    from django.contrib import messages

    d = get_droplet(droplet_id)
    if d is None:
        from django.http import Http404
        raise Http404('Droplet not found')

    linked_client = ClientProfile.objects.filter(do_droplet_ip=d['ip']).first()
    # Same rule as the list-row gate: 'client' tag OR a real client linkage
    # via matching IP protects the Droplet. Legacy client Droplets predate
    # the tag convention, so the IP arm matters in production.
    is_client_droplet = (
        'client' in (d.get('tags') or []) or linked_client is not None)

    if request.method == 'POST':
        if is_client_droplet:
            from django.http import HttpResponseForbidden
            return HttpResponseForbidden(
                "Refusing to destroy this Droplet — it is either tagged "
                "'client' or linked to a ClientProfile by IP. "
                "Unlink the client first.")
        typed = (request.POST.get('confirm_name') or '').strip()
        if typed != d['name']:
            messages.error(
                request, "Name did not match — Droplet was NOT destroyed.")
            return redirect('admin_dashboard:droplet_destroy',
                            droplet_id=droplet_id)
        if not destroy_droplet(droplet_id):
            messages.error(
                request, f"DigitalOcean refused to destroy '{d['name']}'.")
            return redirect('admin_dashboard:droplet_list')
        if linked_client:
            linked_client.do_droplet_id = ''
            linked_client.do_droplet_ip = None
            linked_client.save(update_fields=[
                'do_droplet_id', 'do_droplet_ip', 'updated_at'])
        messages.success(
            request, f"Droplet '{d['name']}' has been destroyed.")
        return redirect('admin_dashboard:droplet_list')

    return render(request, 'admin_dashboard/droplets_destroy.html',
                  _admin_context(
                      'droplets',
                      droplet=d,
                      is_client_droplet=is_client_droplet,
                      linked_client=linked_client,
                  ))


@admin_required
def droplet_metrics(request, droplet_id):
    """
    Per-Droplet metrics — DO API basics + (if vault unlocked + we have an
    SSH credential) live supervisor/disk/memory/uptime over SSH.
    Uptime stats come from the existing UptimeRecord table.
    """
    from billing.do_helpers import get_droplet
    from clients.models import ClientProfile
    from reporting.uptime_helpers import (
        get_avg_response_time, get_current_status, get_uptime_percentage,
    )
    from vault.models import VaultCredential
    from vault.views import get_vault_key

    d = get_droplet(droplet_id)
    if d is None:
        from django.http import Http404
        raise Http404('Droplet not found')

    client = ClientProfile.objects.filter(do_droplet_ip=d['ip']).first()

    cred = None
    if d['ip']:
        cred = VaultCredential.objects.filter(
            is_ssh_credential=True,
            vault__client__do_droplet_ip=d['ip']).first()

    vault_key = get_vault_key(request)
    ssh_metrics = None
    if cred and vault_key is not None and not cred.encrypted_with_server_key:
        ssh_metrics = _fetch_ssh_metrics(cred, vault_key)

    uptime_30 = uptime_avg_ms = uptime_status = None
    if client:
        uptime_30 = get_uptime_percentage(client, 30)
        uptime_avg_ms = get_avg_response_time(client, 30)
        uptime_status = get_current_status(client)

    return render(request, 'admin_dashboard/droplets_metrics.html',
                  _admin_context(
                      'droplets',
                      droplet=d,
                      client=client,
                      cred=cred,
                      vault_unlocked=vault_key is not None,
                      ssh_metrics=ssh_metrics,
                      uptime_30=uptime_30,
                      uptime_avg_ms=uptime_avg_ms,
                      uptime_status=uptime_status,
                  ))


def _fetch_ssh_metrics(cred, vault_key):
    """
    Run a handful of read-only diagnostics over SSH. Returns a dict of
    {label: command_output} or None on connection failure. Each command
    is capped short — this is a dashboard view, not a long-running task.
    """
    import paramiko

    from vault.crypto import decrypt_value

    host = decrypt_value(cred.ssh_host_encrypted, vault_key)
    user = decrypt_value(cred.ssh_username_encrypted, vault_key)
    if not host or host == '[decryption failed]' or not user:
        return None

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {
        'hostname': host, 'port': cred.ssh_port or 22, 'username': user,
        'timeout': 10, 'allow_agent': False, 'look_for_keys': False,
    }
    if (cred.ssh_auth_type or 'password') == 'private_key':
        key_text = decrypt_value(cred.ssh_private_key_encrypted, vault_key)
        passphrase = (
            decrypt_value(cred.ssh_key_passphrase_encrypted, vault_key)
            if cred.ssh_key_passphrase_encrypted else None)
        from vault.consumers import _load_private_key
        try:
            connect_kwargs['pkey'] = _load_private_key(key_text, passphrase)
        except Exception:
            return None
    else:
        connect_kwargs['password'] = decrypt_value(
            cred.ssh_password_encrypted, vault_key)

    commands = [
        ('supervisor', 'supervisorctl status'),
        ('disk', 'df -h /'),
        ('memory', 'free -h'),
        ('uptime', 'uptime'),
        ('gunicorn_errors',
         'tail -5 /var/www/aspired/logs/gunicorn-error.log 2>/dev/null'
         ' || echo "(no log)"'),
    ]
    out = {}
    try:
        ssh.connect(**connect_kwargs)
        for label, cmd in commands:
            try:
                _, stdout, _ = ssh.exec_command(cmd, timeout=8)
                out[label] = stdout.read().decode(
                    'utf-8', errors='replace').strip()
            except Exception:
                out[label] = '(failed)'
    except Exception:
        return None
    finally:
        try:
            ssh.close()
        except Exception:
            pass
    return out


# ────────────────────────────────────────────────────────────────────────────
# Phase 6c — vulnerability scans (Part 1 UI: list + run)
# ────────────────────────────────────────────────────────────────────────────

def _scan_row_border(scan):
    """Pick the left-border colour for one scans-table row."""
    if scan.status == 'failed':
        return 'red'
    if scan.status == 'running':
        return 'teal'
    if scan.status == 'pending':
        return 'muted'
    # complete
    if scan.critical_count:
        return 'red'
    if scan.high_count:
        return 'orange'
    return 'green'


def _build_scan_rows(scans):
    """Decorate VulnerabilityScan iterables with display extras."""
    rows = []
    for s in scans:
        duration = None
        if s.started_at and s.completed_at:
            duration = int(
                (s.completed_at - s.started_at).total_seconds())
        rows.append({
            'scan': s,
            'duration_seconds': duration,
            'border': _scan_row_border(s),
        })
    return rows


@admin_required
def scans_list(request):
    """
    Full scan dashboard — filters, pagination, stats, HTMX auto-refresh
    when scans are pending or running, run-new-scan modal.
    """
    from clients.models import ClientProfile
    from reporting.models import VulnerabilityScan

    client_id = (request.GET.get('client') or '').strip()
    status = (request.GET.get('status') or '').strip()

    qs = (VulnerabilityScan.objects
          .select_related('client')
          .order_by('-created_at'))
    if client_id:
        qs = qs.filter(client_id=client_id)
    if status:
        qs = qs.filter(status=status)

    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get('page', 1))
    rows = _build_scan_rows(page.object_list)

    pending_count = VulnerabilityScan.objects.filter(
        status='pending').count()
    running_count = VulnerabilityScan.objects.filter(
        status='running').count()
    last_scan = VulnerabilityScan.objects.order_by(
        '-created_at').first()

    clients = ClientProfile.objects.filter(
        status='active').order_by('firm_name')

    # Preserve filter querystring (sans `page`) so the HTMX partial
    # respects the filters across each poll.
    qs_params = request.GET.copy()
    qs_params.pop('page', None)
    filter_qs = qs_params.urlencode()

    return render(request, 'admin_dashboard/scans_list.html',
                  _admin_context(
                      'scans',
                      rows=rows,
                      page=page,
                      paginator=paginator,
                      total_scans=qs.count(),
                      pending_count=pending_count,
                      running_count=running_count,
                      last_scan=last_scan,
                      clients=clients,
                      selected_client=client_id,
                      selected_status=status,
                      status_choices=VulnerabilityScan.STATUS_CHOICES,
                      type_choices=VulnerabilityScan.SCAN_TYPE_CHOICES,
                      filter_qs=filter_qs,
                      auto_refresh=(pending_count + running_count) > 0,
                  ))


@admin_required
def scans_table(request):
    """HTMX partial — only the table rows, polled every 15s."""
    from reporting.models import VulnerabilityScan

    client_id = (request.GET.get('client') or '').strip()
    status = (request.GET.get('status') or '').strip()

    qs = (VulnerabilityScan.objects
          .select_related('client')
          .order_by('-created_at'))
    if client_id:
        qs = qs.filter(client_id=client_id)
    if status:
        qs = qs.filter(status=status)
    paginator = Paginator(qs, 25)
    page = paginator.get_page(request.GET.get('page', 1))
    rows = _build_scan_rows(page.object_list)

    return render(request, 'admin_dashboard/_scan_rows.html',
                  {'rows': rows, 'page': page})


# ── scan detail helpers ─────────────────────────────────────────────────────

def _ssl_grade_class(grade):
    """CSS class for the SSL grade circle on the scan detail page."""
    if not grade:
        return None
    first = (grade or '').strip()[:1].upper()
    if first == 'A':
        return 'a'
    if first == 'B':
        return 'b'
    if first == 'C':
        return 'c'
    if first in ('D', 'E', 'F', 'T', 'M'):
        return 'f'
    return None


def _build_tool_blocks(scan):
    """
    Per-tool execution summary on the scan-detail page. `status` is one
    of 'ok' / 'skipped' / 'error' / 'idle'; `summary` is a short human
    one-liner ("3 findings", "Grade A", "Skipped — not WordPress", …).
    """
    blocks = []
    for tool, label, raw in (
            ('nmap', 'nmap', scan.raw_nmap),
            ('nikto', 'Nikto', scan.raw_nikto),
            ('ssl', 'SSL Labs', scan.raw_ssl),
            ('wpscan', 'WPScan', scan.raw_wpscan),
    ):
        raw = raw or {}
        if not raw:
            blocks.append({'tool': tool, 'label': label,
                           'status': 'idle', 'summary': 'not run'})
            continue
        if raw.get('skipped'):
            blocks.append({'tool': tool, 'label': label,
                           'status': 'skipped',
                           'summary': raw.get('reason') or 'skipped'})
            continue
        if raw.get('error'):
            blocks.append({'tool': tool, 'label': label,
                           'status': 'error',
                           'summary': str(raw.get('error'))[:120]})
            continue
        if tool == 'ssl':
            grade = raw.get('grade') or '—'
            blocks.append({'tool': tool, 'label': label,
                           'status': 'ok',
                           'summary': f'Grade {grade}'})
        else:
            n = len(raw.get('findings') or [])
            blocks.append({'tool': tool, 'label': label,
                           'status': 'ok',
                           'summary': (
                               f'{n} finding{"" if n == 1 else "s"}')})
    return blocks


@admin_required
def scan_detail(request, scan_id):
    """
    Scan detail — severity-grid header, SSL grade circle if present,
    findings grouped by severity, per-tool execution summary.
    """
    from reporting.models import VulnerabilityScan

    scan = get_object_or_404(
        VulnerabilityScan.objects.select_related('client'),
        id=scan_id,
    )
    findings = list(scan.findings.order_by('severity', 'tool', 'title'))

    by_sev = {sev: [] for sev in
              ('critical', 'high', 'medium', 'low', 'info')}
    for f in findings:
        by_sev.setdefault(f.severity, []).append(f)

    # List form (template-friendly — Django templates can't index a
    # dict by variable key without a custom tag).
    sev_meta = [
        ('critical', 'Critical', '🔴', True),
        ('high',     'High',     '🟠', True),
        ('medium',   'Medium',   '🟡', False),
        ('low',      'Low',      '🔵', False),
        ('info',     'Info',     'ℹ',  False),
    ]
    severity_groups = [
        {'severity': sev, 'label': label, 'glyph': glyph,
         'open_by_default': by_default and bool(by_sev.get(sev)),
         'items': by_sev.get(sev) or []}
        for sev, label, glyph, by_default in sev_meta
    ]

    duration = None
    if scan.started_at and scan.completed_at:
        duration = int(
            (scan.completed_at - scan.started_at).total_seconds())

    ssl_grade = (scan.raw_ssl or {}).get('grade')
    open_count = sum(1 for f in findings if f.status == 'open')

    return render(request, 'admin_dashboard/scan_detail.html',
                  _admin_context(
                      'scans',
                      scan=scan,
                      severity_groups=severity_groups,
                      findings_total=len(findings),
                      open_count=open_count,
                      duration_seconds=duration,
                      tool_blocks=_build_tool_blocks(scan),
                      ssl_grade=ssl_grade,
                      ssl_grade_class=_ssl_grade_class(ssl_grade),
                  ))


@admin_required
@require_POST
def generate_scan_pdf_view(request, scan_id):
    """
    (Re-)generate the PDF for a completed scan. Returns a small HTML
    banner the scan-detail page swaps in via HTMX with a Download link.
    """
    from reporting.models import VulnerabilityScan
    from reporting.scan_runner import generate_scan_pdf

    scan = get_object_or_404(VulnerabilityScan, id=scan_id)
    pdf_path = generate_scan_pdf(str(scan.id))
    if not pdf_path:
        return HttpResponse(
            '<div class="scan-banner scan-banner--error">'
            'PDF generation failed — check server logs.'
            '</div>', status=500)
    download_url = reverse(
        'admin_dashboard:scan_download_pdf', args=[scan.id])
    return HttpResponse(
        f'<div class="scan-banner scan-banner--info">'
        f'PDF generated. '
        f'<a href="{download_url}">Download report &rarr;</a>'
        f'</div>')


@admin_required
def download_scan_pdf(request, scan_id):
    """
    Serve the rendered scan PDF (or HTML fallback) as an attachment.
    `pdf_path` on the scan is RELATIVE to MEDIA_ROOT.
    """
    import os

    from django.http import FileResponse, Http404
    from reporting.models import VulnerabilityScan

    scan = get_object_or_404(VulnerabilityScan, id=scan_id)
    if not scan.pdf_path:
        raise Http404('Report not generated yet.')
    abs_path = os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
    if not os.path.exists(abs_path):
        raise Http404('Report file missing on disk.')

    slug = scan.client.firm_name.replace(' ', '-')
    month = (scan.completed_at or scan.created_at).strftime('%Y-%m')
    ext = os.path.splitext(abs_path)[1] or '.pdf'
    filename = f'security-report-{slug}-{month}{ext}'

    return FileResponse(
        open(abs_path, 'rb'),
        as_attachment=True,
        filename=filename,
        content_type=('application/pdf'
                      if ext == '.pdf' else 'text/html'),
    )


@admin_required
@require_POST
def send_scan_report(request, scan_id):
    """
    Email the scan PDF to the client via SendGrid. Generates the PDF
    first if it isn't on disk yet. Updates `sent_to_client` + `sent_at`
    on the scan record so the button can flip to "Resend Report".
    Returns an HTMX-friendly HTML banner.
    """
    import base64
    import os

    from reporting.models import VulnerabilityScan
    from reporting.scan_runner import generate_scan_pdf

    scan = get_object_or_404(
        VulnerabilityScan.objects.select_related('client'), id=scan_id)
    client = scan.client

    def _banner(kind, msg, status=200):
        return HttpResponse(
            f'<div class="scan-banner scan-banner--{kind}">{msg}</div>',
            status=status)

    # Make sure the PDF exists.
    abs_path = (os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
                if scan.pdf_path else None)
    if not abs_path or not os.path.exists(abs_path):
        generate_scan_pdf(str(scan.id))
        scan.refresh_from_db()
        abs_path = (os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
                    if scan.pdf_path else None)
    if not abs_path or not os.path.exists(abs_path):
        return _banner('error', 'Could not generate PDF.', status=500)

    client_email = client.user.email if client.user else ''
    if not client_email:
        return _banner(
            'error', 'No email address on file for this client.',
            status=400)

    month_str = (scan.completed_at or scan.created_at).strftime('%B %Y')

    if scan.critical_count or scan.high_count:
        severity_line = (
            f"{scan.critical_count} critical and {scan.high_count} "
            f"high severity issue(s) were identified that require "
            f"attention.")
    else:
        severity_line = (
            "No critical or high severity issues were detected. "
            "Your site is in good standing.")

    contact_name = client.contact_name or client.firm_name
    first_name = (contact_name or '').split(' ')[0] or 'there'

    text_body = (
        f'Hi {first_name},\n\n'
        f'Please find attached your monthly security assessment report '
        f'for {month_str}.\n\n'
        f'{severity_line}\n\n'
        f'The full report is attached as a PDF. You can also log into '
        f'your portal to view your security history:\n'
        f'{settings.SITE_BASE_URL}/portal/security/\n\n'
        f'— Zachery Long\nAspired Websites LLC\n'
    )

    ext = os.path.splitext(abs_path)[1] or '.pdf'
    mime = 'application/pdf' if ext.lower() == '.pdf' else 'text/html'
    with open(abs_path, 'rb') as fh:
        pdf_bytes = fh.read()

    from clients.emails import send_branded
    try:
        send_branded(
            subject=(f'Your Security Report — {month_str} — '
                     f'{client.firm_name}'),
            template='security_report',
            context={
                'name': first_name,
                'client_firm': client.firm_name,
                'month_str': month_str,
                'critical_count': scan.critical_count,
                'high_count': scan.high_count,
                'security_url': (
                    f'{settings.SITE_BASE_URL}/portal/security/'),
                'preheader': severity_line,
            },
            recipient_list=[client_email],
            text_body=text_body,
            from_email=getattr(settings, 'EMAIL_FROM_NO_REPLY',
                               settings.DEFAULT_FROM_EMAIL),
            attachments=[
                (f'security-report-{month_str}{ext}', pdf_bytes, mime)],
            fail_silently=False,
        )
    except Exception as exc:  # noqa: BLE001 — surface to operator
        return _banner(
            'error', f'Email send failed: {str(exc)[:200]}', status=500)

    scan.sent_to_client = True
    scan.sent_at = timezone.now()
    scan.save(update_fields=[
        'sent_to_client', 'sent_at', 'updated_at'])

    return _banner('info', f'Report sent to {client_email}.')


@admin_required
@require_POST
def toggle_auto_send_scans(request, client_id):
    """HTMX toggle — flip ClientProfile.auto_send_scan_reports."""
    from clients.models import ClientProfile
    client = get_object_or_404(ClientProfile, id=client_id)
    client.auto_send_scan_reports = not client.auto_send_scan_reports
    client.save(update_fields=['auto_send_scan_reports', 'updated_at'])
    return render(request,
                  'admin_dashboard/_auto_send_scans_toggle.html',
                  {'client': client})


@admin_required
@require_POST
def update_finding_status(request, finding_id):
    """
    HTMX POST: change a VulnerabilityFinding's status.

    Body:
      status          — open | accepted_risk | false_positive | resolved
      acceptance_note — required text when status == accepted_risk

    Returns the refreshed finding card HTML so HTMX swaps it in place.
    """
    from reporting.models import VulnerabilityFinding

    finding = get_object_or_404(VulnerabilityFinding, id=finding_id)
    new_status = (request.POST.get('status') or '').strip()
    valid = {choice for choice, _ in VulnerabilityFinding.STATUS_CHOICES}
    if new_status not in valid:
        return HttpResponseBadRequest('invalid status')

    finding.status = new_status
    if new_status == 'accepted_risk':
        finding.accepted_by = (
            request.user.get_full_name() or request.user.username)[:100]
        finding.accepted_at = timezone.now()
        finding.acceptance_note = (
            request.POST.get('acceptance_note') or '').strip()
    else:
        # Moving away from accepted_risk — wipe the acceptance metadata
        # so the audit trail doesn't show stale acceptance details.
        finding.accepted_by = ''
        finding.accepted_at = None
        finding.acceptance_note = ''
    finding.save(update_fields=[
        'status', 'accepted_by', 'accepted_at',
        'acceptance_note', 'updated_at',
    ])

    return render(request, 'admin_dashboard/_finding_card.html',
                  {'f': finding, 'expanded': True})


@admin_required
@require_POST
def run_scan(request):
    """
    Trigger a scan from the admin client detail page. Body:
      client_id (required), scan_type (default 'full').
    Returns an HTMX fragment for inline status, or redirects if not HTMX.
    """
    from clients.models import ClientProfile
    from reporting.models import VulnerabilityScan
    from reporting.tasks import run_vulnerability_scan_task

    client_id = (request.POST.get('client_id') or '').strip()
    scan_type = (request.POST.get('scan_type') or 'full').strip()
    if scan_type not in dict(VulnerabilityScan.SCAN_TYPE_CHOICES):
        scan_type = 'full'

    client = get_object_or_404(ClientProfile, id=client_id)
    # Post-2026-05-25 refactor: project fields live on ClientProfile.
    # `project` alias preserved so existing reads (project.stage,
    # project.intake, project.stage_logs, etc.) keep working.
    project = client
    # client.website is the canonical live URL (post-2026-05-25 fix).
    # Legacy project.live_url data was backfilled, so we don't need
    # the fallback any more.
    target_url = client.website or ''
    target_ip = client.do_droplet_ip or ''

    if not (target_url or target_ip):
        return HttpResponseBadRequest(
            'Client has no live URL or Droplet IP to scan.')

    scan = VulnerabilityScan.objects.create(
        client=client,
        target_url=target_url,
        target_ip=target_ip,
        scan_type=scan_type,
        is_scheduled=False,
    )
    async_result = run_vulnerability_scan_task.delay(str(scan.id))
    scan.celery_task_id = async_result.id or ''
    scan.save(update_fields=['celery_task_id', 'updated_at'])

    if request.headers.get('HX-Request') == 'true':
        return HttpResponse(
            f'<div class="scan-banner scan-banner--info">'
            f'Scan started — check '
            f'<a href="{reverse("admin_dashboard:scans_list")}">Scans</a> '
            f'for results.</div>')
    return redirect('admin_dashboard:scans_list')


@admin_required
@require_POST
def scan_cancel(request, scan_id):
    """
    Stop a stuck/long-running scan from the scan detail page.

    Two-part teardown:
      1. Revoke the Celery task (terminate=True kills the worker
         process running it — necessary because the scan subprocess
         calls nmap/Nikto which can hang indefinitely on network
         issues)
      2. Mark the scan row as 'cancelled' so the UI reflects it
         immediately + the daily auto-scan cron won't see it as
         'last completed' and reset its scheduling window

    Safe to call on a scan whose Celery task is already gone (worker
    restart, etc.) — revoke is best-effort, the DB update always runs.
    Only acts on scans in 'pending' or 'running' status.
    """
    from django.contrib import messages
    from django.utils import timezone
    from reporting.models import VulnerabilityScan

    scan = get_object_or_404(VulnerabilityScan, id=scan_id)
    if scan.status not in ('pending', 'running'):
        messages.info(
            request,
            f'This scan is already {scan.get_status_display().lower()} — '
            f'nothing to cancel.')
        return redirect('admin_dashboard:scan_detail', scan_id=scan_id)

    if scan.celery_task_id:
        try:
            from AspiredWebsitesRevamped.celery import app as celery_app
            celery_app.control.revoke(
                scan.celery_task_id, terminate=True, signal='SIGTERM')
            logger.info(
                'scan_cancel: revoked celery task %s for scan %s',
                scan.celery_task_id, scan.id)
        except Exception:
            logger.exception(
                'scan_cancel: revoke failed for task %s — proceeding '
                'with DB-only cancellation',
                scan.celery_task_id)

    scan.status = 'cancelled'
    scan.completed_at = timezone.now()
    scan.error_message = (
        f'Cancelled by admin ({request.user}) at '
        f'{timezone.now().isoformat()}'
    )[:2000]
    scan.save(update_fields=[
        'status', 'completed_at', 'error_message', 'updated_at'])
    messages.success(
        request,
        f'Scan cancelled. '
        f'{"Worker process killed." if scan.celery_task_id else ""}')
    return redirect('admin_dashboard:scan_detail', scan_id=scan_id)


# ────────────────────────────────────────────────────────────────────────────
# Client edit + inline quick-edit
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def client_edit(request, client_id):
    """
    Full client edit form.

    The live URL lives on `ClientProfile.website` (the canonical
    field). For backward-compat with existing code that reads
    `Project.live_url` (uptime monitor, scans, monthly reports), we
    also mirror writes to the project when one exists. Initial value
    is read from website first, then falls back to project.live_url
    so legacy data still surfaces correctly.

    Critical: the save MUST work even when the client has no Project
    (auxiliary vault-only profiles, freshly-created accounts, etc.).
    The old code silently no-op'd in that case while flashing
    "saved" — losing the URL.
    """
    from clients.models import ClientProfile
    from django.contrib import messages

    from .forms import ClientProfileEditForm

    client = get_object_or_404(ClientProfile, id=client_id)
    # Post-2026-05-25 refactor: project fields live on ClientProfile.
    # `project` alias preserved so existing reads (project.stage,
    # project.intake, project.stage_logs, etc.) keep working.
    project = client
    # Single source of truth: client.website. Legacy project.live_url
    # data was backfilled on 2026-05-25.
    current_live_url = client.website or ''

    if request.method == 'POST':
        form = ClientProfileEditForm(request.POST, instance=client)
        if form.is_valid():
            client = form.save(commit=False)
            new_url = (form.cleaned_data.get('live_url') or '').strip()
            client.website = new_url
            client.save()
            messages.success(request, 'Client updated successfully.')
            return redirect(
                'admin_dashboard:client_detail', client_id=client.id)
    else:
        form = ClientProfileEditForm(
            instance=client, initial={'live_url': current_live_url})

    return render(request, 'admin_dashboard/client_edit.html',
                  _admin_context(
                      'clients', client=client, form=form,
                      project=project,
                      client_email=(client.user.email if client.user
                                    else ''),
                  ))


# ── Inline HTMX quick-edit on the client detail page ───────────────────────

def _quick_edit_field_meta(field_name):
    """Return (label, html_type, current_value, is_project_field) tuple."""
    from .forms import CLIENT_QUICK_EDIT_FIELDS
    if field_name not in CLIENT_QUICK_EDIT_FIELDS:
        return None
    meta = CLIENT_QUICK_EDIT_FIELDS[field_name]
    return meta['label'], meta['type'], field_name == 'live_url'


@admin_required
def client_quick_edit_field(request, client_id):
    """
    Inline HTMX edit for one of the whitelisted fields. Two states:

      GET ?field=X         → swaps in an <input> + Save button
      POST field=X value=… → persists, swaps back to the display row

    Whitelist lives in `CLIENT_QUICK_EDIT_FIELDS` so the endpoint
    can't be tricked into writing arbitrary model fields.
    """
    from clients.models import ClientProfile
    from .forms import CLIENT_QUICK_EDIT_FIELDS

    client = get_object_or_404(ClientProfile, id=client_id)

    field_name = (
        request.POST.get('field') or request.GET.get('field') or ''
    ).strip()
    if field_name not in CLIENT_QUICK_EDIT_FIELDS:
        return HttpResponseBadRequest('unknown field')
    meta = CLIENT_QUICK_EDIT_FIELDS[field_name]

    # Post-2026-05-25 refactor: project fields live on ClientProfile.
    # `project` alias preserved so existing reads (project.stage,
    # project.intake, project.stage_logs, etc.) keep working.
    project = client

    def _current_value():
        if field_name == 'live_url':
            return client.website or ''
        return getattr(client, field_name, '') or ''

    if request.method == 'POST':
        new_value = (request.POST.get('value') or '').strip()
        # Phone always normalises to (###) ###-#### so the quick-edit
        # path matches every other phone-accepting form.
        if field_name == 'phone':
            from core.phone_utils import normalize_phone
            new_value = normalize_phone(new_value)
        # Live URL: auto-prepend https:// if scheme missing so admins
        # can type "clientdomain.com" without dealing with the
        # protocol prefix. Empty stays empty.
        if field_name == 'live_url' and new_value:
            if not new_value.lower().startswith(('http://', 'https://')):
                new_value = f'https://{new_value}'
        # Live URL writes to client.website (the single source of
        # truth post-2026-05-25 refactor).
        if field_name == 'live_url':
            client.website = new_value
            client.save(update_fields=['website', 'updated_at'])
        else:
            setattr(client, field_name, new_value)
            client.save(update_fields=[field_name, 'updated_at'])
        # After save, render the display row so HTMX swaps back.
        return render(
            request, 'admin_dashboard/_quick_edit_display.html',
            {'client': client, 'field': field_name,
             'label': meta['label'], 'value': _current_value()})

    # GET — either render the editor row, or (when cancelled) drop
    # back to the display row without persisting anything.
    if request.GET.get('cancel') == '1':
        return render(
            request, 'admin_dashboard/_quick_edit_display.html',
            {'client': client, 'field': field_name,
             'label': meta['label'], 'value': _current_value()})

    return render(
        request, 'admin_dashboard/_quick_edit_field.html',
        {'client': client, 'field': field_name,
         'label': meta['label'], 'input_type': meta['type'],
         'value': _current_value()})


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 1 — Business Intelligence dashboard + Daily Focus
# ────────────────────────────────────────────────────────────────────────────

# Sort key for the client-health table: critical first, then at-risk,
# then healthy. Anything outside the choice set drops to the bottom.
_HEALTH_SORT_ORDER = {'critical': 0, 'at_risk': 1, 'healthy': 2}


def _critical_health_count():
    """How many active non-tester clients currently in critical band.
    Used by the sidebar badge + the Intelligence dashboard banner."""
    from clients.models import ClientHealthScore
    # Latest score per client via Subquery would be ideal, but the
    # daily Celery beat means "any critical row from today" is a tight
    # enough proxy; we de-duplicate on client_id in Python.
    from django.utils import timezone as _tz
    today = _tz.now().date()
    rows = (ClientHealthScore.objects
            .filter(health_status='critical',
                    calculated_at__date=today,
                    client__status='active',
                    client__is_tester=False)
            .values_list('client_id', flat=True))
    return len(set(rows))


def get_daily_focus():
    """
    Top-five-most-urgent triage list used by both the Intelligence
    dashboard and the home page Today's Focus widget. Sorted by
    priority (lower number = more urgent).
    """
    from clients.models import ClientHealthScore, ClientProfile

    items = []
    today = timezone.now().date()

    # 1. Critical-band clients flagged today
    critical_scores = (
        ClientHealthScore.objects
        .filter(health_status='critical',
                churn_risk=True,
                calculated_at__date=today)
        .select_related('client')[:3]
    )
    seen_clients = set()
    for hs in critical_scores:
        if hs.client_id in seen_clients:
            continue
        seen_clients.add(hs.client_id)
        items.append({
            'priority': 1,
            'icon': '🔴',
            'title': f'Critical health risk: {hs.client.firm_name}',
            'description': (
                f'Health score: {hs.score}/100 — '
                f'immediate attention needed'),
            'url': reverse('admin_dashboard:client_detail',
                           args=[hs.client.id]),
            'action': 'View Client',
        })

    # 2. Scans with unsent critical findings
    from reporting.models import VulnerabilityScan
    critical_scans = (
        VulnerabilityScan.objects
        .filter(status='complete', critical_count__gt=0,
                sent_to_client=False)
        .select_related('client')
        .order_by('-completed_at')[:3]
    )
    for scan in critical_scans:
        items.append({
            'priority': 2,
            'icon': '🔴',
            'title': (f'Critical scan findings: '
                      f'{scan.client.firm_name}'),
            'description': (
                f'{scan.critical_count} critical finding'
                f'{"" if scan.critical_count == 1 else "s"} '
                f'not yet sent to client'),
            'url': reverse('admin_dashboard:scan_detail',
                           args=[scan.id]),
            'action': 'Review Scan',
        })

    # 3. Active non-tester clients in 'live' stage with no website
    #    set — uptime monitoring + scans can't run without one.
    # Post-2026-05-25: stage + website on ClientProfile directly.
    no_url = (ClientProfile.objects
              .filter(status='active', is_tester=False,
                      stage='live', website='')
              [:3])
    for client in no_url:
        items.append({
            'priority': 3,
            'icon': '⚠',
            'title': f'No live URL: {client.firm_name}',
            'description': (
                'Uptime monitoring and scans cannot run without a '
                'live URL'),
            'url': reverse('admin_dashboard:client_edit',
                           args=[client.id]),
            'action': 'Add URL',
        })

    items.sort(key=lambda x: x['priority'])
    return items[:5]


@admin_required
def intelligence_dashboard(request):
    """
    Business Intelligence — revenue stats + client health table +
    Daily Focus. Admin-only; no client-facing components in this
    phase.
    """
    from clients.health import get_latest_health_score
    from clients.models import (
        ClientHealthScore, ClientProfile, RevenueSnapshot,
    )
    from clients.revenue import (
        get_current_mrr, get_mrr_trend, get_revenue_forecast,
    )

    # ── Revenue ────────────────────────────────────────────────
    mrr = get_current_mrr()
    mrr_trend = get_mrr_trend(months=6)
    forecast = get_revenue_forecast(months=3)
    arr = mrr['mrr_total'] * 12

    # New + churned come from the most recent snapshot — live calc
    # only knows total, not the deltas. Fall back to 0 when no
    # snapshots have been taken yet (fresh install).
    latest_snapshot = RevenueSnapshot.objects.first()
    snap_new = (float(latest_snapshot.mrr_new)
                if latest_snapshot else 0)
    snap_churned = (float(latest_snapshot.mrr_churned)
                    if latest_snapshot else 0)

    # MRR trend chart bar heights — relative to the max so the
    # tallest bar is always 100%. One pass over the data.
    max_mrr = max(
        (row['mrr'] for row in mrr_trend), default=0) or 1
    for row in mrr_trend:
        row['height_pct'] = round(row['mrr'] / max_mrr * 100)

    # ── Client health ─────────────────────────────────────────
    active_clients = (ClientProfile.objects
                      .filter(status='active', is_tester=False)
                      .order_by('firm_name'))
    rows = []
    for client in active_clients:
        rows.append({
            'client': client,
            'health': get_latest_health_score(client),
        })
    rows.sort(key=lambda r: _HEALTH_SORT_ORDER.get(
        r['health'].health_status, 3))

    critical = sum(1 for r in rows
                   if r['health'].health_status == 'critical')
    at_risk = sum(1 for r in rows
                  if r['health'].health_status == 'at_risk')
    healthy = sum(1 for r in rows
                  if r['health'].health_status == 'healthy')

    # ── Intelligence Engine rollups ────────────────────────────
    from clients.models import (
        CompetitorGapReport, IntelligenceSuggestion,
    )
    from django.db.models import Sum as _Sum
    intel_pending = IntelligenceSuggestion.objects.filter(
        status='pending_review').count()
    intel_sent = IntelligenceSuggestion.objects.filter(
        status='sent_to_client').count()
    intel_approved = IntelligenceSuggestion.objects.filter(
        status__in=['client_approved', 'in_scope',
                    'out_of_scope_offered', 'implemented']).count()
    intel_revenue = (IntelligenceSuggestion.objects.filter(
        status__in=['client_approved', 'out_of_scope_offered',
                    'implemented'],
        is_in_maintenance_scope=False,
    ).aggregate(s=_Sum('one_time_fee'))['s'] or 0)

    # ── Competitor gap rollups (Phase 7 Part 5) ────────────────
    gap_reports_run = CompetitorGapReport.objects.filter(
        status='complete').count()
    gap_high_priority = (CompetitorGapReport.objects
        .filter(status='complete')
        .aggregate(s=_Sum('high_priority_gaps'))['s'] or 0)
    gap_suggestions_created = IntelligenceSuggestion.objects.filter(
        suggestion_type='competitor').count()

    return render(request, 'admin_dashboard/intelligence.html',
                  _admin_context(
                      'intelligence',
                      mrr_total=mrr['mrr_total'],
                      arr=arr,
                      mrr_new=snap_new,
                      mrr_churned=snap_churned,
                      active_maintenance_clients=(
                          mrr['active_maintenance_clients']),
                      mrr_breakdown=mrr['breakdown'],
                      mrr_trend=mrr_trend,
                      forecast=forecast,
                      rows=rows,
                      critical_count=critical,
                      at_risk_count=at_risk,
                      healthy_count=healthy,
                      latest_snapshot_month=(
                          latest_snapshot.snapshot_month
                          if latest_snapshot else None),
                      daily_focus=get_daily_focus(),
                      intel_pending=intel_pending,
                      intel_sent=intel_sent,
                      intel_approved=intel_approved,
                      intel_revenue=intel_revenue,
                      gap_reports_run=gap_reports_run,
                      gap_high_priority=gap_high_priority,
                      gap_suggestions_created=(
                          gap_suggestions_created),
                  ))


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 2 — Referrals admin dashboard
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def referrals_list(request):
    """
    Admin view of every ReferralLink with rollup stats at the top and a
    per-link row table beneath. Sorted by conversions then leads so the
    most-effective referrers float up.
    """
    from clients.models import ReferralEvent, ReferralLink

    links = (ReferralLink.objects
             .select_related('client')
             .order_by('-conversions', '-leads_generated',
                       'client__firm_name'))

    totals = ReferralLink.objects.aggregate(
        total_clicks=Count('id'),  # placeholder, overwritten below
    )
    # Use SQL sum, not the placeholder above.
    from django.db.models import Sum
    agg = ReferralLink.objects.aggregate(
        clicks=Sum('clicks'),
        leads=Sum('leads_generated'),
        convs=Sum('conversions'),
        rewards=Sum('total_reward_value'),
    )
    rewards_given = ReferralEvent.objects.filter(
        reward_given=True).aggregate(s=Sum('reward_amount'))['s'] or 0

    return render(request, 'admin_dashboard/referrals_list.html',
                  _admin_context(
                      'referrals',
                      links=links,
                      total_clicks=agg['clicks'] or 0,
                      total_leads=agg['leads'] or 0,
                      total_conversions=agg['convs'] or 0,
                      total_rewards=agg['rewards'] or 0,
                      rewards_given=rewards_given,
                  ))


@admin_required
@require_POST
def referral_toggle_active(request, link_id):
    """Flip ReferralLink.is_active. Returns to the list."""
    from clients.models import ReferralLink
    link = get_object_or_404(ReferralLink, id=link_id)
    link.is_active = not link.is_active
    link.save(update_fields=['is_active', 'updated_at'])
    return redirect('admin_dashboard:referrals_list')


@admin_required
@require_POST
def referral_mark_conversion(request, link_id):
    """
    Record a conversion + optional reward against a ReferralLink.
    POST fields:
      reward_amount  (decimal, default 0)
      reward_note    (text)
    Creates a ReferralEvent(event_type='conversion') and bumps the
    parent link's counters in one go.
    """
    from decimal import Decimal, InvalidOperation

    from clients.models import ReferralEvent, ReferralLink

    link = get_object_or_404(ReferralLink, id=link_id)

    raw = (request.POST.get('reward_amount') or '0').strip()
    try:
        amount = Decimal(raw) if raw else Decimal('0')
    except InvalidOperation:
        amount = Decimal('0')
    note = (request.POST.get('reward_note') or '').strip()[:200]

    ReferralEvent.objects.create(
        referral_link=link,
        event_type='conversion',
        reward_given=amount > 0,
        reward_amount=amount,
        reward_note=note,
    )
    link.conversions = (link.conversions or 0) + 1
    if amount > 0:
        link.total_reward_value = (
            (link.total_reward_value or 0) + amount)
        link.save(update_fields=[
            'conversions', 'total_reward_value', 'updated_at'])
    else:
        link.save(update_fields=['conversions', 'updated_at'])

    return redirect('admin_dashboard:referrals_list')


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 2 — Proposals
# ────────────────────────────────────────────────────────────────────────────

def _active_proposals_count():
    """Sent + viewed proposals — used for the sidebar badge."""
    try:
        from clients.models import Proposal
        return Proposal.objects.filter(
            status__in=('sent', 'viewed')).count()
    except Exception:
        return 0


@admin_required
def proposals_list(request):
    """All proposals, newest first."""
    from clients.models import Proposal
    proposals = (Proposal.objects
                 .select_related('lead')
                 .order_by('-created_at'))
    return render(request, 'admin_dashboard/proposals_list.html',
                  _admin_context(
                      'proposals',
                      proposals=proposals,
                  ))


@admin_required
def proposal_new(request):
    """Proposal creation form."""
    from billing.pricing_models import ServiceTier
    from clients.models import CaseStudy, Proposal
    from outreach.models import Lead

    if request.method == 'POST':
        try:
            from decimal import Decimal
            project_price = Decimal(
                request.POST.get('project_price') or '0')
            maintenance_price = Decimal(
                request.POST.get('maintenance_price') or '0')
        except Exception:
            from decimal import Decimal
            project_price = Decimal('0')
            maintenance_price = Decimal('0')

        # Optional Lead link
        lead = None
        lead_id_raw = (request.POST.get('lead_id') or '').strip()
        if lead_id_raw:
            try:
                lead = Lead.objects.get(pk=int(lead_id_raw))
            except (Lead.DoesNotExist, ValueError):
                lead = None

        # Expiry — default 30 days from now if blank
        expires_raw = (request.POST.get('expires_at') or '').strip()
        if expires_raw:
            try:
                expires_at = datetime.datetime.strptime(
                    expires_raw, '%Y-%m-%d').date()
            except ValueError:
                expires_at = (timezone.now()
                              + datetime.timedelta(days=30)).date()
        else:
            expires_at = (timezone.now()
                          + datetime.timedelta(days=30)).date()

        case_study_ids = request.POST.getlist('case_study_ids')

        proposal = Proposal.objects.create(
            lead=lead,
            prospect_name=(request.POST.get('prospect_name')
                           or '').strip()[:200],
            prospect_email=(request.POST.get('prospect_email')
                            or '').strip()[:254],
            prospect_business=(request.POST.get('prospect_business')
                               or '').strip()[:200],
            prospect_city=(request.POST.get('prospect_city')
                           or '').strip()[:100],
            prospect_state=(request.POST.get('prospect_state')
                            or '').strip()[:50],
            package=(request.POST.get('package') or '').strip()[:100],
            project_price=project_price,
            maintenance_price=maintenance_price,
            goals=(request.POST.get('goals') or '').strip(),
            pain_points=(request.POST.get('pain_points') or '').strip(),
            case_study_ids=list(case_study_ids),
            notes=(request.POST.get('notes') or '').strip(),
            expires_at=expires_at,
            status='draft',
        )

        # Auto-generate the PDF on save so the operator can preview
        # it immediately on the detail page.
        from clients.proposal_pdf import render_proposal_pdf
        try:
            proposal.pdf_path = render_proposal_pdf(proposal)
            proposal.save(update_fields=['pdf_path', 'updated_at'])
        except Exception:
            # Don't block proposal creation on PDF errors — show a
            # banner on the detail page instead.
            pass

        return redirect('admin_dashboard:proposal_detail',
                        proposal_id=proposal.id)

    leads = (Lead.objects
             .filter(status__in=['new', 'contacted', 'replied',
                                 'call_booked', 'proposal_sent'])
             .order_by('-created_at')[:200])
    case_studies = (CaseStudy.objects
                    .filter(is_published=True)
                    .select_related('client')
                    .order_by('-created_at'))

    build_tiers = ServiceTier.objects.filter(
        category='website_build', is_active=True).order_by('sort_order',
                                                           'price')
    maint_tiers = ServiceTier.objects.filter(
        category='maintenance', is_active=True).order_by('sort_order',
                                                        'price')

    return render(request, 'admin_dashboard/proposal_new.html',
                  _admin_context(
                      'proposals',
                      leads=leads,
                      case_studies=case_studies,
                      build_tiers=build_tiers,
                      maint_tiers=maint_tiers,
                  ))


@admin_required
def proposal_detail(request, proposal_id):
    """Single-proposal detail + action buttons."""
    from clients.models import CaseStudy, Proposal

    proposal = get_object_or_404(Proposal, id=proposal_id)

    case_studies = []
    if proposal.case_study_ids:
        case_studies = list(
            CaseStudy.objects.filter(id__in=proposal.case_study_ids))

    return render(request, 'admin_dashboard/proposal_detail.html',
                  _admin_context(
                      'proposals',
                      proposal=proposal,
                      case_studies=case_studies,
                  ))


@admin_required
@require_POST
def proposal_generate(request, proposal_id):
    """(Re)generate the proposal PDF on demand."""
    from clients.models import Proposal
    from clients.proposal_pdf import render_proposal_pdf

    proposal = get_object_or_404(Proposal, id=proposal_id)
    try:
        proposal.pdf_path = render_proposal_pdf(proposal)
        proposal.save(update_fields=['pdf_path', 'updated_at'])
    except Exception as exc:  # noqa: BLE001 — surface on detail page
        return HttpResponse(
            f'PDF generation failed: {exc}', status=500)
    return redirect('admin_dashboard:proposal_detail',
                    proposal_id=proposal.id)


@admin_required
@require_POST
def proposal_send(request, proposal_id):
    """Email the proposal PDF to the prospect via SendGrid."""
    import base64
    import os
    from pathlib import Path

    from clients.models import Proposal

    proposal = get_object_or_404(Proposal, id=proposal_id)
    if not proposal.prospect_email:
        return HttpResponseBadRequest('Prospect email is required.')
    if not proposal.pdf_path:
        return HttpResponseBadRequest(
            'Generate the PDF before sending.')

    abs_path = Path(settings.MEDIA_ROOT) / proposal.pdf_path
    if not abs_path.exists():
        return HttpResponseBadRequest(
            'PDF file is missing — regenerate first.')

    business = (proposal.prospect_business
                or proposal.prospect_name or 'your project')
    subject = f'Website Proposal — {business}'

    view_url = proposal.get_tracking_url()
    html_content = (
        f"<p>Hi {proposal.prospect_name.split()[0] if proposal.prospect_name else 'there'},</p>"
        f"<p>Attached is your proposal for "
        f"<strong>{business}</strong>. You can also view it online:</p>"
        f"<p><a href='{view_url}'>View proposal</a></p>"
        f"<p>It's good for 30 days. Reply to this email or "
        f"call/text 210-896-2536 with any questions.</p>"
        f"<p>— Zachery Long<br>"
        f"Aspired Websites LLC</p>"
    )

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (
            Attachment, Disposition, FileContent, FileName,
            FileType, Mail,
        )
    except ImportError:
        return HttpResponse('SendGrid SDK not installed.', status=500)

    # SDK path — append the legal address footer manually since
    # AspiredEmailBackend doesn't see SendGrid SDK sends.
    from core.email_signature import append_signature
    _, html_content = append_signature(html=html_content)

    message = Mail(
        from_email=settings.DEFAULT_FROM_EMAIL,
        to_emails=proposal.prospect_email,
        subject=subject,
        html_content=html_content,
    )
    with open(abs_path, 'rb') as fh:
        encoded = base64.b64encode(fh.read()).decode()
    ext = os.path.splitext(abs_path)[1] or '.pdf'
    mime = ('application/pdf' if ext.lower() == '.pdf'
            else 'text/html')
    attachment = Attachment(
        FileContent(encoded),
        FileName(f'proposal{ext}'),
        FileType(mime),
        Disposition('attachment'),
    )
    message.attachment = attachment

    try:
        sg = SendGridAPIClient(settings.SENDGRID_API_KEY)
        sg.send(message)
    except Exception as exc:  # noqa: BLE001 — surface to operator
        return HttpResponse(
            f'SendGrid error: {str(exc)[:200]}', status=500)

    proposal.sent_at = timezone.now()
    proposal.status = 'sent'
    if not proposal.expires_at:
        proposal.expires_at = (
            timezone.now() + datetime.timedelta(days=30)).date()
    proposal.save(update_fields=[
        'sent_at', 'status', 'expires_at', 'updated_at',
    ])

    return redirect('admin_dashboard:proposal_detail',
                    proposal_id=proposal.id)


@admin_required
@require_POST
def proposal_set_status(request, proposal_id):
    """Flip status to accepted/declined from the detail page buttons."""
    from clients.models import Proposal

    proposal = get_object_or_404(Proposal, id=proposal_id)
    new_status = (request.POST.get('status') or '').strip()
    valid = {choice for choice, _ in Proposal.STATUS_CHOICES}
    if new_status not in valid:
        return HttpResponseBadRequest('invalid status')
    proposal.status = new_status
    proposal.save(update_fields=['status', 'updated_at'])
    return redirect('admin_dashboard:proposal_detail',
                    proposal_id=proposal.id)


@admin_required
def proposal_lead_autofill(request):
    """HTMX endpoint — fill prospect fields when a Lead is picked."""
    from outreach.models import Lead

    lead_id = (request.GET.get('lead_id') or '').strip()
    if not lead_id:
        return HttpResponse('')
    try:
        lead = Lead.objects.get(pk=int(lead_id))
    except (Lead.DoesNotExist, ValueError):
        return HttpResponse('')

    return render(request, 'admin_dashboard/_proposal_autofill.html',
                  {'lead': lead})


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 2 — Case studies
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def case_studies_list(request):
    """List view of CaseStudy rows."""
    from clients.models import CaseStudy
    case_studies = (CaseStudy.objects
                    .select_related('client')
                    .order_by('-created_at'))
    return render(request, 'admin_dashboard/case_studies_list.html',
                  _admin_context(
                      'case_studies',
                      case_studies=case_studies,
                  ))


@admin_required
def case_study_new(request):
    """Create a new CaseStudy (form + save)."""
    from clients.models import CaseStudy, ClientProfile

    if request.method == 'POST':
        client = None
        cid = (request.POST.get('client_id') or '').strip()
        if cid:
            try:
                client = ClientProfile.objects.get(id=cid)
            except (ClientProfile.DoesNotExist, ValueError):
                client = None

        is_published = request.POST.get('is_published') == 'on'

        cs = CaseStudy.objects.create(
            client=client,
            title=(request.POST.get('title') or '').strip()[:300],
            business_type=(request.POST.get('business_type')
                           or (client.business_type if client else '')
                           or '').strip()[:100],
            location=(request.POST.get('location')
                      or _client_location(client)
                      or '').strip()[:100],
            challenge=(request.POST.get('challenge') or '').strip(),
            solution=(request.POST.get('solution') or '').strip(),
            results=(request.POST.get('results') or '').strip(),
            metric_1_label=(request.POST.get('metric_1_label')
                            or '').strip()[:100],
            metric_1_value=(request.POST.get('metric_1_value')
                            or '').strip()[:50],
            metric_2_label=(request.POST.get('metric_2_label')
                            or '').strip()[:100],
            metric_2_value=(request.POST.get('metric_2_value')
                            or '').strip()[:50],
            metric_3_label=(request.POST.get('metric_3_label')
                            or '').strip()[:100],
            metric_3_value=(request.POST.get('metric_3_value')
                            or '').strip()[:50],
            testimonial_quote=(request.POST.get('testimonial_quote')
                               or '').strip(),
            testimonial_name=(request.POST.get('testimonial_name')
                              or '').strip()[:100],
            is_published=is_published,
            published_at=(timezone.now() if is_published else None),
        )
        return redirect('admin_dashboard:case_study_edit', cs_id=cs.id)

    preselect_client = None
    cid_query = (request.GET.get('client') or '').strip()
    if cid_query:
        try:
            preselect_client = ClientProfile.objects.get(id=cid_query)
        except (ClientProfile.DoesNotExist, ValueError):
            preselect_client = None

    clients = (ClientProfile.objects.filter(is_tester=False)
               .order_by('firm_name'))

    return render(request, 'admin_dashboard/case_study_form.html',
                  _admin_context(
                      'case_studies',
                      clients=clients,
                      case_study=None,
                      preselect_client=preselect_client,
                  ))


@admin_required
def case_study_edit(request, cs_id):
    """Edit an existing CaseStudy."""
    from clients.models import CaseStudy, ClientProfile

    cs = get_object_or_404(CaseStudy, id=cs_id)

    if request.method == 'POST':
        client = cs.client
        cid = (request.POST.get('client_id') or '').strip()
        if cid:
            try:
                client = ClientProfile.objects.get(id=cid)
            except (ClientProfile.DoesNotExist, ValueError):
                pass

        was_published = cs.is_published
        is_published = request.POST.get('is_published') == 'on'

        cs.client = client
        cs.title = (request.POST.get('title') or '').strip()[:300]
        cs.business_type = (request.POST.get('business_type')
                            or '').strip()[:100]
        cs.location = (request.POST.get('location') or '').strip()[:100]
        cs.challenge = (request.POST.get('challenge') or '').strip()
        cs.solution = (request.POST.get('solution') or '').strip()
        cs.results = (request.POST.get('results') or '').strip()
        cs.metric_1_label = (
            request.POST.get('metric_1_label') or '').strip()[:100]
        cs.metric_1_value = (
            request.POST.get('metric_1_value') or '').strip()[:50]
        cs.metric_2_label = (
            request.POST.get('metric_2_label') or '').strip()[:100]
        cs.metric_2_value = (
            request.POST.get('metric_2_value') or '').strip()[:50]
        cs.metric_3_label = (
            request.POST.get('metric_3_label') or '').strip()[:100]
        cs.metric_3_value = (
            request.POST.get('metric_3_value') or '').strip()[:50]
        cs.testimonial_quote = (
            request.POST.get('testimonial_quote') or '').strip()
        cs.testimonial_name = (
            request.POST.get('testimonial_name') or '').strip()[:100]
        cs.is_published = is_published
        if is_published and not was_published:
            cs.published_at = timezone.now()
        cs.save()
        return redirect('admin_dashboard:case_studies_list')

    clients = (ClientProfile.objects.filter(is_tester=False)
               .order_by('firm_name'))
    return render(request, 'admin_dashboard/case_study_form.html',
                  _admin_context(
                      'case_studies',
                      clients=clients,
                      case_study=cs,
                      preselect_client=cs.client,
                  ))


@admin_required
@require_POST
def case_study_toggle_publish(request, cs_id):
    """One-click toggle on the list page."""
    from clients.models import CaseStudy
    cs = get_object_or_404(CaseStudy, id=cs_id)
    cs.is_published = not cs.is_published
    if cs.is_published and not cs.published_at:
        cs.published_at = timezone.now()
    cs.save(update_fields=[
        'is_published', 'published_at', 'updated_at'])
    return redirect('admin_dashboard:case_studies_list')


@admin_required
@require_POST
def case_study_ai_draft(request):
    """
    POST a {client_id, title?} pair, get back a JSON draft of
    challenge / solution / results / 3 metrics. Front-end renders the
    response into the form fields.
    """
    import json

    from clients.models import ClientProfile
    from reporting.ai import (
        AIError, AINotConfigured, claude_complete, MODEL_CONTENT,
    )

    cid = (request.POST.get('client_id') or '').strip()
    if not cid:
        return HttpResponseBadRequest('client_id required')
    try:
        client = ClientProfile.objects.get(id=cid)
    except (ClientProfile.DoesNotExist, ValueError):
        return HttpResponseBadRequest('client not found')

    title_hint = (request.POST.get('title') or '').strip()

    # Post-2026-05-25 refactor: project fields live on ClientProfile.
    # `project` alias preserved so existing reads (project.stage,
    # project.intake, project.stage_logs, etc.) keep working.
    project = client
    package_label = (project.get_package_display()
                     if project and project.package else '')
    intake_summary = ''
    if project and hasattr(project, 'intake'):
        intake = project.intake
        bits = [
            intake.about_copy,
            f'Practice areas: {intake.practice_areas}'
            if intake.practice_areas else '',
            f'Brand colors: {intake.brand_colors}'
            if intake.brand_colors else '',
        ]
        intake_summary = '\n'.join(b for b in bits if b)[:1500]

    location = _client_location(client)

    system = (
        "You are writing a case study for Aspired Websites LLC, a "
        "custom web design agency. Keep it concise and focused on "
        "business impact. Avoid hype and clichés. Return ONLY a JSON "
        "object with keys: challenge, solution, results, "
        "metric_1_label, metric_1_value, metric_2_label, "
        "metric_2_value, metric_3_label, metric_3_value. No prose "
        "outside the JSON."
    )

    user = (
        f"Client: {client.firm_name}\n"
        f"Business type: {client.business_type or 'unspecified'}\n"
        f"Location: {location or 'unspecified'}\n"
        f"Project package: {package_label or 'unspecified'}\n"
        f"Working title: {title_hint or 'not provided'}\n\n"
        f"Available info from their intake:\n{intake_summary or '(none)'}\n\n"
        "Write the case study now. Estimate plausible metrics (e.g. "
        "'40%' increase in inquiries, '2.3x' faster page load) when "
        "exact numbers are unavailable. Three short metric pairs."
    )

    try:
        raw = claude_complete(
            messages=[{'role': 'user', 'content': user}],
            system=system,
            model=MODEL_CONTENT,
            max_tokens=1200,
        )
    except AINotConfigured:
        return HttpResponse(
            'ANTHROPIC_API_KEY not configured.', status=503)
    except AIError as exc:
        return HttpResponse(f'AI draft failed: {exc}', status=502)

    # Defensive JSON parse — strip code fences if Claude adds them.
    stripped = raw.strip()
    if stripped.startswith('```'):
        stripped = stripped.strip('`')
        if stripped.lower().startswith('json'):
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return HttpResponse(
            'AI returned non-JSON. Try again.', status=502)

    from django.http import JsonResponse
    return JsonResponse({
        'challenge': data.get('challenge', ''),
        'solution': data.get('solution', ''),
        'results': data.get('results', ''),
        'metric_1_label': data.get('metric_1_label', ''),
        'metric_1_value': data.get('metric_1_value', ''),
        'metric_2_label': data.get('metric_2_label', ''),
        'metric_2_value': data.get('metric_2_value', ''),
        'metric_3_label': data.get('metric_3_label', ''),
        'metric_3_value': data.get('metric_3_value', ''),
    })


def _client_location(client):
    """City, State string for a client or empty."""
    if not client:
        return ''
    parts = [p for p in (client.city, client.state) if p]
    return ', '.join(parts)


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


def _intel_pending_count():
    """Sidebar badge — admin needs to triage these."""
    try:
        from clients.models import IntelligenceSuggestion
        return IntelligenceSuggestion.objects.filter(
            status='pending_review').count()
    except Exception:
        return 0


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


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 4 — Annual Business Health Report
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def annual_reports_list(request):
    """All annual reports in one table — newest year first per client."""
    from clients.models import AnnualReport
    reports = (
        AnnualReport.objects
        .select_related('client')
        .order_by('-report_year', 'client__firm_name')
    )
    return render(request, 'admin_dashboard/annual_reports_list.html',
                  _admin_context(
                      'annual_reports',
                      reports=reports,
                  ))


@admin_required
def annual_report_detail(request, report_id):
    """Single-report detail + action buttons."""
    from clients.models import AnnualReport
    report = get_object_or_404(AnnualReport, id=report_id)
    return render(request, 'admin_dashboard/annual_report_detail.html',
                  _admin_context(
                      'annual_reports',
                      report=report,
                      data=report.report_data or {},
                  ))


@admin_required
@require_POST
def annual_report_send(request, report_id):
    """Email the PDF to the client via SendGrid."""
    import base64
    import os
    from pathlib import Path

    from clients.models import AnnualReport

    report = get_object_or_404(AnnualReport, id=report_id)
    client = report.client
    if report.status not in ('ready', 'sent'):
        return HttpResponseBadRequest(
            'Report must be in status ready or sent to send.')
    to_email = client.user.email if client.user else ''
    if not to_email:
        return HttpResponseBadRequest(
            'Client has no email on file — cannot send.')
    if not report.pdf_path:
        return HttpResponseBadRequest(
            'Report has no PDF — regenerate first.')

    abs_path = Path(settings.MEDIA_ROOT) / report.pdf_path
    if not abs_path.exists():
        return HttpResponseBadRequest(
            'PDF file is missing — regenerate first.')

    contact_name = (client.contact_name
                    or (client.user.get_full_name() if client.user else '')
                    or 'there')

    first_name = (contact_name or '').split(' ')[0] or 'there'

    text_body = (
        f"Hi {first_name},\n\n"
        f"Your {report.report_year} Annual Business Health Report "
        f"is attached. It covers a full year of website performance, "
        f"security work, and growth.\n\n"
        f"I'd love to schedule a quick call to walk through it "
        f"together. Reply with a couple of times that work for you.\n\n"
        f"— Zachery Long\nAspired Websites LLC\n"
    )

    ext = os.path.splitext(abs_path)[1].lower() or '.pdf'
    mime = 'application/pdf' if ext == '.pdf' else 'text/html'
    with open(abs_path, 'rb') as fh:
        pdf_bytes = fh.read()

    subject = (f'Your {report.report_year} Annual Website '
               f'Performance Report — {client.firm_name}')
    from clients.emails import send_branded
    try:
        send_branded(
            subject=subject,
            template='annual_report',
            context={
                'name': first_name,
                'client_firm': client.firm_name,
                'report_year': report.report_year,
                'preheader': (
                    f'A full year of performance, security, and growth.'),
            },
            recipient_list=[to_email],
            text_body=text_body,
            from_email=getattr(settings, 'EMAIL_FROM_MAIN',
                               settings.DEFAULT_FROM_EMAIL),
            attachments=[
                (f'annual-report-{report.report_year}{ext}',
                 pdf_bytes, mime)],
            fail_silently=False,
        )
    except Exception as exc:  # noqa: BLE001
        return HttpResponse(f'Email send failed: {str(exc)[:200]}',
                            status=500)

    report.status = 'sent'
    report.sent_at = timezone.now()
    report.save(update_fields=['status', 'sent_at', 'updated_at'])

    return redirect('admin_dashboard:annual_report_detail',
                    report_id=report.id)


@admin_required
@require_POST
def annual_report_regenerate(request, report_id):
    """
    Force a fresh generation pass — flips the row back to
    `generating` (so the idempotency guard in the task doesn't
    short-circuit) and queues the Celery task.
    """
    from clients.models import AnnualReport
    from clients.tasks import generate_annual_report

    report = get_object_or_404(AnnualReport, id=report_id)
    report.status = 'generating'
    report.save(update_fields=['status', 'updated_at'])
    generate_annual_report.apply_async(
        args=[str(report.client.id), report.report_year])
    return redirect('admin_dashboard:annual_report_detail',
                    report_id=report.id)


@admin_required
def annual_report_generate(request):
    """
    Manual on-demand generation — admin picks a client + year,
    we queue the Celery task and bounce to the detail page.
    """
    from clients.models import AnnualReport, ClientProfile
    from clients.tasks import generate_annual_report

    if request.method == 'POST':
        cid = (request.POST.get('client_id') or '').strip()
        year_raw = (request.POST.get('report_year') or '').strip()
        if not cid:
            return HttpResponseBadRequest('client_id required.')
        try:
            year = int(year_raw)
        except ValueError:
            return HttpResponseBadRequest(
                'report_year must be an integer.')
        client = get_object_or_404(ClientProfile, id=cid)

        report, _ = AnnualReport.objects.get_or_create(
            client=client, report_year=year,
            defaults={'status': 'generating'},
        )
        report.status = 'generating'
        report.save(update_fields=['status', 'updated_at'])
        generate_annual_report.apply_async(
            args=[str(client.id), year])
        return redirect(
            'admin_dashboard:annual_report_detail',
            report_id=report.id)

    clients = (ClientProfile.objects.filter(is_tester=False)
               .order_by('firm_name'))
    return render(request, 'admin_dashboard/annual_report_generate.html',
                  _admin_context(
                      'annual_reports',
                      clients=clients,
                      default_year=(timezone.now().year - 1),
                  ))


@admin_required
def annual_report_download(request, report_id):
    """Serve the PDF (or .html fallback) inline."""
    from pathlib import Path

    from django.http import FileResponse, Http404

    from clients.models import AnnualReport

    report = get_object_or_404(AnnualReport, id=report_id)
    if not report.pdf_path:
        raise Http404('Report has no PDF yet.')
    abs_path = Path(settings.MEDIA_ROOT) / report.pdf_path
    if not abs_path.exists():
        raise Http404('PDF file missing.')
    content_type = ('application/pdf' if abs_path.suffix.lower() == '.pdf'
                    else 'text/html')
    return FileResponse(open(abs_path, 'rb'),
                        content_type=content_type)


# ────────────────────────────────────────────────────────────────────────────
# Phase 7 Part 5 — Competitor Content Gap Tracker
# ────────────────────────────────────────────────────────────────────────────

_COMPETITOR_LIMIT = 3


def _high_priority_gaps_count():
    """Sidebar badge — un-actioned high-priority gaps."""
    try:
        from clients.models import CompetitorGapReport
        from django.db.models import Sum
        return (CompetitorGapReport.objects
                .filter(status='complete')
                .aggregate(s=Sum('high_priority_gaps'))['s'] or 0)
    except Exception:
        return 0


# ── Competitor CRUD (HTMX on the client detail page) ──────────────────────

def _competitors_fragment(request, client):
    """Render the competitors box that HTMX swaps in/out."""
    from clients.models import ClientCompetitor
    competitors = list(client.competitors.all()[:_COMPETITOR_LIMIT])
    return render(
        request, 'admin_dashboard/_competitors_box.html',
        {
            'client': client,
            'competitors': competitors,
            'can_add': len(competitors) < _COMPETITOR_LIMIT,
            'competitor_limit': _COMPETITOR_LIMIT,
        },
    )


@admin_required
def competitor_add(request, client_id):
    """
    Add a competitor for `client_id`. POST adds + returns the
    refreshed competitors box (HTMX); GET returns the inline form
    fragment.
    """
    from clients.models import ClientCompetitor, ClientProfile

    client = get_object_or_404(ClientProfile, id=client_id)

    if request.method == 'POST':
        existing = client.competitors.count()
        if existing >= _COMPETITOR_LIMIT:
            return HttpResponseBadRequest(
                f'Max {_COMPETITOR_LIMIT} competitors per client.')
        name = (request.POST.get('name') or '').strip()[:200]
        domain = (request.POST.get('domain') or '').strip()[:200]
        notes = (request.POST.get('notes') or '').strip()[:300]
        if not name or not domain:
            return HttpResponseBadRequest('name + domain required.')
        if not domain.startswith(('http://', 'https://')):
            domain = f'https://{domain}'
        if client.competitors.filter(domain=domain).exists():
            return HttpResponseBadRequest(
                'That domain is already tracked for this client.')
        ClientCompetitor.objects.create(
            client=client, name=name, domain=domain, notes=notes,
            sort_order=existing,
        )
        return _competitors_fragment(request, client)

    return render(
        request, 'admin_dashboard/_competitor_form.html',
        {'client': client, 'competitor': None,
         'form_url': reverse(
             'admin_dashboard:competitor_add', args=[client.id])},
    )


@admin_required
def competitor_edit(request, client_id, comp_id):
    """Inline edit; same HTMX contract as add."""
    from clients.models import ClientCompetitor, ClientProfile

    client = get_object_or_404(ClientProfile, id=client_id)
    comp = get_object_or_404(
        ClientCompetitor, id=comp_id, client=client)

    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()[:200]
        domain = (request.POST.get('domain') or '').strip()[:200]
        notes = (request.POST.get('notes') or '').strip()[:300]
        if not name or not domain:
            return HttpResponseBadRequest('name + domain required.')
        if not domain.startswith(('http://', 'https://')):
            domain = f'https://{domain}'
        # Allow same domain on self; reject if a *different* row
        # already uses it.
        if client.competitors.filter(domain=domain).exclude(
                id=comp.id).exists():
            return HttpResponseBadRequest(
                'Another competitor already uses that domain.')
        comp.name = name
        comp.domain = domain
        comp.notes = notes
        comp.save(update_fields=['name', 'domain', 'notes',
                                 'updated_at'])
        return _competitors_fragment(request, client)

    return render(
        request, 'admin_dashboard/_competitor_form.html',
        {'client': client, 'competitor': comp,
         'form_url': reverse(
             'admin_dashboard:competitor_edit',
             args=[client.id, comp.id])},
    )


@admin_required
@require_POST
def competitor_delete(request, client_id, comp_id):
    """Drop a competitor; return the refreshed box."""
    from clients.models import ClientCompetitor, ClientProfile

    client = get_object_or_404(ClientProfile, id=client_id)
    comp = get_object_or_404(
        ClientCompetitor, id=comp_id, client=client)
    comp.delete()
    return _competitors_fragment(request, client)


# ── Competitor gap reports list + detail ───────────────────────────────────

@admin_required
def competitor_gaps_list(request):
    """All gap reports + 4 summary cards."""
    from clients.models import (
        ClientProfile, CompetitorGapReport,
    )
    from django.db.models import Sum

    qs = (CompetitorGapReport.objects
          .select_related('client')
          .order_by('-report_month', 'client__firm_name'))

    client_filter = (request.GET.get('client') or '').strip()
    status_filter = (request.GET.get('status') or '').strip()
    month_filter = (request.GET.get('month') or '').strip()

    if client_filter:
        try:
            qs = qs.filter(client_id=client_filter)
        except (ValueError, TypeError):
            pass
    if status_filter and status_filter != 'all':
        qs = qs.filter(status=status_filter)
    if month_filter:
        try:
            y, m = month_filter.split('-')
            qs = qs.filter(report_month__year=int(y),
                           report_month__month=int(m))
        except (ValueError, AttributeError):
            pass

    base = CompetitorGapReport.objects.all()
    summary = {
        'total_reports': base.count(),
        'high_priority': (base.aggregate(
            s=Sum('high_priority_gaps'))['s'] or 0),
        'with_competitors': (
            ClientProfile.objects
            .filter(competitors__isnull=False,
                    is_tester=False, status='active')
            .distinct().count()),
        'without_competitors': (
            ClientProfile.objects
            .filter(competitors__isnull=True,
                    is_tester=False, status='active')
            .distinct().count()),
    }

    clients = (ClientProfile.objects
               .filter(competitor_gap_reports__isnull=False)
               .distinct().order_by('firm_name'))

    return render(
        request, 'admin_dashboard/competitor_gaps_list.html',
        _admin_context(
            'competitor_gaps',
            reports=qs,
            summary=summary,
            clients=clients,
            filter_client=client_filter,
            filter_status=status_filter,
            filter_month=month_filter,
            status_choices=CompetitorGapReport.STATUS_CHOICES,
        ),
    )


@admin_required
def competitor_gap_detail(request, report_id):
    """Single-report detail page."""
    from clients.models import CompetitorGapReport
    report = get_object_or_404(CompetitorGapReport, id=report_id)

    # Index gaps so the create-suggestion button has a stable handle.
    gaps_indexed = list(enumerate(report.gaps or []))

    # Sort high → medium → low → unknown.
    _PRIORITY = {'high': 0, 'medium': 1, 'low': 2}
    gaps_indexed.sort(
        key=lambda pair: _PRIORITY.get(
            (pair[1].get('priority') or '').lower(), 3))

    return render(
        request, 'admin_dashboard/competitor_gap_detail.html',
        _admin_context(
            'competitor_gaps',
            report=report,
            gaps_indexed=gaps_indexed,
        ),
    )


@admin_required
@require_POST
def competitor_gap_run_now(request, client_id):
    """"Run Analysis Now" — fires the Celery task async."""
    from clients.models import ClientProfile
    from clients.tasks import run_competitor_gap_analysis

    client = get_object_or_404(ClientProfile, id=client_id)
    run_competitor_gap_analysis.apply_async(args=[str(client.id)])

    if request.headers.get('HX-Request') == 'true':
        return HttpResponse(
            '<div class="banner banner--info">'
            'Analysis queued — usually under a minute. '
            'Refresh to see the report.'
            '</div>')
    return redirect('admin_dashboard:competitor_gaps_list')


@admin_required
@require_POST
def gap_create_suggestion(request, report_id, gap_index):
    """
    Convert a single gap → IntelligenceSuggestion(pending_review).
    Idempotent on (report, gap_index) via a marker stamped into the
    gap dict so the operator can't accidentally create two.
    """
    from clients.models import (
        CompetitorGapReport, IntelligenceSuggestion,
    )

    report = get_object_or_404(CompetitorGapReport, id=report_id)
    gaps = list(report.gaps or [])
    if gap_index < 0 or gap_index >= len(gaps):
        return HttpResponseBadRequest('gap_index out of range.')

    gap = gaps[gap_index]
    if gap.get('suggestion_id'):
        # Already converted — give them a link to the existing row.
        return redirect(
            'admin_dashboard:intelligence_suggestion_detail',
            suggestion_id=gap['suggestion_id'])

    competitors_str = ', '.join(
        gap.get('competitors_with_this') or [])
    expected = (
        f'Targeting this gap could help '
        f'{report.client.firm_name} compete with '
        f'{competitors_str}'
        f' who already cover this topic.'
        if competitors_str
        else (
            f'Targeting this gap could help '
            f'{report.client.firm_name} attract searches that '
            f'currently land on competitor sites.'
        )
    )

    suggestion = IntelligenceSuggestion.objects.create(
        client=report.client,
        suggestion_type='competitor',
        title=(gap.get('suggested_page_title')
               or gap.get('title') or 'Competitor gap')[:300],
        description=gap.get('description', '') or '',
        expected_impact=expected,
        implementation_notes=(
            gap.get('suggested_action', '') or ''),
        one_time_fee=500,
        is_in_maintenance_scope=False,
        data_sources=['competitor_gaps'],
        ai_reasoning=json.dumps(gap, default=str),
        status='pending_review',
    )

    # Stamp the gap so we don't double-create.
    gaps[gap_index] = {**gap,
                       'suggestion_id': str(suggestion.id)}
    report.gaps = gaps
    report.save(update_fields=['gaps', 'updated_at'])

    return redirect(
        'admin_dashboard:intelligence_suggestion_detail',
        suggestion_id=suggestion.id)


# ────────────────────────────────────────────────────────────────────────────
# Lead delete — single + bulk (Phase 7 round 2)
# ────────────────────────────────────────────────────────────────────────────

@admin_required
@require_POST
def lead_delete(request, pk):
    """
    Delete a single Lead. Cascades clean up LeadNote, EmailSent,
    EmailReply (all FK on_delete=CASCADE) and SET_NULL drops the
    Lead pointer on referral_events.
    """
    from django.contrib import messages as _msg
    lead = get_object_or_404(Lead, pk=pk)
    firm = lead.firm_name
    lead.delete()
    _msg.success(request, f'Deleted lead: {firm}')
    return redirect('admin_dashboard:leads_table')


@admin_required
@require_POST
def lead_bulk_delete(request):
    """
    Delete every Lead whose pk is in POST.getlist('lead_ids').
    Confirmation happens client-side; the form action requires POST
    so CSRF protection covers it.
    """
    from django.contrib import messages as _msg
    raw_ids = request.POST.getlist('lead_ids')
    ids = []
    for r in raw_ids:
        try:
            ids.append(int(r))
        except (TypeError, ValueError):
            continue
    if not ids:
        _msg.warning(request, 'No leads selected for deletion.')
        return redirect('admin_dashboard:leads_table')

    qs = Lead.objects.filter(pk__in=ids)
    n = qs.count()
    qs.delete()
    _msg.success(request, f'Deleted {n} lead{"" if n == 1 else "s"}.')
    return redirect('admin_dashboard:leads_table')


# ────────────────────────────────────────────────────────────────────────────
# Tier 2 — Session recording (rrweb) admin views
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def recordings_list(request, client_id):
    """
    Per-client recordings table with storage stats + filters.

    Filters (all optional, all GET): page, min_duration, q.
    """
    from django.db.models import Avg, Count, Sum
    from django.utils import timezone

    from clients.models import ClientProfile
    from reporting.models import SessionRecording

    client = get_object_or_404(ClientProfile, id=client_id)

    qs = (SessionRecording.objects
          .filter(client=client).order_by('-created_at'))

    # Filters.
    page_filter = (request.GET.get('page') or '').strip()
    min_dur = (request.GET.get('min_duration') or '').strip()
    if page_filter:
        qs = qs.filter(page_url__icontains=page_filter)
    if min_dur:
        try:
            qs = qs.filter(duration_seconds__gte=int(min_dur))
        except (TypeError, ValueError):
            pass

    # Storage stats — never filtered, always full picture.
    stats = SessionRecording.objects.filter(client=client).aggregate(
        total_recordings=Count('id'),
        total_size_kb=Sum('estimated_size_kb'),
        avg_duration=Avg('duration_seconds'),
    )
    total_size_kb = stats['total_size_kb'] or 0
    total_size_mb = round(total_size_kb / 1024, 1)

    oldest = (SessionRecording.objects
              .filter(client=client)
              .order_by('created_at').first())
    oldest_days = (timezone.now() - oldest.created_at).days if oldest else 0

    # Distinct page URLs for the dropdown filter.
    pages_seen = list(
        SessionRecording.objects.filter(client=client)
        .values_list('page_url', flat=True)
        .distinct().order_by('page_url')[:30])

    return render(
        request,
        'admin_dashboard/recordings_list.html',
        _admin_context(
            'clients',
            client=client,
            recordings=qs[:200],
            total_recordings=stats['total_recordings'] or 0,
            total_size_mb=total_size_mb,
            oldest_days=oldest_days,
            pages_seen=pages_seen,
            filter_page=page_filter,
            filter_min_duration=min_dur,
        ),
    )


@admin_required
def recording_replay(request, client_id, rec_id):
    """
    Full-page rrweb Replayer view. Events are inlined via
    `{{ events_json|json_script:"recording-events" }}` so the
    payload is automatically HTML-escaped inside a typed
    <script type="application/json"> tag (XSS-safe). The replay
    JS parses that with JSON.parse — no |safe filter needed.
    """
    from django.core.serializers.json import DjangoJSONEncoder

    from clients.models import ClientProfile
    from reporting.models import SessionRecording

    client = get_object_or_404(ClientProfile, id=client_id)
    rec = get_object_or_404(SessionRecording, id=rec_id, client=client)
    events = rec.get_all_events()

    # Lightweight diagnostics for the operator — surface whether
    # the recording will actually replay before they click Play.
    # rrweb event types: 0=DomContentLoaded, 1=Load, 2=FullSnapshot,
    # 3=IncrementalSnapshot, 4=Meta, 5=Custom.
    first_event_type = (events[0].get('type')
                        if events and isinstance(events[0], dict)
                        else None)
    has_full_snapshot = any(
        isinstance(e, dict) and e.get('type') == 2 for e in events)

    return render(
        request,
        'admin_dashboard/recording_replay.html',
        _admin_context(
            'clients',
            client=client,
            recording=rec,
            # DjangoJSONEncoder handles datetime/UUID/Decimal cleanly
            # if any sneak into the rrweb chunks via custom plugins.
            events_json=json.dumps(events, cls=DjangoJSONEncoder),
            event_count=len(events),
            first_event_type=first_event_type,
            has_full_snapshot=has_full_snapshot,
        ),
    )


@admin_required
def recording_download(request, client_id, rec_id):
    """
    Stream a self-contained HTML file — rrweb-player CSS + JS +
    the recording events all inlined. Recipient just opens it in
    any browser, no server required.
    """
    from pathlib import Path

    from django.http import HttpResponse

    from clients.models import ClientProfile
    from reporting.models import SessionRecording

    client = get_object_or_404(ClientProfile, id=client_id)
    rec = get_object_or_404(SessionRecording, id=rec_id, client=client)

    static_root = Path(settings.BASE_DIR) / 'core' / 'static' / 'js'
    try:
        rrweb_js = (static_root / 'rrweb.min.js').read_text(
            encoding='utf-8')
    except OSError:
        rrweb_js = ''

    events = rec.get_all_events()
    events_json = json.dumps(events, default=str)

    safe_page = (rec.page_url or '').replace(
        'https://', '').replace('http://', '').replace('/', '_')[:60]
    safe_page = safe_page or 'page'
    filename = (f'recording-{rec.created_at:%Y%m%d-%H%M}-'
                f'{safe_page}.html')

    html = render(
        request,
        'admin_dashboard/recording_download.html',
        {
            'client': client,
            'recording': rec,
            'rrweb_js': rrweb_js,
            'events_json': events_json,
        },
    ).content

    response = HttpResponse(html, content_type='text/html')
    response['Content-Disposition'] = (
        f'attachment; filename="{filename}"')
    return response


@admin_required
@require_POST
def recording_delete(request, client_id, rec_id):
    """Single-row delete from the recordings list."""
    from django.contrib import messages as _msg

    from clients.models import ClientProfile
    from reporting.models import SessionRecording

    client = get_object_or_404(ClientProfile, id=client_id)
    rec = get_object_or_404(SessionRecording, id=rec_id, client=client)
    rec.delete()
    _msg.success(request, 'Recording deleted.')
    return redirect('admin_dashboard:recordings_list',
                    client_id=client.id)


@admin_required
@require_POST
def recording_delete_all(request, client_id):
    """Wipe every recording for one client (with confirmation in template)."""
    from django.contrib import messages as _msg

    from clients.models import ClientProfile
    from reporting.models import SessionRecording

    client = get_object_or_404(ClientProfile, id=client_id)
    n, _ = SessionRecording.objects.filter(client=client).delete()
    _msg.success(request, f'Deleted {n} recording(s).')
    return redirect('admin_dashboard:recordings_list',
                    client_id=client.id)


# ────────────────────────────────────────────────────────────────────────────
# Billing — admin-created onboarding invoices (Part 2)
# ────────────────────────────────────────────────────────────────────────────


def _billing_packages():
    """Build options for the new-invoice form: website-build tiers + Custom."""
    from billing.pricing_models import ServiceTier
    tiers = list(ServiceTier.objects.filter(
        category='website_build', is_active=True
    ).order_by('sort_order', 'price'))
    return tiers


def _billing_maintenance_plans():
    """Optional first-month maintenance line for the onboarding invoice."""
    from billing.pricing_models import ServiceTier
    return list(ServiceTier.objects.filter(
        category='maintenance', is_active=True
    ).order_by('sort_order', 'price'))


def _billing_hosting():
    """The single hosting line ($150/yr)."""
    from billing.pricing_models import ServiceTier
    return ServiceTier.objects.filter(
        category='hosting', is_active=True
    ).order_by('price').first()


@admin_required
def billing_list(request):
    """List every OnboardingInvoice + its onboarding state."""
    from clients.models import OnboardingInvoice
    qs = (
        OnboardingInvoice.objects
        .select_related('client', 'client__user', 'client__onboarding_token')
        .order_by('-created_at')
    )
    return render(
        request,
        'admin_dashboard/billing_list.html',
        _admin_context(active='billing', invoices=qs),
    )


@admin_required
def new_invoice(request):
    """Create a Stripe customer + invoice + client shell + setup token."""
    from decimal import Decimal, InvalidOperation

    from django.contrib import messages as _msg
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from decimal import Decimal as _Decimal

    from billing.stripe_helpers import (
        StripeNotConfigured, create_onboarding_payment_intent,
    )
    from clients.emails import send_invoice_email
    from clients.models import (
        ClientProfile, OnboardingInvoice, OnboardingToken,
    )

    packages = _billing_packages()
    maintenance_plans = _billing_maintenance_plans()
    hosting_tier = _billing_hosting()

    if request.method == 'POST':
        from core.phone_utils import normalize_phone

        first = (request.POST.get('first_name') or '').strip()
        last = (request.POST.get('last_name') or '').strip()
        firm_name = (request.POST.get('firm_name') or '').strip()
        email = (request.POST.get('email') or '').strip().lower()
        phone = normalize_phone(request.POST.get('phone') or '')
        city = (request.POST.get('city') or '').strip()
        state = (request.POST.get('state') or '').strip()
        package_slug = (request.POST.get('package') or '').strip()
        custom_amount_raw = (request.POST.get('custom_amount') or '').strip()
        maintenance_slug = (
            request.POST.get('maintenance_plan') or '').strip()
        add_hosting = bool(request.POST.get('add_hosting'))
        notes = (request.POST.get('internal_notes') or '').strip()

        errors = []
        if not email or '@' not in email:
            errors.append('A valid email is required.')
        if not package_slug:
            errors.append('Please choose a package.')

        # ── Resolve project line ──
        project_amount = None
        project_label = ''
        package_db_slug = ''  # for ClientProfile.package
        if package_slug == 'custom':
            try:
                project_amount = Decimal(custom_amount_raw)
                if project_amount <= 0:
                    raise InvalidOperation()
            except (InvalidOperation, ValueError):
                errors.append(
                    'Custom amount must be a positive number.')
            project_label = 'Custom website build'
        elif package_slug:
            tier = next((t for t in packages
                         if t.slug == package_slug), None)
            if tier is None:
                errors.append('Unknown package selected.')
            else:
                project_amount = tier.price
                project_label = tier.name
                # Map ServiceTier slug → ClientProfile.PACKAGE_CHOICES.
                package_db_slug = (
                    'essential_build' if 'essential' in tier.slug
                    else 'premium_build' if 'premium' in tier.slug
                    else '')

        # ── Optional maintenance + hosting lines ──
        line_items = []
        maintenance_tier = None
        if maintenance_slug:
            maintenance_tier = next(
                (t for t in maintenance_plans
                 if t.slug == maintenance_slug), None)
            if maintenance_tier is None:
                errors.append('Unknown maintenance plan selected.')

        if errors:
            for e in errors:
                _msg.error(request, e)
            return render(
                request,
                'admin_dashboard/billing_new_invoice.html',
                _admin_context(
                    active='billing',
                    packages=packages,
                    maintenance_plans=maintenance_plans,
                    hosting_tier=hosting_tier,
                    form_data=request.POST,
                ),
            )

        # Build the line items for Stripe — descriptions are what the
        # client sees on the hosted invoice.
        line_items.append({
            'description': project_label,
            'amount': project_amount,
        })
        if maintenance_tier:
            line_items.append({
                'description': (
                    f'{maintenance_tier.name} — first month'),
                'amount': maintenance_tier.price,
            })
        if add_hosting and hosting_tier:
            line_items.append({
                'description': (
                    f'{hosting_tier.name} (annual)'),
                'amount': hosting_tier.price,
            })

        User = get_user_model()

        # Total — used both to create the PaymentIntent + the
        # OnboardingInvoice snapshot.
        total = sum((_Decimal(item['amount']) for item in line_items),
                    _Decimal('0'))

        # ── Single transaction; Stripe is called inside so a Stripe
        #    failure rolls back the half-built client. ──
        try:
            with transaction.atomic():
                # Inactive user — activated when they consume the
                # setup token after payment.
                user, _created = User.objects.get_or_create(
                    username=email,
                    defaults={
                        'email': email,
                        'first_name': first,
                        'last_name': last,
                        'is_active': False,
                    },
                )
                user.set_unusable_password()
                if not user.email:
                    user.email = email
                user.save()

                display_name = (
                    firm_name or f'{first} {last}'.strip()
                    or email.split('@')[0])

                profile = ClientProfile.objects.create(
                    user=user,
                    firm_name=display_name,
                    contact_name=f'{first} {last}'.strip(),
                    phone=phone,
                    city=city,
                    state=state,
                    package=package_db_slug,
                    status='active',
                    onboarding_status='pending_setup',
                    onboarding_complete=False,
                    maintenance_active=False,
                    internal_notes=notes,
                )

                # OnboardingInvoice row (snapshot of what's being
                # billed — line items render on our /pay/ page and
                # on the PDF receipt).
                invoice = OnboardingInvoice.objects.create(
                    client=profile,
                    line_items=[
                        {'description': it['description'],
                         'amount': str(it['amount'])}
                        for it in line_items
                    ],
                    total_amount=total,
                    status='draft',
                )

                # PaymentIntent (card-only, no Stripe receipt — our
                # own branded receipt fires from the webhook).
                customer, payment_intent = (
                    create_onboarding_payment_intent(
                        email=email,
                        name=display_name,
                        line_items=line_items,
                        client_profile_id=profile.id,
                        invoice_id=invoice.id,
                    ))
                profile.stripe_customer_id = customer.id
                profile.save(update_fields=[
                    'stripe_customer_id', 'updated_at'])

                invoice.stripe_payment_intent_id = payment_intent.id
                invoice.stripe_client_secret = (
                    payment_intent.client_secret or '')
                invoice.status = 'sent'
                invoice.sent_at = timezone.now()
                invoice.save(update_fields=[
                    'stripe_payment_intent_id',
                    'stripe_client_secret',
                    'status', 'sent_at', 'updated_at',
                ])

                # OnboardingToken is created up-front so the setup
                # link is ready the moment payment.intent.succeeded
                # webhook fires.
                OnboardingToken.objects.create(client=profile)
        except StripeNotConfigured:
            _msg.error(
                request,
                'Stripe is not configured (STRIPE_SECRET_KEY missing). '
                'Invoice not created.')
            return redirect('admin_dashboard:billing_list')
        except Exception as exc:  # noqa: BLE001
            _msg.error(
                request,
                f'Stripe rejected the request: {exc}. '
                'Nothing was saved.')
            return redirect('admin_dashboard:new_invoice')

        # Send the branded invoice email — points to our /pay/<token>/
        # page. NO setup link yet — that's sent post-payment.
        try:
            send_invoice_email(invoice)
        except Exception:
            logger.exception(
                'Invoice email send failed for %s', profile.pk)

        _msg.success(
            request,
            f'Invoice created and sent to {email}. '
            f'Pay URL: {invoice.get_pay_url()}')
        return redirect(
            'admin_dashboard:invoice_detail',
            invoice_id=profile.id)

    return render(
        request,
        'admin_dashboard/billing_new_invoice.html',
        _admin_context(
            active='billing',
            packages=packages,
            maintenance_plans=maintenance_plans,
            hosting_tier=hosting_tier,
            form_data={},
        ),
    )


@admin_required
def invoice_detail(request, invoice_id):
    """Per-invoice admin page: status, onboarding state, resend actions."""
    from clients.models import (
        ClientProfile, OnboardingInvoice, OnboardingToken,
    )
    profile = get_object_or_404(
        ClientProfile.objects.select_related('user'), id=invoice_id)
    token = OnboardingToken.objects.filter(client=profile).first()
    invoice = OnboardingInvoice.objects.filter(client=profile).first()

    return render(
        request,
        'admin_dashboard/billing_invoice_detail.html',
        _admin_context(
            active='billing',
            profile=profile,
            token=token,
            invoice=invoice,
        ),
    )


@admin_required
@require_POST
def invoice_resend_setup(request, invoice_id):
    """Resend the account-setup link email."""
    from django.contrib import messages as _msg

    from clients.emails import send_onboarding_setup_email
    from clients.models import ClientProfile

    profile = get_object_or_404(ClientProfile, id=invoice_id)
    token = getattr(profile, 'onboarding_token', None)
    if token is None:
        _msg.error(request, 'No onboarding token on file.')
    elif token.used:
        _msg.warning(
            request, 'Setup link has already been used.')
    else:
        try:
            send_onboarding_setup_email(profile, token)
            _msg.success(request, 'Setup link resent.')
        except Exception as exc:  # noqa: BLE001
            _msg.error(request, f'Could not send: {exc}')
    return redirect(
        'admin_dashboard:invoice_detail', invoice_id=profile.id)


@admin_required
@require_POST
def invoice_resend(request, invoice_id):
    """Resend the branded invoice email — points to our /pay/ page."""
    from django.contrib import messages as _msg

    from clients.emails import send_invoice_email
    from clients.models import ClientProfile, OnboardingInvoice

    profile = get_object_or_404(ClientProfile, id=invoice_id)
    invoice = OnboardingInvoice.objects.filter(client=profile).first()
    if invoice is None:
        _msg.error(request, 'No invoice on file for this client.')
        return redirect(
            'admin_dashboard:invoice_detail', invoice_id=profile.id)
    if invoice.status == 'paid':
        _msg.warning(
            request, 'Invoice is already paid — nothing to resend.')
        return redirect(
            'admin_dashboard:invoice_detail', invoice_id=profile.id)
    try:
        send_invoice_email(invoice)
        _msg.success(request, 'Invoice email resent.')
    except Exception as exc:  # noqa: BLE001
        _msg.error(request, f'Email send failed: {exc}')
    return redirect(
        'admin_dashboard:invoice_detail', invoice_id=profile.id)


@admin_required
@require_POST
def invoice_send_intake_reminder(request, invoice_id):
    """One-click intake reminder from the client detail / invoice page."""
    from django.contrib import messages as _msg
    from django.utils import timezone

    from clients.models import ClientProfile
    from clients.tasks import _send_intake_reminder

    profile = get_object_or_404(ClientProfile, id=invoice_id)
    token = getattr(profile, 'onboarding_token', None)
    if profile.onboarding_status != 'pending_intake' or token is None:
        _msg.warning(
            request,
            'Client is not in the pending-intake state — '
            'no reminder sent.')
        return redirect(
            'admin_dashboard:invoice_detail', invoice_id=profile.id)
    try:
        _send_intake_reminder(profile, token)
        token.intake_reminders_sent += 1
        token.last_intake_reminder_at = timezone.now()
        token.save(update_fields=[
            'intake_reminders_sent',
            'last_intake_reminder_at',
            'updated_at',
        ])
        _msg.success(request, 'Intake reminder sent.')
    except Exception as exc:  # noqa: BLE001
        _msg.error(request, f'Could not send: {exc}')
    return redirect(
        'admin_dashboard:invoice_detail', invoice_id=profile.id)


@admin_required
@require_POST
def client_change_stage(request, client_id):
    """
    Move a client's active project to a new stage. Triggered from the
    Project Progress section on the admin client detail page.

    Side effects:
      - Updates Project.stage + updated_at
      - Logs to ProjectStageLog (immutable audit trail)
      - Sends the branded stage-change email to the client (unless the
        new stage has no copy in _STAGE_COPY — e.g. 'intake' which is
        the default starting state and doesn't need a notification)
    """
    from django.contrib import messages as _msg

    from clients.emails import send_stage_change_email
    from clients.models import (
        ClientProfile, PROJECT_STAGES, ProjectStageLog,
    )

    profile = get_object_or_404(ClientProfile, id=client_id)

    valid = {key for key, _ in PROJECT_STAGES}
    new_stage = (request.POST.get('stage') or '').strip()
    if new_stage not in valid:
        _msg.error(request, f'Unknown stage: {new_stage}')
        return redirect(
            'admin_dashboard:client_detail', client_id=profile.id)

    if new_stage == profile.stage:
        _msg.info(request, 'Stage unchanged.')
        return redirect(
            'admin_dashboard:client_detail', client_id=profile.id)

    from_stage = profile.stage
    profile.stage = new_stage
    profile.save(update_fields=['stage', 'updated_at'])

    note = (request.POST.get('note') or '').strip()
    setter = (request.user.get_full_name()
              or request.user.username
              or 'admin')

    log = ProjectStageLog.objects.create(
        client=profile,
        from_stage=from_stage,
        to_stage=new_stage,
        note=note,
        set_by=setter,
        client_notified=False,
    )

    # Best-effort — email failure should not block the stage save.
    notify_ok = False
    try:
        send_stage_change_email(profile, new_stage)
        notify_ok = True
    except Exception:
        logger.exception(
            'stage-change email failed for %s', profile.pk)

    if notify_ok:
        log.client_notified = True
        log.notification_sent_at = timezone.now()
        log.save(update_fields=[
            'client_notified', 'notification_sent_at', 'updated_at'])

    label = dict(PROJECT_STAGES).get(new_stage, new_stage)
    _msg.success(
        request,
        f'Project moved to "{label}".'
        + (' Client emailed.' if notify_ok else
           ' (Client email skipped or failed.)'))
    return redirect(
        'admin_dashboard:client_detail', client_id=profile.id)


@admin_required
def send_onboarding(request):
    """
    SKIP-INVOICE onboarding flow — create a client + immediately mark
    the invoice paid + email the setup link.

    Useful for clients who paid offline, comped clients, or anyone who
    shouldn't see the pay-this-invoice gate. Behind the scenes we still
    mint a zero-amount, status=paid OnboardingInvoice so the downstream
    gate logic is satisfied uniformly.
    """
    from decimal import Decimal as _Decimal

    from django.contrib import messages as _msg
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from clients.emails import send_onboarding_setup_email
    from clients.models import (
        ClientProfile, OnboardingInvoice, OnboardingToken,
    )

    if request.method == 'POST':
        from core.phone_utils import normalize_phone

        first = (request.POST.get('first_name') or '').strip()
        last = (request.POST.get('last_name') or '').strip()
        firm_name = (request.POST.get('firm_name') or '').strip()
        email = (request.POST.get('email') or '').strip().lower()
        phone = normalize_phone(request.POST.get('phone') or '')
        city = (request.POST.get('city') or '').strip()
        state = (request.POST.get('state') or '').strip()
        notes = (request.POST.get('internal_notes') or '').strip()

        errors = []
        if not email or '@' not in email:
            errors.append('A valid email is required.')
        if not firm_name and not (first or last):
            errors.append(
                'Enter a firm name or at least a first/last name.')

        if errors:
            for e in errors:
                _msg.error(request, e)
            return render(
                request,
                'admin_dashboard/billing_send_onboarding.html',
                _admin_context(
                    active='billing', form_data=request.POST),
            )

        User = get_user_model()
        try:
            with transaction.atomic():
                user, _created = User.objects.get_or_create(
                    username=email,
                    defaults={
                        'email': email,
                        'first_name': first,
                        'last_name': last,
                        'is_active': False,
                    },
                )
                user.set_unusable_password()
                if not user.email:
                    user.email = email
                user.save()

                display_name = (
                    firm_name or f'{first} {last}'.strip()
                    or email.split('@')[0])

                profile = ClientProfile.objects.create(
                    user=user,
                    firm_name=display_name,
                    contact_name=f'{first} {last}'.strip(),
                    phone=phone,
                    city=city,
                    state=state,
                    status='active',
                    onboarding_status='pending_setup',
                    onboarding_complete=False,
                    maintenance_active=False,
                    internal_notes=notes,
                )

                # Zero-amount paid invoice so the downstream gate
                # treats this client identically to a paid client.
                OnboardingInvoice.objects.create(
                    client=profile,
                    line_items=[],
                    total_amount=_Decimal('0'),
                    status='paid',
                    sent_at=timezone.now(),
                    paid_at=timezone.now(),
                )

                token = OnboardingToken.objects.create(client=profile)
        except Exception as exc:  # noqa: BLE001
            _msg.error(request, f'Could not create client: {exc}')
            return redirect('admin_dashboard:send_onboarding')

        # Branded setup email — the only email this flow produces.
        try:
            send_onboarding_setup_email(profile, token)
        except Exception:
            logger.exception(
                'Setup email send failed for %s', profile.pk)

        _msg.success(
            request,
            f'Onboarding link sent to {email}. No invoice required.')
        return redirect(
            'admin_dashboard:invoice_detail', invoice_id=profile.id)

    return render(
        request,
        'admin_dashboard/billing_send_onboarding.html',
        _admin_context(active='billing', form_data={}),
    )


# ── Domain registrations (Namecheap) ────────────────────────────────────────

@admin_required
def admin_stripe_customer_recovery(request, client_id):
    """
    GET — list every Stripe Customer matching the client's email,
    showing the cards on each so admin can identify the right one
    and relink. Solves "saved card disappeared" scenarios where the
    DB's stripe_customer_id got swapped (e.g. by the now-fixed
    `create_or_get_customer` bug that silently orphaned customers).
    """
    import stripe
    from django.conf import settings as _s
    from django.shortcuts import get_object_or_404
    from clients.models import ClientProfile

    stripe.api_key = _s.STRIPE_SECRET_KEY
    profile = get_object_or_404(ClientProfile, pk=client_id)
    email = (profile.user.email or '').strip() if profile.user else ''

    candidates = []
    error = ''
    if not email:
        error = 'Client has no email on file — cannot search Stripe.'
    else:
        try:
            results = stripe.Customer.list(email=email, limit=20)
            for c in (getattr(results, 'data', None) or []):
                # Pull cards for each candidate so we can show
                # last4 + brand — that's how admin tells them apart.
                cards = []
                try:
                    pms = stripe.PaymentMethod.list(
                        customer=c.id, type='card', limit=10)
                    for pm in (getattr(pms, 'data', None) or []):
                        card = getattr(pm, 'card', None)
                        if card is not None:
                            cards.append({
                                'pm_id': pm.id,
                                'brand': getattr(card, 'brand', '').upper(),
                                'last4': getattr(card, 'last4', ''),
                                'exp_month': getattr(card, 'exp_month', ''),
                                'exp_year': getattr(card, 'exp_year', ''),
                            })
                except Exception:
                    logger.exception(
                        'PM list failed for candidate %s', c.id)
                inv_settings = getattr(c, 'invoice_settings', None)
                default_pm = (
                    getattr(inv_settings, 'default_payment_method', '')
                    if inv_settings else ''
                ) or ''
                candidates.append({
                    'id':           c.id,
                    'created':      getattr(c, 'created', None),
                    'name':         getattr(c, 'name', '') or '',
                    'is_current':   c.id == profile.stripe_customer_id,
                    'cards':        cards,
                    'default_pm':   default_pm,
                    'metadata':     getattr(c, 'metadata', None) or {},
                })
        except Exception as exc:  # noqa: BLE001
            error = f'Stripe customer search failed: {exc}'
            logger.exception('Stripe customer search failed')

    return render(
        request,
        'admin_dashboard/stripe_customer_recovery.html',
        _admin_context(
            active='clients',
            profile=profile,
            email=email,
            candidates=candidates,
            error=error,
        ),
    )


@admin_required
@require_POST
def admin_stripe_customer_relink(request, client_id):
    """Switch a client's stripe_customer_id to the chosen Stripe Customer."""
    from django.contrib import messages as _msg
    from django.shortcuts import get_object_or_404
    from clients.models import ClientProfile

    profile = get_object_or_404(ClientProfile, pk=client_id)
    new_customer_id = (request.POST.get('customer_id') or '').strip()
    if not new_customer_id.startswith('cus_'):
        _msg.error(request, 'Invalid Stripe customer ID.')
        return redirect(
            'admin_dashboard:admin_stripe_customer_recovery',
            client_id=client_id)

    old_id = profile.stripe_customer_id
    profile.stripe_customer_id = new_customer_id
    profile.save(update_fields=['stripe_customer_id', 'updated_at'])
    logger.warning(
        'admin_stripe_customer_relink: client %s switched %s -> %s by %s',
        client_id, old_id, new_customer_id, request.user)
    _msg.success(
        request,
        f'Relinked {profile.firm_name} to Stripe customer '
        f'{new_customer_id}. (Was: {old_id or "(none)"})')
    return redirect(
        'admin_dashboard:client_detail', client_id=client_id)


@admin_required
def admin_domain_list(request):
    """Admin overview of every DomainRegistration across all clients."""
    from domains.models import DomainRegistration, NamecheapConfig
    from domains.namecheap_client import NamecheapClient

    domains_qs = (
        DomainRegistration.objects
        .select_related('client', 'client__user')
        .order_by('-created_at')
    )

    # Live NC account balance widget. Best-effort: failure here
    # shows '?' on the dashboard but doesn't break the page.
    nc_balance = None
    nc_balance_error = ''
    try:
        nc_balance = NamecheapClient().get_balances()
    except Exception as exc:  # noqa: BLE001
        nc_balance_error = str(exc)

    return render(
        request,
        'admin_dashboard/domains_list.html',
        _admin_context(
            active='domains',
            domains=domains_qs,
            sandbox_mode=NamecheapConfig.is_sandbox(),
            nc_balance=nc_balance,
            nc_balance_error=nc_balance_error,
            counts={
                'active':  domains_qs.filter(status='active').count(),
                'pending': domains_qs.filter(status='pending').count(),
                'grace':   domains_qs.filter(status='grace').count(),
                'failed':  domains_qs.filter(status='failed').count(),
            },
        ),
    )


@admin_required
def admin_domain_config(request):
    """
    Namecheap configuration page — sandbox/live toggle + a live
    connection test against whichever environment is currently
    active. The toggle itself is a separate POST endpoint so the
    page can be safely refreshed without re-firing it.
    """
    from domains.models import NamecheapConfig
    from domains.namecheap_client import NamecheapError, NamecheapClient

    config = NamecheapConfig.get_solo()

    # Live ping — verify the active credentials still work. Cheap,
    # read-only call (domains.check), so it's safe to fire on every
    # GET. No retries — surface failure fast.
    ping_ok = None
    ping_error = ''
    try:
        client = NamecheapClient()
        result = client.check_availability(['aspiredwebsites.com'])
        ping_ok = bool(result)
    except NamecheapError as exc:
        ping_ok = False
        ping_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        ping_ok = False
        ping_error = f'unexpected: {exc}'

    return render(
        request,
        'admin_dashboard/domains_config.html',
        _admin_context(
            active='domains',
            config=config,
            ping_ok=ping_ok,
            ping_error=ping_error,
        ),
    )


@admin_required
@require_POST
def admin_domain_config_toggle(request):
    """
    Flip sandbox_mode on the singleton config row. Records who did
    it + when so the toggle history is traceable. Followed up with
    a flash message warning if they just switched to LIVE.
    """
    from django.contrib import messages as _msg
    from django.utils import timezone as _tz
    from domains.models import NamecheapConfig

    config = NamecheapConfig.get_solo()
    was_sandbox = config.sandbox_mode
    config.sandbox_mode = not was_sandbox
    config.last_toggled_at = _tz.now()
    config.last_toggled_by = request.user
    config.save(update_fields=[
        'sandbox_mode', 'last_toggled_at',
        'last_toggled_by', 'updated_at'])

    logger.warning(
        'Namecheap mode toggled by %s: %s -> %s',
        request.user, 'SANDBOX' if was_sandbox else 'LIVE',
        'LIVE' if was_sandbox else 'SANDBOX')

    if was_sandbox:
        # Just switched TO live — make this the loudest possible flash.
        _msg.warning(
            request,
            '⚠ Namecheap is now in LIVE mode. Real registrations will '
            'charge the Namecheap account balance ($50.00 currently '
            'available). Switch back to sandbox for testing.')
    else:
        _msg.success(
            request,
            'Namecheap is back in SANDBOX mode. Registrations are '
            'free play-money against the sandbox registry.')
    return redirect('admin_dashboard:admin_domain_config')


@admin_required
def admin_domain_detail(request, reg_id):
    """Full admin view of one DomainRegistration."""
    from django.shortcuts import get_object_or_404
    from domains.models import DomainRegistration

    reg = get_object_or_404(DomainRegistration, pk=reg_id)
    records = reg.dns_records.all().order_by('host', 'record_type')
    return render(
        request,
        'admin_dashboard/domains_detail.html',
        _admin_context(active='domains', reg=reg, records=records),
    )


@admin_required
@require_POST
def admin_domain_sync(request, reg_id):
    """Manual Namecheap state sync trigger."""
    from django.contrib import messages as _msg
    from django.shortcuts import get_object_or_404
    from domains.models import DomainRegistration
    from domains.services import sync_one

    reg = get_object_or_404(DomainRegistration, pk=reg_id)
    try:
        sync_one(reg)
        _msg.success(request, f'Synced {reg.domain_name} from Namecheap.')
    except Exception as exc:  # noqa: BLE001
        _msg.error(request, f'Sync failed: {exc}')
    return redirect('admin_dashboard:admin_domain_detail', reg_id=reg.id)


@admin_required
@require_POST
def admin_domain_repoint(request, reg_id):
    """
    Manual re-point of the auto-A record. Used when a client's
    Droplet IP changes (manual rebuild) and the daily reconcile cron
    hasn't fired yet, or when staff wants to force a re-sync.
    """
    from django.contrib import messages as _msg
    from django.shortcuts import get_object_or_404
    from domains.models import DomainRegistration
    from domains.services import set_auto_a_record

    reg = get_object_or_404(DomainRegistration, pk=reg_id)
    target_ip = (request.POST.get('ip') or '').strip()
    if not target_ip:
        target_ip = str(reg.client.do_droplet_ip or '')
    if not target_ip:
        _msg.error(
            request,
            f'No Droplet IP on client + no IP supplied — can\'t re-point.')
        return redirect(
            'admin_dashboard:admin_domain_detail', reg_id=reg.id)
    try:
        set_auto_a_record(reg, target_ip)
        _msg.success(request, f'Pointed {reg.domain_name} -> {target_ip}.')
    except Exception as exc:  # noqa: BLE001
        _msg.error(request, f'Re-point failed: {exc}')
    return redirect('admin_dashboard:admin_domain_detail', reg_id=reg.id)


@admin_required
def admin_domain_register(request):
    """
    Admin "register a domain for a client" form.

    GET shows the form (client picker + name input + TLD picker +
    optional notes). POST runs the no-Stripe-charge admin path
    via `admin_register_domain_for_client`.

    Multi-domain is supported natively — DomainRegistration has no
    unique-per-client constraint, so the same client can have any
    number of domains.
    """
    from django.contrib import messages as _msg
    from clients.models import ClientProfile
    from domains.models import TLD_CHOICES, NamecheapConfig
    from domains.namecheap_client import NamecheapError
    from domains.services import admin_register_domain_for_client

    clients = (
        ClientProfile.objects
        .filter(status='active')
        .select_related('user')
        .order_by('firm_name')
    )

    if request.method == 'POST':
        client_id = request.POST.get('client_id', '')
        sld = (request.POST.get('sld') or '').strip().lower()
        tld = (request.POST.get('tld') or '').strip().lower()
        notes = (request.POST.get('notes') or '').strip()

        client = ClientProfile.objects.filter(pk=client_id).first()
        if client is None:
            _msg.error(request, 'Pick a client.')
            return redirect('admin_dashboard:admin_domain_register')

        if not sld:
            _msg.error(request, 'Enter a domain name.')
            return redirect('admin_dashboard:admin_domain_register')
        if tld not in dict(TLD_CHOICES):
            _msg.error(request, 'Pick a TLD.')
            return redirect('admin_dashboard:admin_domain_register')

        try:
            reg = admin_register_domain_for_client(
                client, sld, tld,
                send_email=True,
                internal_notes=notes)
        except ValueError as exc:
            _msg.error(request, str(exc))
            return redirect('admin_dashboard:admin_domain_register')
        except NamecheapError as exc:
            _msg.error(
                request,
                f'Namecheap rejected the registration: {exc}')
            return redirect('admin_dashboard:admin_domain_register')
        except Exception as exc:  # noqa: BLE001
            logger.exception('Admin domain registration failed')
            _msg.error(request, f'Registration failed: {exc}')
            return redirect('admin_dashboard:admin_domain_register')

        _msg.success(
            request,
            f'{reg.domain_name} registered to {client.firm_name} — '
            f'no Stripe sub created (admin gift / promo).')
        return redirect(
            'admin_dashboard:admin_domain_detail', reg_id=reg.id)

    return render(
        request,
        'admin_dashboard/domains_register.html',
        _admin_context(
            active='domains',
            clients=clients,
            tld_choices=TLD_CHOICES,
            sandbox_mode=NamecheapConfig.is_sandbox(),
        ),
    )


@admin_required
@require_POST
def admin_domain_register_check(request):
    """
    HTMX endpoint — checks availability for the entered name across
    all 6 TLDs in real time on the admin register form. Returns a
    fragment that swaps into the form.
    """
    from domains.services import check_availability_all_tlds

    sld = (request.POST.get('sld') or '').strip().lower()
    if not sld:
        return render(
            request,
            'admin_dashboard/_domains_register_check.html',
            {'results': [], 'error': 'Enter a name to check.'})

    try:
        results = check_availability_all_tlds(sld)
        error = ''
    except Exception as exc:  # noqa: BLE001
        results = []
        error = f'Namecheap check failed: {exc}'

    return render(
        request,
        'admin_dashboard/_domains_register_check.html',
        {'results': results, 'sld': sld, 'error': error})


@admin_required
def admin_domain_dns(request, reg_id):
    """
    Admin DNS-record editor for any client's domain.

    GET — show the editor pre-filled with the current record set.
    POST — replace the full record set on Namecheap + mirror locally.

    Same foot-shoot guards as the client portal version (no empty
    set, must keep an apex record).
    """
    from django.contrib import messages as _msg
    from django.shortcuts import get_object_or_404
    from domains.models import (
        DNS_RECORD_TYPE_CHOICES, DomainRegistration,
    )
    from domains.namecheap_client import NamecheapError
    from domains.services import replace_dns_records

    reg = get_object_or_404(DomainRegistration, pk=reg_id)

    if request.method == 'POST':
        types = request.POST.getlist('types[]')
        hosts = request.POST.getlist('hosts[]')
        values = request.POST.getlist('values[]')
        ttls = request.POST.getlist('ttls[]')
        prefs = request.POST.getlist('mx_prefs[]')

        new_records = []
        valid_types = {k for k, _ in DNS_RECORD_TYPE_CHOICES}
        for i, raw_value in enumerate(values):
            value = (raw_value or '').strip()
            if not value:
                continue
            r_type = (types[i] if i < len(types) else 'A').upper()
            if r_type not in valid_types:
                continue
            host = (hosts[i] if i < len(hosts) else '@').strip() or '@'
            try:
                ttl = int(ttls[i] if i < len(ttls) else 1800)
            except (ValueError, TypeError):
                ttl = 1800
            ttl = max(60, min(ttl, 86400))
            try:
                mx_pref = int(prefs[i] if i < len(prefs) else 10)
            except (ValueError, TypeError):
                mx_pref = 10
            new_records.append({
                'host': host, 'type': r_type, 'value': value,
                'ttl': ttl, 'mx_pref': mx_pref,
            })

        if not new_records:
            _msg.error(
                request,
                'Refusing to push an empty record set — that would '
                'break the domain. Add at least one record before '
                'saving.')
            return redirect(
                'admin_dashboard:admin_domain_dns', reg_id=reg.id)

        has_apex = any(
            r['host'] in ('@', '')
            and r['type'] in ('A', 'AAAA', 'CNAME', 'URL',
                              'URL301', 'FRAME')
            for r in new_records)
        if not has_apex:
            _msg.error(
                request,
                f'No apex record (host = "@") for {reg.domain_name}. '
                f'Add an A/CNAME/URL with host "@" or the bare '
                f'domain won\'t resolve.')
            return redirect(
                'admin_dashboard:admin_domain_dns', reg_id=reg.id)

        try:
            replace_dns_records(reg, new_records)
        except NamecheapError as exc:
            _msg.error(
                request, f'Namecheap rejected the record set: {exc}')
            return redirect(
                'admin_dashboard:admin_domain_dns', reg_id=reg.id)
        except Exception as exc:  # noqa: BLE001
            logger.exception('Admin DNS update failed for %s', reg.pk)
            _msg.error(request, f'DNS update failed: {exc}')
            return redirect(
                'admin_dashboard:admin_domain_dns', reg_id=reg.id)

        _msg.success(
            request,
            f'DNS records saved for {reg.domain_name}. Propagation '
            f'takes 5-15 minutes.')
        return redirect(
            'admin_dashboard:admin_domain_detail', reg_id=reg.id)

    records = list(reg.dns_records.all().order_by('host', 'record_type'))
    return render(
        request,
        'admin_dashboard/domains_dns.html',
        _admin_context(
            active='domains',
            reg=reg,
            records=records,
            record_types=DNS_RECORD_TYPE_CHOICES,
        ),
    )


@admin_required
@require_POST
def admin_domain_resume(request, reg_id):
    """Admin equivalent of the portal resume button."""
    from django.contrib import messages as _msg
    from django.shortcuts import get_object_or_404
    from domains.models import DomainRegistration
    from domains.services import resume_domain

    reg = get_object_or_404(DomainRegistration, pk=reg_id)
    try:
        resume_domain(reg)
        _msg.success(
            request,
            f'{reg.domain_name} resumed. Registrant restored to '
            f'Aspired Websites, registrar lock re-enabled, Stripe '
            f'cancel reversed, EPP code invalidated.')
    except ValueError as exc:
        _msg.error(request, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception('Admin resume failed for %s', reg.pk)
        _msg.error(request, f'Resume failed: {exc}')
    return redirect('admin_dashboard:admin_domain_detail', reg_id=reg.id)


@admin_required
@require_POST
def admin_domain_park(request, reg_id):
    """
    Force-park a domain (replace DNS with URL301 redirects to
    /parked/). Normally fired automatically when hosting cancels;
    this is the manual escape hatch for admin.
    """
    from django.contrib import messages as _msg
    from django.shortcuts import get_object_or_404
    from domains.models import DomainRegistration
    from domains.services import park_domain

    reg = get_object_or_404(DomainRegistration, pk=reg_id)
    try:
        park_domain(reg)
        _msg.success(
            request,
            f'{reg.domain_name} parked. Visitors now see our '
            f'parking page until you unpark or repoint.')
    except Exception as exc:  # noqa: BLE001
        logger.exception('Admin park failed for %s', reg.pk)
        _msg.error(request, f'Park failed: {exc}')
    return redirect('admin_dashboard:admin_domain_detail', reg_id=reg.id)


@admin_required
@require_POST
def admin_domain_unpark(request, reg_id):
    """Repoint a parked domain at a specified IP (typically the
    client's new Droplet)."""
    from django.contrib import messages as _msg
    from django.shortcuts import get_object_or_404
    from domains.models import DomainRegistration
    from domains.services import unpark_domain

    reg = get_object_or_404(DomainRegistration, pk=reg_id)
    target_ip = (request.POST.get('ip') or '').strip()
    if not target_ip:
        target_ip = str(reg.client.do_droplet_ip or '')
    if not target_ip:
        _msg.error(
            request,
            'No IP supplied + client has no Droplet IP on file. '
            'Unparking needs a destination address.')
        return redirect(
            'admin_dashboard:admin_domain_detail', reg_id=reg.id)
    try:
        unpark_domain(reg, target_ip)
        _msg.success(
            request, f'{reg.domain_name} unparked -> {target_ip}.')
    except Exception as exc:  # noqa: BLE001
        logger.exception('Admin unpark failed for %s', reg.pk)
        _msg.error(request, f'Unpark failed: {exc}')
    return redirect('admin_dashboard:admin_domain_detail', reg_id=reg.id)


@admin_required
@require_POST
def admin_domain_delete(request, reg_id):
    """
    Admin permanent-delete for a FAILED domain registration row.
    Same status guard as the client-portal version. Cascades
    DNSRecord rows. The row goes away for everyone — single source
    of truth (the DB).
    """
    from django.contrib import messages as _msg
    from django.shortcuts import get_object_or_404
    from domains.models import DomainRegistration

    reg = get_object_or_404(DomainRegistration, pk=reg_id)
    if reg.status != 'failed':
        _msg.error(
            request,
            f'Refusing to delete {reg.domain_name} — only failed '
            f'registrations can be deleted from this button. Current '
            f'status: {reg.get_status_display()}.')
        return redirect(
            'admin_dashboard:admin_domain_detail', reg_id=reg.id)

    name = reg.domain_name
    client_name = reg.client.firm_name
    reg.delete()
    _msg.success(
        request,
        f'Deleted failed registration {name} '
        f'(belonged to {client_name}).')
    return redirect('admin_dashboard:admin_domain_list')


@admin_required
@require_POST
def admin_domain_transfer_out(request, reg_id):
    """
    Force the transfer-out package (unlock + EPP + email) from admin.
    Used when a client requests transfer-out via support channel
    rather than the portal.
    """
    from django.contrib import messages as _msg
    from django.shortcuts import get_object_or_404
    from domains.models import DomainRegistration
    from domains.services import begin_transfer_out

    reg = get_object_or_404(DomainRegistration, pk=reg_id)
    reason = (request.POST.get('reason') or 'admin-initiated').strip()
    try:
        epp = begin_transfer_out(reg, reason=reason)
        if epp:
            _msg.success(
                request,
                f'Transfer-out started for {reg.domain_name}. EPP code '
                f'emailed to client.')
        else:
            _msg.success(
                request,
                f'Transfer-out started for {reg.domain_name}. EPP '
                f'will arrive separately from the registry.')
    except Exception as exc:  # noqa: BLE001
        _msg.error(request, f'Transfer-out failed: {exc}')
    return redirect('admin_dashboard:admin_domain_detail', reg_id=reg.id)


# ────────────────────────────────────────────────────────────────────────────
# Phase C — Account + Website admin
# ────────────────────────────────────────────────────────────────────────────

# Account-level fields exposed on the edit form. Keyed by the model
# field; metadata drives the renderer (input type, optional choices,
# section grouping). Kept here rather than in a Form class so the
# template can render the whole thing as a single "edit everything"
# page per the user's spec.
_ACCOUNT_EDIT_SECTIONS = [
    ('Identity', [
        ('name',            'Account holder name',     'text'),
        ('contact_name',    'Secondary contact name',  'text'),
        ('phone',           'Phone',                   'tel'),
        ('email_alt',       'Billing email (optional)', 'email'),
    ]),
    ('Mailing / WHOIS Address', [
        ('address',         'Street address',          'text'),
        ('city',            'City',                    'text'),
        ('state',           'State',                   'text'),
        ('zip_code',        'ZIP code',                'text'),
        ('country',         'Country (2-letter)',      'text'),
    ]),
    ('Account State', [
        ('status',          'Status',                  'select'),
        ('is_tester',       'Tester account',          'checkbox'),
        ('stripe_customer_id', 'Stripe customer ID',   'text'),
    ]),
    ('Communication Preferences', [
        ('preferred_contact_method', 'Preferred contact method', 'select'),
        ('notify_on_stage_change',   'Notify on stage change',   'checkbox'),
        ('notify_on_invoice',        'Notify on invoice',         'checkbox'),
        ('notify_on_scan_complete',  'Notify on scan complete',   'checkbox'),
    ]),
    ('Onboarding', [
        ('onboarding_status',   'Onboarding status',           'select'),
        ('onboarding_complete', 'Onboarding marked complete',  'checkbox'),
    ]),
    ('Internal', [
        ('internal_notes',  'Internal notes (staff only)', 'textarea'),
    ]),
]


@admin_required
def accounts_list(request):
    """
    Primary admin list — accounts (the new top-level entity), with
    each account's website cards inline. Replaces /clients/ as the
    main entry point; the old /clients/ list stays available and
    redirects here only at the user's discretion.
    """
    from clients.account_models import Account

    query = (request.GET.get('q') or '').strip()
    accounts = Account.objects.all().order_by('is_tester', 'name')
    if query:
        accounts = accounts.filter(
            Q(name__icontains=query)
            | Q(user__email__icontains=query)
            | Q(phone__icontains=query))

    accounts = list(accounts.prefetch_related('websites'))
    # Inline per-account summary for the table.
    rows = []
    for acc in accounts:
        websites = list(acc.websites.all().order_by('name'))
        rows.append({
            'account': acc,
            'website_count': len(websites),
            'websites': websites,
        })

    return render(request, 'admin_dashboard/accounts_list.html', _admin_context(
        'accounts', rows=rows, query=query, total=len(accounts),
    ))


@admin_required
def account_detail(request, account_id):
    """
    Single-page editor for everything on an Account — per the user's
    spec, "virtually everything in django admin on my dashboard i can
    edit". POST updates fields on the Account; nested website cards
    deep-link to website_detail.
    """
    from clients.account_models import Account

    account = get_object_or_404(Account, id=account_id)
    user = account.user  # surfaced separately for the Login section

    if request.method == 'POST':
        # Login-enabled toggle is on user.is_active, not on Account.
        # Handle it here BEFORE the Account-field loop so a single
        # Save button writes both.
        if 'user_is_active' in request.POST and user is not None:
            new_active = request.POST.get('user_is_active') == 'on'
            if user.is_active != new_active:
                user.is_active = new_active
                user.save(update_fields=['is_active'])
        errors = []
        # Build allowed-fields whitelist from the section metadata so a
        # crafted POST can't write to fields outside this surface.
        allowed = {
            fname for _, group in _ACCOUNT_EDIT_SECTIONS for fname, _, _ in group
        }
        for field in allowed:
            if field not in request.POST and field not in ('is_tester',
                                                           'onboarding_complete',
                                                           'notify_on_stage_change',
                                                           'notify_on_invoice',
                                                           'notify_on_scan_complete'):
                continue
            spec = _account_field_spec(field)
            if spec['type'] == 'checkbox':
                setattr(account, field, request.POST.get(field) == 'on')
            elif spec['type'] == 'select':
                value = (request.POST.get(field) or '').strip()
                # Validate against the model's choices set so a crafted
                # POST can't write a status the model doesn't accept.
                choices = dict(account._meta.get_field(field).choices or [])
                if value and value not in choices:
                    errors.append(f'{field}: invalid value {value!r}')
                else:
                    setattr(account, field, value)
            else:
                value = (request.POST.get(field) or '').strip()
                setattr(account, field, value)

        from django.contrib import messages
        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            try:
                account.save()
                messages.success(request, 'Account saved.')
                return redirect(
                    'admin_dashboard:account_detail', account_id=account.id)
            except Exception as exc:  # noqa: BLE001
                messages.error(request, f'Save failed: {exc}')

    # Build the section render data with current values.
    sections = []
    for section_label, fields in _ACCOUNT_EDIT_SECTIONS:
        rendered = []
        for fname, flabel, ftype in fields:
            current = getattr(account, fname, '')
            choices = []
            if ftype == 'select':
                # Read choices from the model so the renderer
                # never goes out of sync with model migrations.
                choices = list(account._meta.get_field(fname).choices or [])
            rendered.append({
                'name': fname,
                'label': flabel,
                'type': ftype,
                'value': current,
                'checked': bool(current) if ftype == 'checkbox' else False,
                'choices': choices,
            })
        sections.append({'label': section_label, 'fields': rendered})

    websites = list(account.websites.all().order_by('name'))
    domains = list(account.domains.all().order_by('domain_name'))

    # Delete-impact summary for the danger card modal — shows the
    # admin exactly what will be wiped before they type the name to
    # confirm. Counts are cheap one-shot aggregates; nothing N+1.
    legacy_cp = account.legacy_client_profile
    delete_impact = {
        'websites': len(websites),
        'domains': len(domains),
        'vault_credentials': 0,
        'support_tickets': 0,
        'documents': 0,
        'revisions': 0,
        'scans': 0,
        'active_droplets': 0,
        'active_subscriptions': 0,
    }
    if legacy_cp is not None:
        try:
            delete_impact['vault_credentials'] = (
                legacy_cp.vault.credentials.count()
                if hasattr(legacy_cp, 'vault') and legacy_cp.vault else 0)
        except Exception:
            pass
        delete_impact['support_tickets'] = legacy_cp.tickets.count()
        delete_impact['documents'] = legacy_cp.documents.count()
        delete_impact['revisions'] = legacy_cp.revisions.count()
        try:
            from reporting.models import VulnerabilityScan
            delete_impact['scans'] = VulnerabilityScan.objects.filter(
                client=legacy_cp).count()
        except Exception:
            pass
    # External-state warnings — these are NOT cascaded by the DB
    # delete, so admin must handle them separately. Surfaced in the
    # modal so the admin doesn't end up with orphan resources.
    for w in websites:
        if w.do_droplet_id:
            delete_impact['active_droplets'] += 1
        if (w.stripe_hosting_subscription_id
                or w.stripe_maintenance_subscription_id):
            delete_impact['active_subscriptions'] += 1

    return render(
        request, 'admin_dashboard/account_detail.html',
        _admin_context(
            'accounts',
            account=account,
            user=user,
            sections=sections,
            websites=websites,
            domains=domains,
            delete_impact=delete_impact,
        ),
    )


@admin_required
@require_POST
def account_send_password_reset(request, account_id):
    """
    Admin-triggered password reset email — fires the same Django
    PasswordResetForm flow the public /password-reset/ page uses,
    but bypasses the public form so a tier-1 support call can be
    handled from the admin page directly.

    Requires the account's User to be is_active=True (Django's
    PasswordResetForm filters inactive users). Surfaces a clear
    message when blocked.
    """
    from django.contrib import messages
    from django.contrib.auth.forms import PasswordResetForm

    from clients.account_models import Account

    account = get_object_or_404(Account, id=account_id)
    user = account.user
    if user is None or not user.email:
        messages.error(
            request,
            'This account has no user / email on file — cannot send a '
            'password reset.')
        return redirect(
            'admin_dashboard:account_detail', account_id=account.id)
    if not user.is_active:
        messages.error(
            request,
            'Login is disabled for this account. Toggle "Login enabled" '
            'on first, then send the reset.')
        return redirect(
            'admin_dashboard:account_detail', account_id=account.id)

    form = PasswordResetForm({'email': user.email})
    if not form.is_valid():
        messages.error(request, f'Reset form invalid: {form.errors}')
        return redirect(
            'admin_dashboard:account_detail', account_id=account.id)

    form.save(
        request=request,
        use_https=request.is_secure(),
        email_template_name='public/password_reset_email.txt',
        subject_template_name='public/password_reset_subject.txt',
        from_email=None,  # Falls back to DEFAULT_FROM_EMAIL.
    )
    messages.success(
        request,
        f'Password reset email sent to {user.email}. The link is good '
        f'for 3 days.')
    return redirect(
        'admin_dashboard:account_detail', account_id=account.id)


@admin_required
@require_POST
def account_delete(request, account_id):
    """
    Hard-delete an Account and everything that cascades from it —
    Websites, Domains, the legacy ClientProfile (which itself
    cascades vault, intake, tickets, scans, reports, etc.), and the
    Django User row so the email can be re-onboarded clean.

    Confirmation gate: the admin must POST a ``confirm_name`` value
    that case-insensitively matches the Account's name. The frontend
    already enforces this with a disabled button until the typed
    value matches; the server re-checks so a crafted POST can't skip
    the gate.

    NOT touched (external state — admin handles separately before
    calling this):
      - DigitalOcean droplets (destroy in DO panel or via the
        droplets dashboard before deleting)
      - Stripe customer + subscriptions (cancel in Stripe first
        so no orphan charges happen at next renewal)

    The modal surfaces both as warnings.
    """
    from django.contrib import messages
    from django.db import transaction

    from clients.account_models import Account

    account = get_object_or_404(Account, id=account_id)

    # Safety rail — refuse to delete the account that backs the
    # currently-logged-in admin (no foot-shooting). Also refuse to
    # delete a staff/superuser account; those have admin powers and
    # should be removed via Django admin with explicit intent.
    if account.user_id == request.user.id:
        messages.error(
            request, 'You cannot delete the account you are signed in as.')
        return redirect(
            'admin_dashboard:account_detail', account_id=account.id)
    if account.user and (account.user.is_staff or account.user.is_superuser):
        messages.error(
            request,
            'Refusing to delete a staff/superuser account from this '
            'page. Use Django admin if that is really what you want.')
        return redirect(
            'admin_dashboard:account_detail', account_id=account.id)

    # Confirmation — name typed in the modal must match (no case).
    typed = (request.POST.get('confirm_name') or '').strip().lower()
    expected = (account.name or '').strip().lower()
    if not expected or typed != expected:
        messages.error(
            request,
            'Account name did not match. Deletion cancelled.')
        return redirect(
            'admin_dashboard:account_detail', account_id=account.id)

    legacy_cp = account.legacy_client_profile
    user = account.user
    label = account.name

    try:
        with transaction.atomic():
            # Order matters for clean cascade:
            # 1. Account delete → Websites, Domains, vault_credentials,
            #    onboarding_token, onboarding_invoice, etc. (everything
            #    with FK to Account with on_delete=CASCADE)
            # 2. Legacy ClientProfile delete → vault, intake, tickets,
            #    scans, reports, freshness, NPS, chatbot, etc.
            # 3. User delete → auth row gone so the email is free to
            #    re-onboard.
            account.delete()
            if legacy_cp is not None:
                # CP.legacy_account FK had on_delete=SET_NULL so the
                # CP survived step 1; now finish it.
                legacy_cp.delete()
            if user is not None:
                user.delete()
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f'Deletion failed: {exc}')
        return redirect(
            'admin_dashboard:account_detail', account_id=account_id)

    messages.success(
        request,
        f'Account "{label}" deleted (including all websites, domains, '
        f'vault credentials, and the login). External resources '
        f'(DigitalOcean droplets, Stripe customer) were NOT touched — '
        f'handle those separately.')
    return redirect('admin_dashboard:accounts_list')


def _account_field_spec(field):
    """Return the renderer spec for a single Account field."""
    for _, fields in _ACCOUNT_EDIT_SECTIONS:
        for fname, flabel, ftype in fields:
            if fname == field:
                return {'name': fname, 'label': flabel, 'type': ftype}
    return {'name': field, 'label': field, 'type': 'text'}


@admin_required
def websites_list(request):
    """
    Secondary admin list — all Websites across all accounts. Useful
    when an admin knows the site name but not the account, or wants
    to scan all builds in a particular stage.
    """
    from clients.account_models import Website

    query = (request.GET.get('q') or '').strip()
    stage = (request.GET.get('stage') or '').strip()

    websites = Website.objects.select_related('account').order_by('name')
    if query:
        websites = websites.filter(
            Q(name__icontains=query)
            | Q(url__icontains=query)
            | Q(slug__icontains=query))
    if stage:
        websites = websites.filter(stage=stage)

    return render(request, 'admin_dashboard/websites_list.html', _admin_context(
        'accounts',
        websites=list(websites),
        query=query,
        active_stage=stage,
        stages=Website._meta.get_field('stage').choices,
    ))


@admin_required
def website_detail(request, website_id):
    """
    Single Website edit page. Direct counterpart to account_detail —
    covers per-build state (stage, URL, droplet, payment, etc.).
    """
    from clients.account_models import Website

    website = get_object_or_404(
        Website.objects.select_related('account'), id=website_id)

    if request.method == 'POST':
        from django.contrib import messages
        # Whitelist of editable Website fields. Anything else is
        # ignored — admin can't accidentally clobber timestamps or FKs.
        text_fields = (
            'name', 'business_type', 'url', 'staging_url',
            'do_droplet_id', 'do_droplet_name',
            'stripe_hosting_subscription_id',
            'stripe_maintenance_subscription_id',
            'stripe_invoice_id', 'testimonial_url',
        )
        select_fields = (
            'status', 'stage', 'package', 'onboarding_status',
            'payment_status',
        )
        bool_fields = (
            'maintenance_active', 'session_recording_enabled',
            'auto_send_scan_reports', 'testimonial_received',
            'moonieful_referred',
        )
        int_fields = ('revision_count', 'revision_limit')

        try:
            for f in text_fields:
                if f in request.POST:
                    setattr(website, f, (request.POST.get(f) or '').strip())
            for f in select_fields:
                if f in request.POST:
                    value = (request.POST.get(f) or '').strip()
                    choices = dict(website._meta.get_field(f).choices or [])
                    if value and value not in choices:
                        messages.error(request, f'{f}: invalid value {value!r}')
                        continue
                    setattr(website, f, value)
            for f in bool_fields:
                setattr(website, f, request.POST.get(f) == 'on')
            for f in int_fields:
                if f in request.POST:
                    try:
                        setattr(website, f,
                                int(request.POST.get(f) or 0))
                    except (TypeError, ValueError):
                        messages.error(request, f'{f}: must be a number.')
            website.save()
            messages.success(request, 'Website saved.')
            return redirect(
                'admin_dashboard:website_detail', website_id=website.id)
        except Exception as exc:  # noqa: BLE001
            messages.error(request, f'Save failed: {exc}')

    from clients.account_models import Account

    domains = list(website.domains.all().order_by('domain_name'))

    # Stage stepper pills — same shape as the client portal Project
    # Progress stepper. `current_idx` drives prev/next buttons.
    stage_choices = list(
        website._meta.get_field('stage').choices)
    stage_keys = [k for k, _ in stage_choices]
    try:
        current_idx = stage_keys.index(website.stage)
    except ValueError:
        current_idx = 0
    stage_steps = []
    for i, (key, label) in enumerate(stage_choices):
        if i < current_idx:
            status = 'completed'
        elif i == current_idx:
            status = 'current'
        else:
            status = 'upcoming'
        stage_steps.append({'key': key, 'label': label, 'status': status})
    prev_stage = stage_keys[current_idx - 1] if current_idx > 0 else ''
    next_stage = (
        stage_keys[current_idx + 1]
        if current_idx + 1 < len(stage_keys) else '')

    # All accounts for the move-account dropdown. Exclude the current
    # account so the user can't pick a no-op.
    other_accounts = list(Account.objects.exclude(
        id=website.account_id).order_by('name'))

    # Build the DigitalOcean control-panel URL for the linked droplet
    # info — clickable straight from the admin page.
    do_console_url = (
        f'https://cloud.digitalocean.com/droplets/{website.do_droplet_id}'
        if website.do_droplet_id else '')

    # Resolve the IntakeResponse via the legacy ClientProfile (intake
    # still lives there during Phase C). Used by the template's
    # admin-override card so the button only renders when intake
    # actually needs the manual flip.
    legacy_cp = website.account.legacy_client_profile if website.account else None
    intake_response = getattr(legacy_cp, 'intake', None) if legacy_cp else None
    intake_needs_admin_complete = (
        website.onboarding_status == 'pending_intake'
        or (intake_response is not None and not intake_response.completed)
        or (legacy_cp is not None
            and legacy_cp.onboarding_status == 'pending_intake'))

    return render(
        request, 'admin_dashboard/website_detail.html',
        _admin_context(
            'accounts',
            website=website,
            account=website.account,
            domains=domains,
            stages=stage_choices,
            packages=website._meta.get_field('package').choices,
            payment_statuses=website._meta.get_field('payment_status').choices,
            onboarding_statuses=(
                website._meta.get_field('onboarding_status').choices),
            statuses=website._meta.get_field('status').choices,
            stage_steps=stage_steps,
            prev_stage=prev_stage,
            next_stage=next_stage,
            other_accounts=other_accounts,
            do_console_url=do_console_url,
            intake_response=intake_response,
            intake_needs_admin_complete=intake_needs_admin_complete,
            legacy_cp=legacy_cp,
        ),
    )


@admin_required
@require_POST
def website_intake_mark_complete(request, website_id):
    """
    Admin override — mark a Website's intake as complete WITHOUT
    triggering droplet provisioning or the client confirmation email.

    Used to clean up legacy websites that were imported with the
    `pending_intake` gate set even though intake was already done
    long before the new model existed.

    What this writes:
      - Website.onboarding_status      → 'intake_complete'
      - IntakeResponse.completed       → True
      - IntakeResponse.completed_at    → now (if not already set)
      - Legacy ClientProfile.onboarding_status → 'onboarding_complete'
        (so the portal stops redirecting the client to /intake/)
      - WebsiteStageLog entry          → audit trail

    What this DOES NOT do:
      - provision_droplet_task  (the whole point — admin is opting out)
      - send_intake_received_email (this is an admin override, not a
        client action)
    """
    from django.contrib import messages

    from clients.account_models import Website, WebsiteStageLog

    website = get_object_or_404(Website, id=website_id)
    account = website.account

    # 1. Website flag. Only upgrade if currently 'pending_intake' —
    #    don't downgrade a site that's already 'complete' (a more
    #    advanced state on the same scale).
    if website.onboarding_status == 'pending_intake':
        website.onboarding_status = 'intake_complete'
        website.save(update_fields=['onboarding_status', 'updated_at'])

    # 2. IntakeResponse on the legacy ClientProfile (where intake
    #    actually lives during Phase C). Look up via the account's
    #    legacy CP link.
    legacy_cp = account.legacy_client_profile if account else None
    intake = getattr(legacy_cp, 'intake', None) if legacy_cp else None
    if intake is not None and not intake.completed:
        intake.completed = True
        if intake.completed_at is None:
            intake.completed_at = timezone.now()
        intake.save(update_fields=[
            'completed', 'completed_at', 'updated_at'])

    # 3. Legacy CP onboarding gate — this is what the client-portal
    #    @client_required decorator reads to decide whether to bounce
    #    a logged-in client to /portal/intake/. Flip it so they can
    #    actually see their dashboard.
    if legacy_cp is not None and (
            legacy_cp.onboarding_status != 'onboarding_complete'):
        legacy_cp.onboarding_status = 'onboarding_complete'
        legacy_cp.onboarding_complete = True
        legacy_cp.save(update_fields=[
            'onboarding_status', 'onboarding_complete', 'updated_at'])

    # 4. Audit trail — same pattern as a stage change.
    WebsiteStageLog.objects.create(
        website=website,
        from_stage=website.stage,
        to_stage=website.stage,  # no stage change, just an annotation
        note='Intake marked complete by admin override (no droplet).',
        set_by=request.user.get_full_name() or request.user.username,
    )

    messages.success(
        request,
        'Intake marked complete. No droplet was provisioned and no '
        'confirmation email was sent — flags only.')
    return redirect(
        'admin_dashboard:website_detail', website_id=website.id)


@admin_required
@require_POST
def website_change_stage(request, website_id):
    """
    Move a Website to a new stage from the admin website page. Three
    entry points POST to this view:
      - "Push to next phase" submits the next stage slug.
      - "Back a phase" submits the previous stage slug.
      - "Skip to <stage>" submits any stage slug from the dropdown.

    Validates the slug against the model choices, writes a stage-log
    row, and (for forward moves) fires the stage-change email so the
    client sees the transition in their portal.
    """
    from django.contrib import messages

    from clients.account_models import Website, WebsiteStageLog

    website = get_object_or_404(Website, id=website_id)
    new_stage = (request.POST.get('stage') or '').strip()
    note = (request.POST.get('note') or '').strip()

    valid = [k for k, _ in website._meta.get_field('stage').choices]
    if new_stage not in valid:
        messages.error(request, f'Unknown stage: {new_stage!r}')
        return redirect(
            'admin_dashboard:website_detail', website_id=website.id)

    if new_stage == website.stage:
        messages.info(request, 'Already in that stage.')
        return redirect(
            'admin_dashboard:website_detail', website_id=website.id)

    from_stage = website.stage
    website.stage = new_stage
    website.save(update_fields=['stage', 'updated_at'])

    WebsiteStageLog.objects.create(
        website=website,
        from_stage=from_stage,
        to_stage=new_stage,
        note=note,
        set_by=request.user.get_full_name() or request.user.username,
    )

    messages.success(
        request,
        f'Stage moved {from_stage} → {new_stage}.')
    return redirect(
        'admin_dashboard:website_detail', website_id=website.id)


@admin_required
@require_POST
def website_move_account(request, website_id):
    """
    Reassign a Website to a different Account. Edge-case admin tool
    per user spec G — needed when (for example) a sole-prop client
    forms an LLC and the new entity should own the site, or a Moonieful
    client buys their second site under a separate account.

    Domains pointed at the website come along (they belong to the
    account too); admin can re-point them afterward if needed.
    """
    from django.contrib import messages

    from clients.account_models import Account, Website

    website = get_object_or_404(Website, id=website_id)
    target_account_id = (request.POST.get('account_id') or '').strip()
    new_account = Account.objects.filter(id=target_account_id).first()
    if new_account is None:
        messages.error(request, 'Unknown destination account.')
        return redirect('admin_dashboard:website_detail', website_id=website.id)
    if new_account.id == website.account_id:
        messages.info(request, 'Website is already on that account.')
        return redirect('admin_dashboard:website_detail', website_id=website.id)

    old_account_id = website.account_id
    website.account = new_account
    website.save(update_fields=['account', 'updated_at'])
    # Move every domain currently pointed at this website to the new
    # account too — a domain follows its site.
    moved = website.domains.update(account_new=new_account)
    messages.success(
        request,
        f'Website moved to {new_account.name}. '
        f'{moved} domain(s) reassigned. Old account: {old_account_id}.',
    )
    return redirect('admin_dashboard:website_detail', website_id=website.id)


@admin_required
@require_POST
def domain_move_account(request, reg_id):
    """
    Reassign a DomainRegistration to a different Account (user spec G).
    Resets ``pointed_at_website`` since the old site's accounts may not
    include it any more — admin must re-point if needed.
    """
    from django.contrib import messages

    from clients.account_models import Account
    from domains.models import DomainRegistration

    reg = get_object_or_404(DomainRegistration, id=reg_id)
    target_account_id = (request.POST.get('account_id') or '').strip()
    new_account = Account.objects.filter(id=target_account_id).first()
    if new_account is None:
        messages.error(request, 'Unknown destination account.')
        return redirect('admin_dashboard:admin_domain_detail', reg_id=reg.id)
    if new_account.id == reg.account_new_id:
        messages.info(request, 'Domain is already on that account.')
        return redirect('admin_dashboard:admin_domain_detail', reg_id=reg.id)

    reg.account_new = new_account
    reg.pointed_at_website = None
    reg.save(update_fields=['account_new', 'pointed_at_website', 'updated_at'])
    messages.success(
        request,
        f'Domain {reg.domain_name} moved to {new_account.name}. '
        f'Re-point to a website on the new account when ready.')
    return redirect('admin_dashboard:admin_domain_detail', reg_id=reg.id)
