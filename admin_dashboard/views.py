"""
Admin dashboard views. Every view is gated by Django's `staff_member_required`
(redirects to /admin/login/ for unauthenticated users, 403s logged-in
non-staff users). Lead data comes from outreach.Lead.
"""

import datetime
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
    ctx = {
        'active': active,
        'needs_you_count': needs_you_count,
        'critical_health_count': critical_health_count,
        'active_proposals_count': active_proposals_count,
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
        project = client.projects.first()
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
    project = client.projects.first()
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
    from clients.models import ClientProfile
    from reporting.models import GBPSyncCheck, NPSSurvey
    from reporting.uptime_helpers import (
        get_avg_response_time, get_current_status, get_uptime_percentage,
    )

    client = get_object_or_404(ClientProfile, id=client_id)
    project = (client.projects.filter(stage='live').first()
               or client.projects.first())

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

    return render(request, 'admin_dashboard/client_detail.html', _admin_context(
        'clients',
        client=client,
        project=project,
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
    ))


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
        live_project = (client.projects.filter(stage='live').first()
                        or client.projects.first())
        live_url = (live_project.live_url or '') if live_project else ''
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
    """Conversion dashboard — month-over-month counts + recent event log."""
    from clients.models import ClientProfile
    from reporting.conversion_helpers import conversion_counts
    from reporting.models import ConversionEvent

    client = get_object_or_404(ClientProfile, id=client_id)
    return render(request, 'admin_dashboard/client_conversions.html',
                  _admin_context(
                      'clients',
                      client=client,
                      counts=conversion_counts(client),
                      events=ConversionEvent.objects.filter(client=client)[:50],
                  ))


@admin_required
def client_tracker(request, client_id):
    """Snippet generator — the personalised conversion-tracker <script> tag."""
    from clients.models import ClientProfile
    from reporting.models import ConversionEvent

    client = get_object_or_404(ClientProfile, id=client_id)
    snippet = (
        f'<script src="{settings.SITE_BASE_URL}/static/js/aspired-tracker.js" '
        f'data-aspired-client="{client.id}" defer></script>'
    )
    return render(request, 'admin_dashboard/client_tracker.html', _admin_context(
        'clients',
        client=client,
        snippet=snippet,
        last_event=ConversionEvent.objects.filter(client=client).first(),
    ))


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

    project = client.projects.first()
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
    html_content = (
        f"<p>Hi {contact_name},</p>"
        f"<p>Please find attached your monthly security assessment "
        f"report for {month_str}.</p>"
        f"<p>{severity_line}</p>"
        f"<p>The full report is attached as a PDF. You can also log "
        f"into your portal to view your security history:</p>"
        f"<p><a href='{settings.SITE_BASE_URL}/portal/security/'>"
        f"View Your Portal</a></p>"
        f"<p>If you have any questions about the findings, please "
        f"don't hesitate to reach out.</p>"
        f"<p>— Zachery Long<br>"
        f"Aspired Websites LLC<br>"
        f"210-896-2536<br>"
        f"zacherylong@aspiredwebsites.com</p>"
    )

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import (
            Attachment, Disposition, FileContent, FileName,
            FileType, Mail,
        )
    except ImportError:
        return _banner(
            'error', 'SendGrid SDK not installed.', status=500)

    message = Mail(
        from_email=getattr(settings, 'EMAIL_FROM_NO_REPLY',
                           settings.DEFAULT_FROM_EMAIL),
        to_emails=client_email,
        subject=(f'Your Security Report — {month_str} — '
                 f'{client.firm_name}'),
        html_content=html_content,
    )
    with open(abs_path, 'rb') as fh:
        encoded = base64.b64encode(fh.read()).decode()
    ext = os.path.splitext(abs_path)[1] or '.pdf'
    mime = 'application/pdf' if ext == '.pdf' else 'text/html'
    attachment = Attachment(
        FileContent(encoded),
        FileName(f'security-report-{month_str}{ext}'),
        FileType(mime),
        Disposition('attachment'),
    )
    message.attachment = attachment

    try:
        sg = SendGridAPIClient(settings.SENDGRID_API_KEY)
        sg.send(message)
    except Exception as exc:  # noqa: BLE001 — surface to operator
        return _banner(
            'error', f'SendGrid error: {str(exc)[:200]}', status=500)

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
    project = (client.projects.filter(stage='live').first()
               or client.projects.first())
    target_url = (project.live_url if project else '') or ''
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
    run_vulnerability_scan_task.delay(str(scan.id))

    if request.headers.get('HX-Request') == 'true':
        return HttpResponse(
            f'<div class="scan-banner scan-banner--info">'
            f'Scan started — check '
            f'<a href="{reverse("admin_dashboard:scans_list")}">Scans</a> '
            f'for results.</div>')
    return redirect('admin_dashboard:scans_list')


# ────────────────────────────────────────────────────────────────────────────
# Client edit + inline quick-edit
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def client_edit(request, client_id):
    """
    Full client edit form. live_url isn't stored on ClientProfile — it
    lives on the Project — so the form surfaces it as a top-level field
    and the view writes it back to the live (or first) project.
    """
    from clients.models import ClientProfile
    from django.contrib import messages

    from .forms import ClientProfileEditForm

    client = get_object_or_404(ClientProfile, id=client_id)
    project = (client.projects.filter(stage='live').first()
               or client.projects.first())
    current_live_url = (project.live_url if project else '') or ''

    if request.method == 'POST':
        form = ClientProfileEditForm(request.POST, instance=client)
        if form.is_valid():
            form.save()
            # live_url lives on Project. Only save when changed so we
            # don't bump updated_at on no-op edits.
            if project:
                new_url = (form.cleaned_data.get('live_url') or '').strip()
                if new_url != (project.live_url or ''):
                    project.live_url = new_url
                    project.save(update_fields=['live_url', 'updated_at'])
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

    project = (client.projects.filter(stage='live').first()
               or client.projects.first())

    def _current_value():
        if field_name == 'live_url':
            return (project.live_url if project else '') or ''
        return getattr(client, field_name, '') or ''

    if request.method == 'POST':
        new_value = (request.POST.get('value') or '').strip()
        # Live URL writes through to Project, everything else to the
        # client profile.
        if field_name == 'live_url':
            if not project:
                return HttpResponseBadRequest('client has no project yet')
            project.live_url = new_value
            project.save(update_fields=['live_url', 'updated_at'])
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

    # 3. Active non-tester clients with no live URL on their live
    #    project — uptime monitoring + scans can't run without one.
    no_url = (ClientProfile.objects
              .filter(status='active', is_tester=False,
                      projects__stage='live', projects__live_url='')
              .distinct()[:3])
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

    project = (client.projects.filter(stage='live').first()
               or client.projects.first())
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
