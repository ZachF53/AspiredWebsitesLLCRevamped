"""
Admin dashboard views. Every view is gated by Django's `staff_member_required`
(redirects to /admin/login/ for unauthenticated users, 403s logged-in
non-staff users). Lead data comes from outreach.Lead.
"""

import datetime

from django.conf import settings
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponseBadRequest
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

from .decorators import admin_required
from .forms import LeadAddForm, LeadNoteForm, ScrapeForm


# ────────────────────────────────────────────────────────────────────────────
# Shared context
# ────────────────────────────────────────────────────────────────────────────

def _admin_context(active=None, **extra):
    """
    Base context every admin view should merge in. Provides:
      - active: which top-nav item to highlight
      - needs_you_count: badge number for the Needs You nav item
    """
    needs_you_count = EmailReply.objects.filter(
        needs_human=True, handled=False
    ).count()
    ctx = {
        'active': active,
        'needs_you_count': needs_you_count,
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

    return render(request, 'admin_dashboard/home.html', _admin_context(
        active='home',
        stats=stats,
        pipeline=pipeline,
        recent_leads=recent_leads,
        recent_emails=recent_emails,
        unhandled_replies=unhandled_replies,
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


def _render_needs_you_list(request):
    """Render the queue list partial (used as the HTMX response after an
    action). Includes an OOB swap that keeps the nav badge in sync."""
    replies = list(_needs_you_replies())
    return render(request, 'admin_dashboard/_needs_you_list.html', {
        'replies': replies,
        'needs_you_count': len(replies),
    })


@admin_required
def needs_you(request):
    replies = list(_needs_you_replies())
    return render(request, 'admin_dashboard/needs_you.html', _admin_context(
        active='needs_you',
        replies=replies,
        needs_you_count=len(replies),
    ))


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
