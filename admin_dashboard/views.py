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
    ScrapeJob,
    SuppressionList,
)
from outreach.pipeline import import_leads
from outreach.scoring import score_lead

logger = logging.getLogger(__name__)

from .decorators import admin_required
from .context import (  # noqa: F401
    _active_proposals_count,
    _admin_context,
    _critical_health_count,
    _high_priority_gaps_count,
    _intel_pending_count,
)

from clients.display import owner_label

from .forms import (
    DeploymentLogForm,
    LeadAddForm,
    LeadNoteForm,
    ScrapeForm,
    ScrapeJobForm,
    ServiceTierForm,
)


# ────────────────────────────────────────────────────────────────────────────
# Shared context
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
        from clients.account_models import Website
        # Per site: an intake is submitted for a website, so an account
        # with two builds awaiting review is two items of work.
        needs_you_count += Website.objects.filter(
            needs_admin_review_at__isnull=False,
            admin_reviewed_at__isnull=True,
        ).count()
    except Exception:
        pass
    # Only actually-sent rows count toward "today" — pending/approved
    # rows have sent_at IS NULL.
    emails_sent_today = EmailSent.objects.filter(
        status='sent', sent_at__date=today
    ).count()

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
    recent_emails = (
        EmailSent.objects.filter(status='sent')
        .select_related('lead').order_by('-sent_at')[:5]
    )
    unhandled_replies = (
        EmailReply.objects.select_related('lead')
        .filter(needs_human=True, handled=False)
        .order_by('-received_at')[:5]
    )

    # Phase 7 Part 1 — Today's Focus widget. `get_daily_focus` is
    # defined further down in this same file; Python resolves the
    # name at call time so the forward reference is fine.

    # AI usage widget — this-month token totals + USD cost across
    # every model the project uses. Defensive: the table might not
    # exist yet on pre-migration environments.
    ai_usage = {'per_model': [], 'total_tokens': 0,
                'total_cost_usd': 0.0, 'total_requests': 0}
    try:
        from reporting.models import ClaudeUsage
        ai_usage = ClaudeUsage.current_month_summary()
    except Exception:
        pass

    return render(request, 'admin_dashboard/home.html', _admin_context(
        active='home',
        stats=stats,
        pipeline=pipeline,
        recent_leads=recent_leads,
        recent_emails=recent_emails,
        unhandled_replies=unhandled_replies,
        daily_focus=get_daily_focus(),
        ai_usage=ai_usage,
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
    # 'no_email' triage chip — surfaces leads the sender will never
    # touch until someone finds an address for them.
    contact_filter = request.GET.get('contact') or ''

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
    if contact_filter == 'no_email':
        qs = qs.filter(email='')
    elif contact_filter == 'no_email_no_phone':
        qs = qs.filter(email='', phone='')
    elif contact_filter == 'has_email':
        qs = qs.exclude(email='')

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
    keep = ['q', 'status', 'temperature', 'state', 'practice_area',
            'source', 'sort', 'created', 'contact']
    qs_parts = [f'{k}={request.GET.get(k)}' for k in keep if request.GET.get(k)]
    filter_qs = ('&' + '&'.join(qs_parts)) if qs_parts else ''

    # Counts for the contact chips — single aggregate scan, surfaces
    # the size of the 'needs an email' bucket above the table so the
    # operator can triage without guessing.
    all_leads_count = Lead.objects.count()
    no_email_count = Lead.objects.filter(email='').count()
    has_email_count = all_leads_count - no_email_count

    # Brave Search usage banner — shows this month's query count
    # against the free-tier quota so the admin sees how close they
    # are to paying $5/1000. Defensive — table renders even if the
    # usage model fails to import (fresh checkout pre-migration).
    brave_used = 0
    brave_limit = getattr(settings, 'BRAVE_SEARCH_MONTHLY_LIMIT', 1000)
    try:
        from outreach.models import BraveSearchUsage
        brave_used = BraveSearchUsage.current()
    except Exception:
        pass
    brave_percent = (
        round(brave_used / brave_limit * 100) if brave_limit else 0)
    brave_remaining = max(0, brave_limit - brave_used)

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
        contact_filter=contact_filter,
        no_email_count=no_email_count,
        has_email_count=has_email_count,
        sort_key=sort_key,
        status_choices=Lead.STATUS_CHOICES,
        temperature_choices=Lead.TEMPERATURE_CHOICES,
        source_choices=Lead.SOURCE_CHOICES,
        states=states,
        practice_areas=practice_areas,
        filter_qs=filter_qs,
        brave_used=brave_used,
        brave_limit=brave_limit,
        brave_percent=brave_percent,
        brave_remaining=brave_remaining,
    ))


# ────────────────────────────────────────────────────────────────────────────
# Lead detail (basic — HTMX interactions land in next iteration)
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def lead_detail(request, pk):
    from outreach.scoring import score_breakdown
    lead = get_object_or_404(Lead, pk=pk)

    # Reconstruct the dict the scorer would have seen at import time
    # (the model stores the same fields the scraper produced). Feeds
    # the "Scraper Data + Score Breakdown" accordion at the bottom of
    # the page so the admin can see WHAT the scraper found and HOW
    # each signal contributed to the final score.
    scorer_dict = {
        'website': lead.website,
        'has_google_business': lead.has_google_business,
        'google_review_count': lead.google_review_count,
        'website_performance_score': lead.website_performance_score,
        # Enricher-populated signals (outreach/enricher.py)
        'facebook_url': lead.facebook_url,
        'instagram_url': lead.instagram_url,
        'linkedin_url': lead.linkedin_url,
        'has_ssl': lead.has_ssl,
        'has_generic_email': lead.has_generic_email,
        'copyright_year': lead.copyright_year,
    }
    from outreach.scoring import MAX_SCORE
    breakdown = score_breakdown(scorer_dict)
    raw_total = sum(r['points'] for r in breakdown)
    score_total_capped = min(raw_total, MAX_SCORE)
    # Raw fields the scraper actually wrote, for the "what the scraper
    # saw" half of the accordion. Anything blank/null gets a dash
    # in the template via |default:'—'.
    scraper_fields = [
        ('Firm name', lead.firm_name),
        ('Attorney / contact name', lead.attorney_name),
        ('Business type', lead.business_type),
        ('Practice area', lead.practice_area),
        ('Email', lead.email),
        ('Phone', lead.phone),
        ('Website', lead.website),
        ('Address', lead.address),
        ('City', lead.city),
        ('State', lead.state),
        ('Google rating', lead.google_rating),
        ('Google review count', lead.google_review_count),
        ('Has Google Business Profile', lead.has_google_business),
        ('PageSpeed performance', lead.website_performance_score),
        ('PageSpeed SEO', lead.website_seo_score),
        ('PageSpeed mobile', lead.website_mobile_score),
        # Enrichment-derived
        ('SSL / HTTPS', lead.has_ssl),
        ('Generic email domain', lead.has_generic_email),
        ('Copyright year', lead.copyright_year),
        ('Facebook', lead.facebook_url),
        ('Instagram', lead.instagram_url),
        ('LinkedIn', lead.linkedin_url),
        ('Other social', ', '.join(lead.other_social_urls or []) or None),
        ('Source', lead.get_source_display()),
        ('Imported at', lead.created_at),
        ('Enriched at', lead.enrichment_completed_at),
    ]

    # State for the 'Generate email' card — pre-compute on the server
    # so the template can branch cleanly without re-querying.
    next_step = lead.sequence_step + 1
    pending_outreach = EmailSent.objects.filter(
        lead=lead, status='pending_approval',
    ).order_by('-created_at').first()
    can_generate = (
        bool(lead.email)
        and not lead.unsubscribed
        and next_step <= 4
        and pending_outreach is None
    )

    return render(request, 'admin_dashboard/lead_detail.html', _admin_context(
        active='leads',
        lead=lead,
        notes=lead.lead_notes.all(),
        emails=lead.emails_sent.all(),
        replies=lead.replies.all(),
        note_form=LeadNoteForm(),
        status_choices=Lead.STATUS_CHOICES,
        score_breakdown=breakdown,
        score_total=score_total_capped,
        score_total_raw=raw_total,
        scraper_fields=scraper_fields,
        next_step=next_step,
        pending_outreach=pending_outreach,
        can_generate=can_generate,
    ))


@admin_required
@require_POST
def lead_reenrich(request, pk):
    """
    Admin-triggered enrichment refresh — fires the same
    outreach.tasks.enrich_lead_task that import_leads enqueues for
    every new lead. Used to:
      - Backfill leads that pre-date the enrichment feature.
      - Refresh a stale lead after its website changed.
      - Retry a lead whose initial enrichment crashed.

    Always returns to the lead detail page with a flash message —
    the actual enrichment work happens in Celery and shows up in the
    accordion log within ~30s.
    """
    from django.contrib import messages

    lead = get_object_or_404(Lead, pk=pk)
    try:
        from outreach.tasks import enrich_lead_task
        enrich_lead_task.delay(str(lead.pk))
        messages.success(
            request,
            'Re-enrichment queued — homepage scrape + PageSpeed + '
            'Custom Search fallback will run in the background. '
            'Refresh in ~30 seconds.')
    except Exception as exc:  # noqa: BLE001
        messages.error(
            request, f'Could not enqueue enrichment: {exc}')
    return redirect('admin_dashboard:lead_detail', pk=lead.pk)


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
        # Playwright imports native greenlet bindings.  Keep that optional,
        # heavyweight dependency out of Django startup so checks, migrations,
        # and unrelated admin pages work even when the browser runtime is not
        # installed or available on this machine.
        from outreach.scraper import (
            scrape_georgia_bar_sync,
            scrape_google_maps_sync,
            scrape_texas_bar_sync,
        )

        source = form.cleaned_data['source']
        niche = form.cleaned_data['niche'].strip()
        city = form.cleaned_data['city']
        state = form.cleaned_data['state']
        max_results = int(form.cleaned_data['max_results'])

        api_calls = None
        try:
            if source == 'google_maps':
                state_full = 'Texas' if state == 'TX' else 'Georgia'
                # Niche goes in verbatim — query becomes
                # "{niche} in {city} {state}". No " lawyer" suffix.
                raw, api_calls = scrape_google_maps_sync(
                    niche, city, state_full, max_results
                )
                import_source = 'google_maps'
            elif source == 'texas_bar':
                # Bar scrapers use the niche as the practice_area
                # filter — type "Family Law" / "Personal Injury" /
                # etc. exactly as they appear in the bar directory.
                raw = scrape_texas_bar_sync(
                    city=city, practice_area=niche,
                    max_results=max_results,
                )
                import_source = 'state_bar'
            else:  # georgia_bar
                raw = scrape_georgia_bar_sync(
                    city=city, practice_area=niche,
                    max_results=max_results,
                )
                import_source = 'state_bar'

            # Tag every imported lead with the niche as business_type
            # so a "dentist" search produces leads labelled 'Dentist',
            # not the legacy 'Law Firm' default. Title-cased for
            # display consistency.
            results = import_leads(
                raw,
                source=import_source,
                business_type_override=niche.title(),
            )
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
    from clients.account_models import Website
    return (
        Website.objects
        # `user` and `intake` were the ClientProfile accessors, left
        # behind when this moved to Website. The queryset raised
        # FieldError on evaluation, so the whole Needs You page 500'd --
        # the queue Zach works from. On Website the user hangs off the
        # account and the intake relation is `intake_new`.
        .select_related('account', 'account__user', 'intake_new')
        .filter(
            needs_admin_review_at__isnull=False,
            admin_reviewed_at__isnull=True,
        )
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
    from clients.account_models import Website
    client = get_object_or_404(Website, id=client_id)
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

    model = 'claude-haiku-4-5-20251001'
    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=model,
        max_tokens=700,
        messages=[{'role': 'user', 'content': prompt}],
    )
    # Token accounting → admin dashboard AI Usage widget. Best-effort.
    try:
        from reporting.models import ClaudeUsage
        u = getattr(message, 'usage', None)
        if u is not None:
            ClaudeUsage.record(
                model=model,
                input_tokens=getattr(u, 'input_tokens', 0),
                output_tokens=getattr(u, 'output_tokens', 0))
    except Exception:
        logger.exception('ClaudeUsage.record failed in reply-draft')
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
        # trust_level, daily_send_cap and outreach_active are no longer
        # accepted here. They drive outreach/sender.py, gating.py and
        # warming.py, whose beat entries were removed when sending moved
        # to Instantly, so editing them changed nothing while looking
        # exactly like the controls that govern sending. The fields
        # remain on the model for historical EmailSent rows; they are
        # simply no longer presented or writable from this page.
        #
        # An unchecked checkbox is absent from POST entirely, which is
        # why this reads as membership rather than a value lookup.
        config.instantly_sending_enabled = (
            'instantly_sending_enabled' in request.POST)

        for field, lo, hi in (
            ('min_warmup_score', 0, 100),
            ('min_warmup_days', 0, 90),
            ('min_ready_mailboxes', 1, 50),
        ):
            try:
                value = int(request.POST.get(field, getattr(config, field)))
                setattr(config, field, max(lo, min(value, hi)))
            except (TypeError, ValueError):
                pass

        config.save()
        return redirect(reverse('admin_dashboard:settings') + '?saved=1')

    # Live mailbox state. Never raises -- warmup_readiness returns a dict
    # carrying its own failure reason so the page can explain an outage
    # rather than 500 or, worse, render blank and imply everything is fine.
    from outreach import instantly

    try:
        warmup = instantly.warmup_readiness()
    except Exception:            # noqa: BLE001 - the page must still render
        logger.exception('settings_view: warmup readiness failed')
        warmup = {'ready': False, 'reason': 'Could not read mailbox state.',
                  'ready_mailboxes': 0, 'required': 0, 'daily_capacity': 0,
                  'detail': []}
    try:
        sending_allowed, sending_reason = instantly.sending_allowed()
    except Exception:            # noqa: BLE001
        sending_allowed, sending_reason = False, 'Could not evaluate gates.'

    return render(request, 'admin_dashboard/settings.html', _admin_context(
        active='settings',
        config=config,
        warming=_warming_status(),
        warmup=warmup,
        sending_allowed=sending_allowed,
        sending_reason=sending_reason,
        saved=request.GET.get('saved') == '1',
    ))


def _stub(request, *, active, title, blurb):
    return render(request, 'admin_dashboard/_stub.html', _admin_context(
        active=active,
        page_title=title,
        blurb=blurb,
    ))


# ────────────────────────────────────────────────────────────────────────────
# Pricing manager — extracted to views_pricing.py
# ────────────────────────────────────────────────────────────────────────────
from .views_pricing import (  # noqa: E402,F401
    pricing_edit,
    pricing_feature_add,
    pricing_feature_delete,
    pricing_list,
    pricing_toggle,
)


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_onboarding_questions.py
# ──────────────────────────────────────────────────────────────────────────
from .views_onboarding_questions import (  # noqa: E402,F401
    _choices_to_text,
    _derive_tier_slug,
    _parse_choices,
    onboarding_mark_complete,
    onboarding_question_delete,
    onboarding_question_form,
    onboarding_questions,
    onboarding_section_delete,
    onboarding_section_form,
)


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_deploy.py
# ──────────────────────────────────────────────────────────────────────────
from .views_deploy import (  # noqa: E402,F401
    _domain_from_url,
    deploy_client,
    deploy_fresh,
    deploy_history,
    deploy_home,
    deploy_log_create,
    deploy_redeploy,
)


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_changelog.py
# ──────────────────────────────────────────────────────────────────────────
from .views_changelog import (  # noqa: E402,F401
    _is_uuid,
    _parse_deploy_log,
    changelog_add,
    changelog_add_website,
    changelog_delete,
    changelog_edit,
    changelog_import,
    changelog_list,
    website_changelog,
)


# ────────────────────────────────────────────────────────────────────────────
# Clients — list, detail hub, and the Phase 5a monitoring pages
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def client_list(request):
    """
    All clients — entry point to the per-client monitoring tools.

    One row per ACCOUNT, expandable into its Websites. Each site carries
    its own droplet IP, stage and package, which is the whole reason the
    row expands: a single-droplet, single-package row cannot describe an
    account running two builds.

    This used to iterate ClientProfiles and hydrate each with its migrated
    Account, carrying a third branch that synthesised a fake one-website
    row out of the legacy profile for clients that had no Account. Reading
    the canonical table removes that branch entirely — and with it the
    possibility of an account created after the cutover being absent from
    the client list, which is what the legacy iteration would have done.
    """
    from clients.account_models import Account, Website
    from reporting.models import VulnerabilityFinding, VulnerabilityScan

    # Real clients first, testers at the bottom. `is_tester` is False=0 /
    # True=1 so a plain ascending sort puts non-testers first.
    accounts = Account.objects.order_by('is_tester', 'name')
    query = (request.GET.get('q') or '').strip()
    if query:
        accounts = accounts.filter(name__icontains=query)
    accounts = list(accounts)

    # Pull every Website for these Accounts in ONE query, grouped by
    # account_id so per-row lookup is O(1).
    account_ids = [a.id for a in accounts]
    websites_by_account = {}
    if account_ids:
        for w in Website.objects.filter(account_id__in=account_ids):
            websites_by_account.setdefault(w.account_id, []).append(w)

    # Last completed scan per SITE — a scan targets one droplet, so an
    # account-level "last scan" described whichever site happened to be
    # scanned most recently and said nothing about the others.
    all_sites = [w for group in websites_by_account.values() for w in group]
    last_scan_by_site = {}
    for scan in (VulnerabilityScan.objects
                 .filter(status='complete', website_new__in=all_sites)
                 .order_by('website_new_id', '-completed_at')):
        last_scan_by_site.setdefault(scan.website_new_id, scan)

    # Has-open-critical / has-open-high lookups for the severity dot —
    # one count() per scan would be N+1, so pre-aggregate in two queries.
    open_critical_by_scan = set(
        VulnerabilityFinding.objects
        .filter(scan__in=last_scan_by_site.values(),
                status='open', severity='critical')
        .values_list('scan_id', flat=True).distinct()
    )
    open_high_by_scan = set(
        VulnerabilityFinding.objects
        .filter(scan__in=last_scan_by_site.values(),
                status='open', severity='high')
        .values_list('scan_id', flat=True).distinct()
    )

    def _dot(scan):
        if scan is None:
            return 'never'      # ⚪
        if scan.id in open_critical_by_scan:
            return 'critical'   # 🔴
        if scan.id in open_high_by_scan:
            return 'high'       # 🟠
        return 'clean'          # 🟢

    # Account-level severity is the WORST of its sites: a clean second
    # site must not mask a critical finding on the first.
    _RANK = {'critical': 0, 'high': 1, 'clean': 2, 'never': 3}

    rows = []
    for account in accounts:
        sites = websites_by_account.get(account.id, [])

        wsite_rows = []
        for w in sites:
            scan = last_scan_by_site.get(w.id)
            wsite_rows.append({
                'pk':              w.pk,
                'name':            w.name,
                'stage':           w.stage,
                'stage_display':   w.get_stage_display(),
                'package_display': (
                    w.get_package_display() if w.package else ''),
                'droplet_ip':      w.do_droplet_ip or '',
                'url':             w.url or '',
                'last_scan':       scan,
                'scan_dot':        _dot(scan),
                'is_legacy':       False,
            })

        droplets_with_ip = sum(1 for w in wsite_rows if w['droplet_ip'])

        # Compact package summary — distinct packages joined; an account
        # with three 'Maintenance — Growth' websites shows one badge
        # labelled "Maintenance — Growth ×3".
        from collections import Counter
        pkg_counts = Counter(
            w['package_display'] for w in wsite_rows if w['package_display'])
        if pkg_counts:
            package_summary = ' · '.join(
                f'{pkg}{" ×" + str(n) if n > 1 else ""}'
                for pkg, n in pkg_counts.items()
            )
        else:
            package_summary = '—'

        dots = [w['scan_dot'] for w in wsite_rows] or ['never']
        worst = min(dots, key=lambda d: _RANK[d])
        newest_scan = max(
            (w['last_scan'] for w in wsite_rows if w['last_scan']),
            key=lambda sc: sc.completed_at,
            default=None,
        )

        rows.append({
            'client':           account,
            'account':          account,
            'last_scan':        newest_scan,
            'scan_dot':         worst,
            'websites':         wsite_rows,
            'website_count':    len(wsite_rows),
            'droplets_count':   droplets_with_ip,
            'package_summary':  package_summary,
        })

    return render(request, 'admin_dashboard/client_list.html', _admin_context(
        'clients', clients=accounts, query=query, rows=rows,
    ))


@admin_required
def client_detail(request, client_id):
    """
    Legacy per-client hub — RETIRED. The Account/Website pages replace it:
    account-level data on account_detail, per-build state + monitoring on
    website_detail. Kept as a redirect so the inbound links across the
    admin still resolve while they get repointed.
    """
    # Inbound links carry a legacy ClientProfile id. Resolved through
    # Account's own legacy_client_profile column, so keeping those links
    # alive costs no legacy read — and an id that is already an Account's
    # works too, for links repointed since.
    from clients.account_models import Account

    account = (Account.objects.filter(id=client_id).first()
               or Account.objects.filter(
                   legacy_client_profile_id=client_id).first())
    if account is not None:
        return redirect(
            'admin_dashboard:account_detail', account_id=account.id)
    return redirect('admin_dashboard:accounts_list')


@admin_required
def clients_onboarding(request):
    """
    Legacy-client onboarding status board — every pre-platform client and
    what still needs finishing on each (user, live URL, SSH vault key,
    uptime monitoring, email). Cards are colour-coded by completeness so
    the most-stale row jumps out first.
    """
    from clients.account_models import Website
    from clients.models import UptimeRecord
    from vault.models import ClientVault, VaultCredential

    # Per site: each card is one build's readiness checklist (its URL, its
    # uptime data, its droplet's key). An account with two builds has two
    # sets of gaps to close.
    legacy = (
        Website.objects
        .filter(account__internal_notes__contains='Legacy client')
        .select_related('account', 'account__user')
        .order_by('account__name', 'name')
    )

    # Cheap lookups so we don't do N+1 queries inside the template.
    # Credentials hang off the ACCOUNT's vault, so the map is keyed on the
    # account and every one of its sites shares the same key.
    account_ids = {w.account_id for w in legacy}
    vault_ids_by_account = {}
    for cred in VaultCredential.objects.filter(
            is_ssh_credential=True,
            vault__account_new_id__in=account_ids).select_related('vault'):
        # First SSH credential wins — link straight into it from the card.
        vault_ids_by_account.setdefault(
            cred.vault.account_new_id, cred.id)
    has_uptime = set(UptimeRecord.objects.filter(
        website_new__in=legacy)
        .values_list('website_new_id', flat=True).distinct())

    cards = []
    any_missing_key = False
    any_missing_url = False
    for client in legacy:
        account = client.account
        live_url = client.url or ''
        # A "real" user account is one that can log in — the seed command
        # creates inactive placeholder users with unusable passwords for
        # legacy clients we don't have an email for yet. The login is
        # account-level: one user per customer.
        user = getattr(account, 'user', None)
        has_user = bool(
            user and user.is_active and user.has_usable_password())
        has_email = bool(user and user.email)
        has_live_url = bool(live_url)
        cred_id = vault_ids_by_account.get(client.account_id)
        has_vault_key = cred_id is not None
        has_uptime_data = has_live_url and (client.id in has_uptime)
        # Read the real boolean now that it's backfilled — leave the
        # internal_notes string lookup as a fallback for any rows that
        # haven't been re-saved since the backfill (belt + suspenders).
        # Both are account-level: a tester is a customer, not a site.
        is_tester = bool(getattr(account, 'is_tester', False)) or (
            'Tester: True' in (getattr(account, 'internal_notes', '') or ''))

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
def website_uptime(request, website_id):
    """Uptime detail (per-website) — 30/60/90-day stats, alerts, checks, chart."""
    from clients.account_models import Website
    from clients.models import UptimeAlert, UptimeRecord
    from reporting.uptime_helpers import (
        get_avg_response_time, get_current_status, get_uptime_chart_data,
        get_uptime_percentage,
    )

    website = get_object_or_404(Website, id=website_id)

    chart = get_uptime_chart_data(website, 30)
    max_ms = max((d['avg_response_ms'] or 0 for d in chart), default=0) or 1
    for day in chart:
        day['bar_h'] = round((day['avg_response_ms'] or 0) / max_ms * 100)

    return render(request, 'admin_dashboard/client_uptime.html', _admin_context(
        'clients',
        website=website,
        uptime_status=get_current_status(website),
        uptime_30=get_uptime_percentage(website, 30),
        uptime_60=get_uptime_percentage(website, 60),
        uptime_90=get_uptime_percentage(website, 90),
        avg_response=get_avg_response_time(website, 30),
        open_alerts=UptimeAlert.objects.filter(
            website_new=website, is_resolved=False),
        records=UptimeRecord.objects.filter(website_new=website)[:50],
        chart=chart,
    ))


@admin_required
def website_keywords(request, website_id):
    """Keyword rank tracker for one website + add-keyword form."""
    from clients.account_models import Website
    from reporting.keyword_helpers import build_keyword_rows

    from .forms import KeywordForm

    website = get_object_or_404(Website, id=website_id)
    return render(request, 'admin_dashboard/client_keywords.html', _admin_context(
        'clients',
        website=website,
        keyword_rows=build_keyword_rows(website),
        form=KeywordForm(),
        checked=request.GET.get('checked', ''),
    ))


@admin_required
@require_POST
def keyword_add(request, website_id):
    """Add a tracked keyword for a website."""
    from clients.account_models import Website
    from reporting.keyword_helpers import build_keyword_rows
    from reporting.models import TrackedKeyword

    from .forms import KeywordForm

    website = get_object_or_404(Website, id=website_id)
    form = KeywordForm(request.POST)
    form.instance.website_new = website
    # No legacy bridge. `TrackedKeyword.client` became nullable in
    # clients.0056, so the comment this replaces ("still non-null during
    # the teardown") stopped being true then — the write was keeping a
    # column alive that nothing requires and the drop removes.
    if form.is_valid():
        # client/website aren't form fields, so the unique check is skipped
        # by ModelForm — verify per-website explicitly here.
        if TrackedKeyword.objects.filter(
                website_new=website,
                keyword=form.cleaned_data['keyword']).exists():
            form.add_error(
                'keyword', 'This keyword is already tracked for this website.')
        else:
            form.save()
            return redirect('admin_dashboard:website_keywords',
                            website_id=website.id)
    return render(request, 'admin_dashboard/client_keywords.html', _admin_context(
        'clients',
        website=website,
        keyword_rows=build_keyword_rows(website),
        form=form,
    ))


@admin_required
@require_POST
def keyword_run_check(request, website_id):
    """
    Manual 'Run Check Now'. Live ranks need Google Search Console OAuth
    (Phase 4) — until then this reports the gap rather than failing.
    """
    from clients.account_models import Website
    get_object_or_404(Website, id=website_id)
    return redirect(
        f"{reverse('admin_dashboard:website_keywords', args=[website_id])}"
        f"?checked=gsc_unavailable"
    )


@admin_required
def website_conversions(request, website_id):
    """
    Tier 1 analytics dashboard (per-website) — overview cards, funnel,
    top pages, scroll-depth, click-density grid, recent sessions.
    Backed by `PageSession` (v2 tracker).
    """
    from django.core.serializers.json import DjangoJSONEncoder

    from clients.account_models import Website
    from reporting.analytics_helpers import (
        click_breakdown, conversion_funnel, overview_stats,
        recent_sessions, scroll_distribution, top_pages,
    )
    from reporting.conversion_helpers import conversion_counts
    from reporting.models import ConversionEvent

    website = get_object_or_404(Website, id=website_id)
    breakdown = click_breakdown(website)
    return render(request, 'admin_dashboard/client_conversions.html',
                  _admin_context(
                      'clients',
                      website=website,
                      counts=conversion_counts(website),
                      overview=overview_stats(website),
                      funnel=conversion_funnel(website),
                      top_pages=top_pages(website, limit=10),
                      scroll_dist=scroll_distribution(website),
                      click_sections=breakdown['sections'],
                      click_overlay_json=json.dumps(
                          breakdown['overlay_clicks'],
                          cls=DjangoJSONEncoder),
                      click_top_elements=breakdown['top_elements'],
                      click_total=breakdown['total_clicks'],
                      sessions=recent_sessions(website, limit=50),
                      events=ConversionEvent.objects.filter(
                          website_new=website)[:20],
                  ))


@admin_required
@require_POST
def client_toggle_session_recording(request, client_id):
    """Operator toggle — flip Website.session_recording_enabled.

    The standalone tracker page was retired (the Website detail page's
    Conversion Tracker card is the snippet/recording UI now); this toggle
    is posted from that card with a ?next= back to the website page.
    """
    from clients.account_models import Website
    client = get_object_or_404(Website, id=client_id)
    client.session_recording_enabled = (
        not client.session_recording_enabled)
    client.save(update_fields=[
        'session_recording_enabled', 'updated_at'])
    # Honor ?next= (the inline tracker card on the Website page) as long
    # as it's a safe same-host path.
    from django.utils.http import url_has_allowed_host_and_scheme
    nxt = request.POST.get('next') or ''
    if nxt and url_has_allowed_host_and_scheme(
            nxt, allowed_hosts={request.get_host()},
            require_https=request.is_secure()):
        return redirect(nxt)
    try:
        account = client.migrated_account
    except Exception:
        account = None
    if account is not None:
        return redirect(
            'admin_dashboard:account_detail', account_id=account.id)
    return redirect('admin_dashboard:accounts_list')


@admin_required
@require_POST
def gbp_flag(request, client_id, check_id):
    """Flag a GBP mismatch for fixing — logs an internal changelog note."""
    from clients.models import SiteChangelogEntry
    from clients.website_helpers import primary_website
    from reporting.models import GBPSyncCheck

    check = get_object_or_404(GBPSyncCheck, id=check_id, client_id=client_id)
    check.flagged_for_fix = True
    check.save(update_fields=['flagged_for_fix', 'updated_at'])
    SiteChangelogEntry.objects.create(
        client=check.client,
        website_new=check.website_new or primary_website(check.client),
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


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_reports.py
# ──────────────────────────────────────────────────────────────────────────
from .views_reports import (  # noqa: E402,F401
    _blog_system_prompt,
    _chatbot_for_website,
    _generate_blog_content,
    blog_detail,
    blog_generate,
    blog_list,
    chatbot_conversation,
    chatbot_regenerate_prompt,
    freshness_flag,
    freshness_generate,
    nps_list,
    report_download,
    report_generate_now,
    report_resend,
    reports_list,
    testimonial_mark_received,
    website_chatbot,
    website_freshness,
)


# ────────────────────────────────────────────────────────────────────────────
# Client edit + inline quick-edit
# ────────────────────────────────────────────────────────────────────────────

@admin_required
def client_edit(request, client_id):
    """
    Legacy ClientProfile editor — RETIRED. Account-level fields are edited
    on account_detail, per-website fields on website_detail. Kept as a
    redirect so any lingering links resolve.
    """
    # Inbound links carry a legacy ClientProfile id. Resolved through
    # Account's own legacy_client_profile column, so keeping those links
    # alive costs no legacy read — and an id that is already an Account's
    # works too, for links repointed since.
    from clients.account_models import Account

    account = (Account.objects.filter(id=client_id).first()
               or Account.objects.filter(
                   legacy_client_profile_id=client_id).first())
    if account is not None:
        return redirect(
            'admin_dashboard:account_detail', account_id=account.id)
    return redirect('admin_dashboard:accounts_list')


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_intelligence.py
# ──────────────────────────────────────────────────────────────────────────
from .views_intelligence import (  # noqa: E402,F401
    get_daily_focus,
    intelligence_dashboard,
)


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_upsell.py
# ──────────────────────────────────────────────────────────────────────────
from .views_upsell import (  # noqa: E402,F401
    _intel_transition,
    intelligence_run_for_client,
    intelligence_suggestion_detail,
    intelligence_suggestion_invoice,
    intelligence_suggestion_send,
    intelligence_suggestion_set_status,
    intelligence_suggestions,
)


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_annual_reports.py
# ──────────────────────────────────────────────────────────────────────────
from .views_annual_reports import (  # noqa: E402,F401
    annual_report_detail,
    annual_report_download,
    annual_report_generate,
    annual_report_regenerate,
    annual_report_send,
    annual_reports_list,
)


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_competitor_gaps.py
# ──────────────────────────────────────────────────────────────────────────
from .views_competitor_gaps import (  # noqa: E402,F401
    _competitors_fragment,
    competitor_add,
    competitor_delete,
    competitor_edit,
    competitor_gap_detail,
    competitor_gap_run_now,
    competitor_gaps_list,
    gap_create_suggestion,
)


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


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_recordings.py
# ──────────────────────────────────────────────────────────────────────────
from .views_recordings import (  # noqa: E402,F401
    recording_delete,
    recording_delete_all,
    recording_download,
    recording_replay,
    recordings_list,
)


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
    from clients.models import OnboardingInvoice, OnboardingToken

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
        package_db_slug = ''  # for Website.package
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
                # Map ServiceTier slug → Website.PACKAGE_CHOICES.
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

                # Account first, then its Website. This used to create a
                # ClientProfile and let a post_save signal materialise
                # both behind it — a signal that swallows its own
                # failures, so `ensure_account` had to run afterwards to
                # check whether it had worked. Creating the rows directly
                # removes the signal, the check, and the window in which
                # an admin had billed a client whose account did not
                # exist.
                from clients.account_models import Account, Website

                profile = Account.objects.create(
                    user=user,
                    name=display_name,
                    contact_name=f'{first} {last}'.strip(),
                    phone=phone,
                    city=city,
                    state=state,
                    status='active',
                    onboarding_status='pending_setup',
                    onboarding_complete=False,
                    internal_notes=notes,
                )
                site = Website.objects.create(
                    account=profile,
                    name=display_name,
                    package=package_db_slug,
                    status='active',
                    onboarding_status='pending_intake',
                )

                # OnboardingInvoice row (snapshot of what's being
                # billed — line items render on our /pay/ page and
                # on the PDF receipt).
                invoice = OnboardingInvoice.objects.create(
                    account_new=profile,
                    website_new=site,
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
                OnboardingToken.objects.create(account_new=profile)
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

    # ── A3 — pre-fill from a Lead's opted_in_addons ──
    # Admin lands here from /admin-dashboard/leads/<id>/?action=invoice
    # When the lead was created on /design/schedule/ with addons checked,
    # we want the invoice form to come up with those addons pre-selected
    # and the 10%-off coupon ready to apply on the first month.
    prefill = {}
    addon_opt_in = False
    addon_opt_in_at = None
    addon_opt_in_slugs = []
    lead_id = (request.GET.get('lead') or '').strip()
    if lead_id:
        try:
            from outreach.models import Lead
            lead = Lead.objects.filter(pk=lead_id).first()
        except Exception:
            lead = None
        if lead:
            # Best-effort name split — Lead.attorney_name is a single
            # CharField used for the contact across all lead sources.
            attorney = (lead.attorney_name or '').strip().split(' ', 1)
            prefill = {
                'first_name': attorney[0] if attorney else '',
                'last_name': attorney[1] if len(attorney) > 1 else '',
                'firm_name': lead.firm_name or '',
                'email':     lead.email or '',
                'phone':     lead.phone or '',
                'city':      lead.city or '',
                'state':     lead.state or '',
            }
            # Build_type tag → suggested package
            if 'build_type:essential' in (lead.tags or ''):
                prefill['package'] = 'essential-build' if any(
                    t.slug == 'essential-build' for t in packages
                ) else 'website-essential'
            elif 'build_type:premium' in (lead.tags or ''):
                prefill['package'] = 'premium-build' if any(
                    t.slug == 'premium-build' for t in packages
                ) else 'website-premium'
            opted = list(getattr(lead, 'opted_in_addons', None) or [])
            if opted:
                addon_opt_in = True
                addon_opt_in_at = getattr(lead, 'opted_in_addons_at', None)
                addon_opt_in_slugs = opted
                # Auto-select maintenance + hosting if matching addons exist
                if any('maintenance-essential' in s for s in opted):
                    prefill['maintenance_plan'] = 'maintenance-essentials'
                elif any('maintenance-growth' in s for s in opted):
                    prefill['maintenance_plan'] = 'maintenance-growth'
                elif any('maintenance-dominant' in s for s in opted):
                    prefill['maintenance_plan'] = 'maintenance-dominant'
                if any('hosting' in s for s in opted):
                    prefill['add_hosting'] = '1'

    return render(
        request,
        'admin_dashboard/billing_new_invoice.html',
        _admin_context(
            active='billing',
            packages=packages,
            maintenance_plans=maintenance_plans,
            hosting_tier=hosting_tier,
            form_data=prefill,
            addon_opt_in=addon_opt_in,
            addon_opt_in_at=addon_opt_in_at,
            addon_opt_in_slugs=addon_opt_in_slugs,
            lead_id=lead_id,
        ),
    )


@admin_required
def invoice_detail(request, invoice_id):
    """Per-invoice admin page: status, onboarding state, resend actions."""
    from clients.account_models import Account
    from clients.models import OnboardingInvoice, OnboardingToken

    # Account-scoped: the onboarding invoice covers the engagement and
    # the setup token creates one login.
    profile = get_object_or_404(
        Account.objects.select_related('user'), id=invoice_id)
    token = OnboardingToken.objects.filter(account_new=profile).first()
    invoice = OnboardingInvoice.objects.filter(account_new=profile).first()

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

    from clients.account_models import Account
    from clients.emails import send_onboarding_setup_email

    profile = get_object_or_404(Account, id=invoice_id)
    token = getattr(profile, 'onboarding_token_new', None)
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

    from clients.account_models import Account
    from clients.emails import send_invoice_email
    from clients.models import OnboardingInvoice

    profile = get_object_or_404(Account, id=invoice_id)
    invoice = OnboardingInvoice.objects.filter(
        account_new=profile).first()
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

    from clients.account_models import Account
    from clients.tasks import _send_intake_reminder

    profile = get_object_or_404(Account, id=invoice_id)
    token = getattr(profile, 'onboarding_token_new', None)
    # The intake is per WEBSITE, so "still owes one" is asked of the
    # account's sites. The account-level status could not express a
    # client who had submitted one build's intake but not the other's.
    pending = profile.websites.filter(
        onboarding_status='pending_intake').first()
    if pending is None or token is None:
        _msg.warning(
            request,
            'Client is not in the pending-intake state — '
            'no reminder sent.')
        return redirect(
            'admin_dashboard:invoice_detail', invoice_id=profile.id)
    try:
        _send_intake_reminder(pending, token)
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

    Side effects (delegated to clients.services.change_client_stage):
      - Updates Website.stage + updated_at
      - Logs to ProjectStageLog (immutable audit trail)
      - Sends the branded stage-change email to the client
      - Stamps log.client_notified=True if the email succeeded

    Phase 4.0 — business logic moved into clients.services so the AI
    assistant and this view share one implementation.
    """
    from django.contrib import messages as _msg

    from clients.account_models import Website
    from clients.models import PROJECT_STAGES
    from clients.services import GuardError, change_client_stage

    # A stage belongs to a build, and change_client_stage is site-scoped.
    profile = get_object_or_404(
        Website.objects.select_related('account'), id=client_id)
    new_stage = (request.POST.get('stage') or '').strip()
    note = (request.POST.get('note') or '').strip()
    setter = (request.user.get_full_name()
              or request.user.username
              or 'admin')

    try:
        log, notified = change_client_stage(
            profile, new_stage, set_by=setter, note=note)
    except ValueError as exc:
        _msg.error(request, str(exc))
        return redirect(
            'admin_dashboard:client_detail', client_id=profile.id)
    except GuardError as exc:
        _msg.error(request, str(exc))
        return redirect(
            'admin_dashboard:client_detail', client_id=profile.id)

    if log is None:
        # Idempotent no-op: same stage as before.
        _msg.info(request, 'Stage unchanged.')
        return redirect(
            'admin_dashboard:client_detail', client_id=profile.id)

    label = dict(PROJECT_STAGES).get(new_stage, new_stage)
    _msg.success(
        request,
        f'Project moved to "{label}".'
        + (' Client emailed.' if notified else
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
    from clients.models import OnboardingInvoice, OnboardingToken

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

                # Account + Website created directly — see the paid
                # invoice path above for why the signal detour is gone.
                from clients.account_models import Account, Website

                profile = Account.objects.create(
                    user=user,
                    name=display_name,
                    contact_name=f'{first} {last}'.strip(),
                    phone=phone,
                    city=city,
                    state=state,
                    status='active',
                    onboarding_status='pending_setup',
                    onboarding_complete=False,
                    internal_notes=notes,
                )
                site = Website.objects.create(
                    account=profile,
                    name=display_name,
                    status='active',
                    onboarding_status='pending_intake',
                )

                # Zero-amount paid invoice so the downstream gate
                # treats this client identically to a paid client.
                OnboardingInvoice.objects.create(
                    account_new=profile,
                    website_new=site,
                    line_items=[],
                    total_amount=_Decimal('0'),
                    status='paid',
                    sent_at=timezone.now(),
                    paid_at=timezone.now(),
                )

                token = OnboardingToken.objects.create(
                    account_new=profile)
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
    from clients.account_models import Account

    # The Stripe customer holds the card and the billing relationship,
    # both account-level.
    stripe.api_key = _s.STRIPE_SECRET_KEY
    profile = get_object_or_404(Account, pk=client_id)
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
    from clients.account_models import Account

    profile = get_object_or_404(Account, pk=client_id)
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
    from clients.account_models import Website
    from domains.models import TLD_CHOICES, NamecheapConfig
    from domains.namecheap_client import NamecheapError
    from domains.services import admin_register_domain_for_client

    # A domain points at one site's droplet, so the picker lists sites.
    clients = (
        Website.objects
        .filter(status='active', account__status='active')
        .select_related('account', 'account__user')
        .order_by('account__name', 'name')
    )

    if request.method == 'POST':
        client_id = request.POST.get('client_id', '')
        sld = (request.POST.get('sld') or '').strip().lower()
        tld = (request.POST.get('tld') or '').strip().lower()
        notes = (request.POST.get('notes') or '').strip()

        client = (Website.objects
                  .select_related('account')
                  .filter(pk=client_id)
                  .first())
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
    client_name = owner_label(reg)
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
    # "New / unsigned" badge — builds booked but not yet under signed contract.
    unsigned_website_count = sum(
        1 for w in websites
        if w.lifecycle_status in ('inquiry', 'contract_sent'))

    # Delete-impact summary for the danger card modal — shows the
    # admin exactly what will be wiped before they type the name to
    # confirm. Counts are cheap one-shot aggregates; nothing N+1.
    # Counted off the canonical rows.
    #
    # Every count here came through the account's legacy profile and was
    # left at 0 when there wasn't one — so an account created since the
    # cutover showed "0 tickets, 0 documents, 0 revisions, 0 scans" in
    # the modal whose entire job is telling the admin what they are about
    # to destroy. Under-reporting on a confirm-by-typing-the-name dialog
    # is the worst direction for that to be wrong in.
    from django.db.models import Count, Q

    from clients.account_models import Website
    from clients.models import SupportTicket

    site_ids = [w.pk for w in websites]
    counts = Website.objects.filter(pk__in=site_ids).aggregate(
        documents=Count('documents_new', distinct=True),
        revisions=Count('revisions_new', distinct=True),
        scans=Count('vulnerability_scans_new', distinct=True),
        credentials=Count('vault_credentials_new', distinct=True),
    ) if site_ids else {}

    delete_impact = {
        'websites': len(websites),
        'domains': len(domains),
        'vault_credentials': counts.get('credentials', 0) or 0,
        # Union, not just the account-level link: the portal sets both
        # `account_new` and `website_new`, but counting one of them alone
        # would miss any ticket written with only the other.
        'support_tickets': SupportTicket.objects.filter(
            Q(account_new=account) | Q(website_new__account=account)
        ).distinct().count(),
        'documents': counts.get('documents', 0) or 0,
        'revisions': counts.get('revisions', 0) or 0,
        'scans': counts.get('scans', 0) or 0,
        'active_droplets': 0,
        'active_subscriptions': 0,
    }
    # External-state warnings — these are NOT cascaded by the DB
    # delete, so admin must handle them separately. Surfaced in the
    # modal so the admin doesn't end up with orphan resources.
    for w in websites:
        if w.do_droplet_id:
            delete_impact['active_droplets'] += 1
        if (w.stripe_hosting_subscription_id
                or w.stripe_maintenance_subscription_id):
            delete_impact['active_subscriptions'] += 1

    # ── Contracts card data ──
    # Tier options per service category (for the "send contract" picker)
    # and the account's existing contracts (for the status list). Tiers
    # are read live from the DB so the picker never drifts from pricing.
    from billing.pricing_models import ServiceTier
    contract_tiers = {
        'build': list(ServiceTier.objects.filter(
            category='website_build', is_active=True).order_by('sort_order')),
        'maintenance': list(ServiceTier.objects.filter(
            category='maintenance', is_active=True).order_by('sort_order')),
        'social': list(ServiceTier.objects.filter(
            category='social_media', is_active=True).order_by('sort_order')),
    }
    contracts = list(
        account.contracts.all().prefetch_related('services')
        .order_by('-created_at')[:10])
    contract_sign_base = request.build_absolute_uri('/portal/contract/')

    # ── Scheduling, add-on opt-ins, and payments ──
    # Leads/calls are keyed by email (no FK between Lead and Account).
    acct_email = (user.email if user else '') or ''
    scheduled_calls = []
    addon_optins = []
    if acct_email:
        try:
            from scheduler.models import ScheduledCall
            scheduled_calls = list(
                ScheduledCall.objects
                .filter(customer_email__iexact=acct_email)
                .order_by('-starts_at')[:10])
        except Exception:
            scheduled_calls = []
        try:
            from billing.pricing_models import ServiceTier
            from outreach.models import Lead
            slug_to_name = dict(
                ServiceTier.objects.values_list('slug', 'name'))
            for lead in (Lead.objects
                         .filter(email__iexact=acct_email)
                         .exclude(opted_in_addons=[])
                         .order_by('-opted_in_addons_at')):
                for slug in (lead.opted_in_addons or []):
                    addon_optins.append({
                        'slug': slug,
                        'name': slug_to_name.get(slug, slug),
                        'at': lead.opted_in_addons_at,
                    })
        except Exception:
            addon_optins = []

    # Payments: onboarding invoice + out-of-scope mini invoices + the
    # recurring service subscriptions (maintenance/social).
    # Off the account. Read through the legacy profile these were both
    # empty for an account without one, so a client created since the
    # cutover showed no invoices on a page whose job is listing them.
    onboarding_invoice = account.onboarding_invoices_new.order_by(
        '-created_at').first()
    try:
        mini_invoices = list(
            account.mini_invoices_new.all().order_by('-created_at')[:10])
    except Exception:
        mini_invoices = []
    try:
        maintenance_plans = list(account.maintenance_plans.all())
        social_plans = list(account.social_media_plans.all())
    except Exception:
        maintenance_plans, social_plans = [], []

    # Sites entitled to GBP management. Filtered here rather than in the
    # template: `has_gbp_features` is a method, so a template-side filter
    # would have to be written as a loop, and the surrounding card markup
    # can only be emitted once the filtered list is known to be non-empty.
    gbp_websites = [w for w in websites if w.has_gbp_features()]

    return render(
        request, 'admin_dashboard/account_detail.html',
        _admin_context(
            'accounts',
            account=account,
            user=user,
            sections=sections,
            websites=websites,
            gbp_websites=gbp_websites,
            domains=domains,
            delete_impact=delete_impact,
            contract_tiers=contract_tiers,
            contracts=contracts,
            contract_sign_base=contract_sign_base,
            unsigned_website_count=unsigned_website_count,
            scheduled_calls=scheduled_calls,
            addon_optins=addon_optins,
            onboarding_invoice=onboarding_invoice,
            mini_invoices=mini_invoices,
            maintenance_plans=maintenance_plans,
            social_plans=social_plans,
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
def account_set_comp_tier(request, account_id):
    """Set ONE of three independent comp buckets on the linked
    ClientProfile:

      bucket=build       -> comp_build_package       (essential/premium)
      bucket=maintenance -> comp_maintenance_package (essentials/growth/dominant/moonieful)
      bucket=social      -> comp_social_tier (social-basic/standard/full)
                            ALSO ensures an active SocialMediaPlan on
                            the Account so the Social manager picks it
                            up. Clearing the social tier deactivates
                            the auto-created plan.
      bucket=notes       -> comp_notes (shared across all buckets)

    Each card on the Account detail page posts its own form so the
    other two buckets are not touched.
    """
    from django.contrib import messages as _messages

    from clients.account_models import Account

    account = get_object_or_404(Account, id=account_id)

    # Comps are written to the Account, which is where the entitlement
    # check reads them (Website.active_tiers). They used to be written to
    # the legacy profile and only reached the Account when someone ran
    # backfill_account_data by hand — so a tier comped through this UI did
    # not actually grant the feature. It also refused outright for an
    # account with no legacy profile, which is every account created after
    # the cutover.
    bucket = (request.POST.get('bucket') or '').strip()

    if bucket == 'build':
        value = (request.POST.get('comp_build_package') or '').strip()
        valid = {k for k, _ in Account.BUILD_COMP_CHOICES}
        if value and value not in valid:
            _messages.error(request, f'Invalid build comp: {value!r}')
            return redirect(
                'admin_dashboard:account_detail',
                account_id=account.id)
        account.comp_build_package = value
        account.save(update_fields=[
            'comp_build_package', 'updated_at'])
        if value:
            label = dict(Account.BUILD_COMP_CHOICES).get(
                value, value)
            _messages.success(
                request,
                f'Comped {account.name} on the {label} build.')
        else:
            _messages.success(
                request,
                f'Cleared the build comp on {account.name}.')

    elif bucket == 'maintenance':
        value = (request.POST.get(
            'comp_maintenance_package') or '').strip()
        valid = {k for k, _ in Account.MAINTENANCE_COMP_CHOICES}
        if value and value not in valid:
            _messages.error(
                request, f'Invalid maintenance comp: {value!r}')
            return redirect(
                'admin_dashboard:account_detail',
                account_id=account.id)
        account.comp_maintenance_package = value
        account.save(update_fields=[
            'comp_maintenance_package', 'updated_at'])
        if value:
            label = dict(
                Account.MAINTENANCE_COMP_CHOICES
            ).get(value, value)
            _messages.success(
                request,
                f'Comped {account.name} on the {label} plan.')
        else:
            _messages.success(
                request,
                f'Cleared the maintenance comp on {account.name}.')

    elif bucket == 'social':
        value = (request.POST.get('comp_social_tier') or '').strip()
        valid = {k for k, _ in Account.SOCIAL_COMP_CHOICES}
        if value and value not in valid:
            _messages.error(request, f'Invalid social comp: {value!r}')
            return redirect(
                'admin_dashboard:account_detail',
                account_id=account.id)
        account.comp_social_tier = value
        account.save(update_fields=[
            'comp_social_tier', 'updated_at'])

        # Side-effect: comping the social tier provisions an active
        # SocialMediaPlan PER WEBSITE on the Account (since plans are
        # per-business now). If the Account has no Websites yet, we
        # create a single legacy account-wide row (website=NULL) so
        # the Social Media manager has something to surface.
        # Clearing the comp pauses every auto-created row without
        # destroying any per-channel data.
        from clients.service_models import SocialMediaPlan
        websites = list(account.websites.all())
        targets = websites if websites else [None]
        if value:
            for w in targets:
                plan = SocialMediaPlan.objects.filter(
                    account=account, website=w,
                ).first()
                if plan is None:
                    SocialMediaPlan.objects.create(
                        account=account,
                        website=w,
                        tier_slug=value,
                        status='active',
                    )
                else:
                    plan.tier_slug = value
                    plan.status = 'active'
                    plan.save(update_fields=[
                        'tier_slug', 'status', 'updated_at'])
            label = dict(Account.SOCIAL_COMP_CHOICES).get(
                value, value)
            count = len(targets)
            _messages.success(
                request,
                f'Comped {account.name} on {label} across '
                f'{count} business{"es" if count != 1 else ""}. '
                f'Social Media manager will treat them as active.')
        else:
            # Pause only auto-created (no-Stripe-sub) rows. Paid plans
            # keep their Stripe-driven status untouched.
            paused = SocialMediaPlan.objects.filter(
                account=account, status='active',
                stripe_subscription_id='',
            ).update(status='paused')
            if paused:
                _messages.success(
                    request,
                    f'Cleared the social comp on {account.name} '
                    f'({paused} plan{"s" if paused != 1 else ""} paused).')
            else:
                _messages.success(
                    request,
                    f'Cleared the social comp on {account.name}.')

    elif bucket == 'notes':
        account.comp_notes = (
            request.POST.get('comp_notes') or '').strip()
        account.save(update_fields=['comp_notes', 'updated_at'])
        _messages.success(request, 'Comp note saved.')

    else:
        _messages.error(request, 'Missing or unknown comp bucket.')

    return redirect(
        'admin_dashboard:account_detail', account_id=account.id)


# `_ensure_client_profile` lived here. It resolved (and back-filled) the
# Account's legacy ClientProfile purely so a Contract could be hung off
# it. Contract now carries `account` and `website_new`, and the contract
# text reads the party name off whichever owner it is handed, so there is
# nothing left for it to do.


# Map a website-build ServiceTier slug to the Contract.package code so the
# legacy build-invoice + PDF code paths keep working on combined contracts.
_BUILD_SLUG_TO_PACKAGE = {
    'website-essential': 'essential_build',
    'website-premium': 'premium_build',
}
# Reverse: Website.package code → build ServiceTier slug.
_PACKAGE_TO_BUILD_SLUG = {v: k for k, v in _BUILD_SLUG_TO_PACKAGE.items()}


@admin_required
@require_POST
def website_send_contract(request, website_id):
    """Create + email the build contract for a single Website (one click —
    uses the build tier captured on the Website at booking). The Contract is
    tied to `website_new`; signing flows into deposit → setup → intake.
    """
    from decimal import Decimal, InvalidOperation

    from django.contrib import messages as _messages

    from billing.pricing_models import ServiceTier
    from clients.account_models import Website
    from clients.contract_template import generate_combined_contract_text
    from clients.emails import send_contract_ready_email
    from clients.models import Contract, ContractService

    website = get_object_or_404(
        Website.objects.select_related('account'), id=website_id)
    account = website.account
    if account is None:
        _messages.error(
            request, 'This website has no account — cannot send a contract.')
        return redirect('admin_dashboard:website_detail', website_id=website.id)
    profile = account

    slug = _PACKAGE_TO_BUILD_SLUG.get(website.package or '')
    tier = (ServiceTier.objects.filter(slug=slug, category='website_build',
                                       is_active=True).first()
            if slug else None)

    # A custom price posted with the form (or already stored on the
    # website) replaces the tier price, and stands in for the tier
    # entirely when there isn't one. Without this a one-off rate had no
    # route to the contract → 50% deposit → intake flow at all: the only
    # alternative was a flat pay-in-full invoice.
    custom_raw = (request.POST.get('custom_build_price') or '').strip()
    custom_price = None
    if custom_raw:
        try:
            custom_price = Decimal(custom_raw)
            if custom_price <= 0:
                raise InvalidOperation()
        except (InvalidOperation, ValueError, TypeError):
            _messages.error(
                request, 'Custom build price must be a positive number.')
            return redirect(
                'admin_dashboard:website_detail', website_id=website.id)
    elif website.custom_build_price:
        custom_price = Decimal(website.custom_build_price)

    if tier is None and custom_price is None:
        _messages.error(
            request,
            'Set this website’s package to Essential or Premium build, or '
            'enter a custom build price, before sending a contract.')
        return redirect('admin_dashboard:website_detail', website_id=website.id)

    # Platform decides whether a Droplet is provisioned at intake, and
    # which scope wording the contract carries.
    platform = (request.POST.get('build_platform')
                or website.build_platform or 'custom')
    if platform not in dict(Website.BUILD_PLATFORM_CHOICES):
        platform = 'custom'

    price = custom_price if custom_price is not None else Decimal(tier.price)
    deposit = (price / 2).quantize(Decimal('0.01'))
    weeks = (tier.timeline_weeks if tier is not None else 0) or 4
    label = (tier.name if tier is not None else 'Custom Website Build')

    fields = []
    if custom_price is not None and website.custom_build_price != custom_price:
        website.custom_build_price = custom_price
        fields.append('custom_build_price')
    if website.build_platform != platform:
        website.build_platform = platform
        fields.append('build_platform')
    if fields:
        website.save(update_fields=fields + ['updated_at'])

    contract = Contract.objects.create(
        account=account, website_new=website,
        package=website.package,
        build_price=price,
        deposit_amount=deposit,
        timeline_weeks=weeks,
        contract_text=generate_combined_contract_text(
            profile, [{'service_type': 'build', 'tier': tier,
                       'price': price, 'name': label,
                       'platform': platform, 'weeks': weeks}]),
    )
    ContractService.objects.create(
        contract=contract, service_type='build',
        tier_slug=(tier.slug if tier is not None else 'custom'),
        tier_name=label, price=price,
        deposit_amount=deposit,
        is_recurring=False, billing_interval='')

    website.lifecycle_status = 'contract_sent'
    website.save(update_fields=['lifecycle_status', 'updated_at'])

    sign_url = request.build_absolute_uri(
        reverse('clients:contract_sign', args=[contract.contract_token]))
    try:
        send_contract_ready_email(contract, sign_url)
    except Exception:
        logger.exception('contract-ready email failed for %s', contract.pk)

    # An owner with no address makes send_mail a silent no-op, and the
    # operator was told "Contract sent" regardless. Say so instead — the
    # signing link is in the message either way.
    from clients.emails import _contract_owner, _recipient
    to = _recipient(_contract_owner(contract))
    if to:
        _messages.success(
            request,
            f'Contract sent to {to[0]} for {website.name} '
            f'({label} — ${price:,.2f}, ${deposit:,.2f} deposit). '
            f'Signing link: {sign_url}')
    else:
        _messages.warning(
            request,
            f'Contract created for {website.name} ({label} — ${price:,.2f}, '
            f'${deposit:,.2f} deposit) but NOT emailed — this account has no '
            f'email address on file. Send the link manually: {sign_url}')
    return redirect('admin_dashboard:website_detail', website_id=website.id)


def _regenerate_contract_text(contract):
    """Rebuild a contract's text from its ContractService rows.

    Mirrors what website_send_contract / account_send_contract produce,
    so "Reset to template" gives back exactly the generated wording —
    including the custom price, name and platform for a build with no
    ServiceTier behind it. Returns None if there is nothing to rebuild.
    """
    from billing.pricing_models import ServiceTier
    from clients.contract_template import generate_combined_contract_text

    website = contract.website_new
    services = []
    for svc in contract.services.all():
        tier = ServiceTier.objects.filter(slug=svc.tier_slug).first()
        entry = {'service_type': svc.service_type, 'tier': tier}
        if svc.service_type == 'build':
            entry.update({
                'price': svc.price,
                'name': svc.tier_name or 'Custom Website Build',
                'platform': getattr(website, 'build_platform', 'custom'),
                'weeks': contract.timeline_weeks or 4,
            })
        services.append(entry)
    if not services:
        return None
    owner = contract.account or contract.client
    if owner is None:
        return None
    return generate_combined_contract_text(owner, services)


@admin_required
def contract_edit(request, contract_id):
    """Read and edit a contract's text before it is signed.

    The generated template is the starting point — this is for the
    per-client wording that a generator can't know about. Everything on
    the page is the real document: what is saved here is exactly what
    the client reads, signs, and gets hashed into the audit trail.

    A SIGNED contract is read-only. `signed_content_hash` is a SHA-256
    of the text as displayed at signing; editing afterwards would break
    the one thing that proves the document wasn't altered after the
    fact, which is the whole ESIGN/UETA defence.
    """
    from django.contrib import messages as _messages

    from clients.models import Contract

    contract = get_object_or_404(
        Contract.objects.select_related('account', 'website_new'),
        id=contract_id)

    if request.method == 'POST':
        if contract.signed:
            _messages.error(
                request,
                'This contract was signed on '
                f'{contract.signed_at:%b %d, %Y} and can no longer be '
                'edited — the signature is bound to the exact text. '
                'Send a new contract instead.')
            return redirect(
                'admin_dashboard:contract_edit', contract_id=contract.id)

        if (request.POST.get('action') or '').strip() == 'regenerate':
            text = _regenerate_contract_text(contract)
            if not text:
                _messages.error(
                    request,
                    'Could not rebuild this contract — it has no service '
                    'lines to generate from.')
            else:
                contract.contract_text = text
                contract.save(update_fields=['contract_text', 'updated_at'])
                _messages.success(
                    request, 'Contract reset to the generated template.')
        else:
            text = (request.POST.get('contract_text') or '').strip()
            if not text:
                _messages.error(request, 'Contract text cannot be empty.')
            else:
                contract.contract_text = text
                contract.save(update_fields=['contract_text', 'updated_at'])
                _messages.success(
                    request,
                    'Contract saved. The client sees this immediately — '
                    'the signing link does not need resending.')
        return redirect(
            'admin_dashboard:contract_edit', contract_id=contract.id)

    sign_url = request.build_absolute_uri(
        reverse('clients:contract_sign', args=[contract.contract_token]))
    return render(
        request, 'admin_dashboard/contract_edit.html',
        _admin_context(
            'accounts',
            contract=contract,
            services=list(contract.services.all()),
            sign_url=sign_url,
        ))


@admin_required
@require_POST
def website_add_plan(request, website_id):
    """Operator: attach a maintenance/social plan to a Website with an
    optional custom discount (first-month-only or forever). Auto-charges the
    card on file, or emails a payment link (plan tagged Awaiting payment).
    """
    from django.contrib import messages as _messages

    from billing.plan_billing import start_website_plan
    from clients.account_models import Website

    website = get_object_or_404(Website, id=website_id)
    service_type = (request.POST.get('service_type') or '').strip()
    tier_slug = (request.POST.get('tier_slug') or '').strip()
    duration = (request.POST.get('discount_duration') or 'once').strip()
    pct_raw = (request.POST.get('discount_percent') or '').strip()

    if service_type not in ('maintenance', 'social') or not tier_slug:
        _messages.error(request, 'Choose a plan type and a tier.')
        return redirect('admin_dashboard:website_detail', website_id=website.id)

    discount = None
    if pct_raw:
        try:
            discount = max(0, min(100, int(pct_raw)))
        except ValueError:
            discount = None

    plan = start_website_plan(
        website, service_type, tier_slug,
        discount_percent=discount, discount_duration=duration)

    if plan is None:
        _messages.error(
            request,
            'Could not start the plan — confirm the tier has a Stripe price '
            '(run sync_stripe_products).')
    elif plan.status == 'awaiting_payment':
        _messages.success(
            request,
            'Plan created — no card on file, so a payment link was emailed. '
            'It activates once they pay.')
    else:
        _messages.success(request, 'Plan started and charged to the card on file.')
    return redirect('admin_dashboard:website_detail', website_id=website.id)


def _issue_website_final_invoice(website):
    """On → Pre-Launch: set up the remaining-balance payment on OUR own
    /pay/ page (not a Stripe-hosted page), store the on-site pay link for the
    portal button, and email our branded notice. Best-effort."""
    from billing.stripe_helpers import start_contract_final_payment
    from clients.models import Contract

    if website.payment_status == 'fully_paid':
        return
    # The signed contract is the only thing this needs. It also required a
    # legacy ClientProfile and returned early without one, so moving a
    # canonical-only client to Pre-Launch raised no final invoice, left
    # `final_invoice_url` empty, and sent no email — and the launch gate
    # then blocks on a payment that was never asked for.
    contract = (Contract.objects.filter(website_new=website, signed=True)
                .order_by('-created_at').first())
    if contract is None:
        return
    try:
        invoice = start_contract_final_payment(contract)
    except Exception:
        logger.exception(
            'final invoice failed for website %s', website.pk)
        return
    if invoice is None:
        return
    # On-site /pay/<token>/ link — everything stays on our domain.
    pay_url = invoice.get_pay_url()
    website.final_invoice_url = pay_url
    website.save(update_fields=['final_invoice_url', 'updated_at'])
    try:
        from clients.emails import send_final_invoice_email
        send_final_invoice_email(website, contract, pay_url)
    except Exception:
        logger.exception(
            'final invoice email failed for website %s', website.pk)


def _start_website_live_plans(website):
    """On → Live: start the maintenance/social plans the client opted into
    (10% off first month honoured). No opt-in → nothing created."""
    from billing.plan_billing import start_website_plan

    website.lifecycle_status = 'live'
    website.save(update_fields=['lifecycle_status', 'updated_at'])
    if website.opted_in_maintenance_tier:
        start_website_plan(
            website, 'maintenance', website.opted_in_maintenance_tier,
            honor_optin_10=True)
    if website.opted_in_social_tier:
        start_website_plan(
            website, 'social', website.opted_in_social_tier,
            honor_optin_10=True)


@admin_required
@require_POST
def account_send_contract(request, account_id):
    """Create + email a combined services contract from the Account page.

    The operator multiselects any of three services (website development,
    maintenance, social media) and picks a tier for each. We build ONE
    Contract with one ContractService row per selected service, render the
    combined agreement text, and email the client the signing link. Signing
    is handled by the existing token-gated ``clients:contract_sign`` view.
    """
    from decimal import Decimal

    from django.contrib import messages as _messages

    from billing.pricing_models import ServiceTier
    from clients.account_models import Account
    from clients.contract_template import generate_combined_contract_text
    from clients.emails import send_contract_ready_email
    from clients.models import Contract, ContractService

    account = get_object_or_404(Account, id=account_id)

    if account.user is None:
        _messages.error(
            request,
            'This account has no user on file — cannot create a contract.')
        return redirect(
            'admin_dashboard:account_detail', account_id=account.id)
    profile = account

    # (service_type, checkbox field, tier-select field, ServiceTier category)
    service_form = [
        ('build', 'svc_build', 'tier_build', 'website_build'),
        ('maintenance', 'svc_maintenance', 'tier_maintenance', 'maintenance'),
        ('social', 'svc_social', 'tier_social', 'social_media'),
    ]
    selected = []
    for service_type, check_field, tier_field, category in service_form:
        if request.POST.get(check_field) != 'on':
            continue
        slug = (request.POST.get(tier_field) or '').strip()
        if not slug:
            _messages.error(
                request,
                f'Select a tier for the {service_type} service before sending.')
            return redirect(
                'admin_dashboard:account_detail', account_id=account.id)
        tier = ServiceTier.objects.filter(
            slug=slug, category=category, is_active=True).first()
        if tier is None:
            _messages.error(
                request, f'Unknown {service_type} tier: {slug!r}.')
            return redirect(
                'admin_dashboard:account_detail', account_id=account.id)
        selected.append((service_type, tier))

    if not selected:
        _messages.error(
            request, 'Select at least one service for the contract.')
        return redirect(
            'admin_dashboard:account_detail', account_id=account.id)

    build_tier = next((t for st, t in selected if st == 'build'), None)

    contract = Contract.objects.create(
        account=account,
        package=(_BUILD_SLUG_TO_PACKAGE.get(build_tier.slug, '')
                 if build_tier else ''),
        build_price=Decimal(build_tier.price) if build_tier else None,
        deposit_amount=((Decimal(build_tier.price) / 2).quantize(Decimal('0.01'))
                        if build_tier else None),
        timeline_weeks=(build_tier.timeline_weeks or 4) if build_tier else 0,
        contract_text=generate_combined_contract_text(
            profile, [{'service_type': st, 'tier': t} for st, t in selected]),
    )
    for service_type, tier in selected:
        deposit = (
            (Decimal(tier.price) / 2).quantize(Decimal('0.01'))
            if service_type == 'build' else None)
        ContractService.objects.create(
            contract=contract,
            service_type=service_type,
            tier_slug=tier.slug,
            tier_name=tier.name,
            price=Decimal(tier.price),
            deposit_amount=deposit,
            is_recurring=bool(tier.is_recurring),
            billing_interval=tier.billing_interval or '',
        )

    sign_url = request.build_absolute_uri(
        reverse('clients:contract_sign', args=[contract.contract_token]))
    try:
        send_contract_ready_email(contract, sign_url)
    except Exception:
        logger.exception(
            'Contract-ready email failed for contract %s', contract.pk)

    _messages.success(
        request,
        f'Contract created for {account.name} covering '
        f'{contract.service_summary}. Signing link: {sign_url}')
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

    from clients.legacy_teardown import delete_mirror_for

    user = account.user
    label = account.name

    try:
        with transaction.atomic():
            # Order matters for clean cascade:
            # 1. Account delete → Websites, Domains, vault_credentials,
            #    onboarding_token, onboarding_invoice, etc. (everything
            #    with FK to Account with on_delete=CASCADE)
            # 2. Legacy mirror → vault, intake, tickets, scans, reports,
            #    freshness, NPS, chatbot. `legacy_client_profile` is
            #    SET_NULL, so the profile survives step 1 and has to be
            #    finished off explicitly. Lives in clients.legacy_teardown
            #    so the one remaining legacy touch is in a module the drop
            #    change deletes whole, rather than buried in a view.
            # 3. User delete → auth row gone so the email is free to
            #    re-onboard.
            # Mirror first: the FK is SET_NULL, so after `account.delete()`
            # the link is already gone in the database and only Django's
            # in-memory cache still holds it — not something to build a
            # destructive path on. Deleting the profile first cascades the
            # rows it owns; `account.delete()` then takes the rest.
            delete_mirror_for(account)
            account.delete()
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
    lifecycle = (request.GET.get('lifecycle') or '').strip()

    websites = Website.objects.select_related('account').order_by('name')
    if query:
        websites = websites.filter(
            Q(name__icontains=query)
            | Q(url__icontains=query)
            | Q(slug__icontains=query))
    if stage:
        websites = websites.filter(stage=stage)
    if lifecycle == 'unsigned':
        # "New / unsigned" convenience bucket — booked but not yet signed.
        websites = websites.filter(
            lifecycle_status__in=['inquiry', 'contract_sent'])
    elif lifecycle:
        websites = websites.filter(lifecycle_status=lifecycle)

    return render(request, 'admin_dashboard/websites_list.html', _admin_context(
        'accounts',
        websites=list(websites),
        query=query,
        active_stage=stage,
        active_lifecycle=lifecycle,
        stages=Website._meta.get_field('stage').choices,
        lifecycle_choices=Website._meta.get_field('lifecycle_status').choices,
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
            'ga4_measurement_id',
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
    intake_response = getattr(website, 'intake_new', None)
    intake_needs_admin_complete = (
        website.onboarding_status == 'pending_intake'
        or (intake_response is not None and not intake_response.completed))

    # Freshness report drives the count badge on the monitoring tools
    # card. Per site — read off the legacy profile it was the account's
    # report, so on a two-build account both sites showed the same badge.
    freshness_report = website.freshness_reports_new.first()

    # ── Contract (per-website) + plan management ──
    from billing.pricing_models import ServiceTier
    from clients.models import Contract
    website_contracts = list(
        Contract.objects.filter(website_new=website)
        .order_by('-created_at')[:5])
    contract_sign_base = request.build_absolute_uri('/portal/contract/')
    # A custom price is a valid basis for a contract on its own — the
    # form below lets one be entered, so a website with no package tier
    # is no longer a dead end.
    can_send_contract = (website.package in _PACKAGE_TO_BUILD_SLUG
                         or bool(website.custom_build_price))
    # Summary-strip flags — cheap derivations off the already-fetched list.
    contract_signed = any(c.signed for c in website_contracts)
    contract_pending = any(not c.signed for c in website_contracts)
    maintenance_tiers = list(ServiceTier.objects.filter(
        category='maintenance', is_active=True).order_by('sort_order'))
    social_tiers = list(ServiceTier.objects.filter(
        category='social_media', is_active=True).order_by('sort_order'))
    website_plans = (
        list(website.maintenance_plans.all())
        + list(website.social_media_plans.all()))

    # ── Tracker snippet (inline on this page) ──
    # Mirrors client_tracker: one snippet forever, session recording
    # (Tier 2) gated server-side. Resolves via the legacy ClientProfile
    # because the tracker + conversion/session data still live there
    # (per-website split is Phase D). Degrades to None when the website
    # has no legacy mirror yet.
    # The snippet carries the WEBSITE id. `_website_for_tracker_id`
    # already accepts either form — a Website id, or a legacy profile id
    # for the snippets sitting in client HTML we cannot redeploy — but
    # this generator only ever emitted the legacy one, and emitted
    # nothing at all when there was no legacy profile. A client created
    # since the cutover was shown no install snippet, so their site was
    # never instrumented and their conversions page stayed empty.
    base_url = settings.SITE_BASE_URL
    tracker_snippet = (
        f'<script src="{base_url}/static/js/aspired-tracker.js" '
        f'data-aspired-client="{website.id}" defer></script>')
    tracker_recording_active = bool(website.session_recording_enabled)
    tracker_recording_included = False
    tracker_last_event = None
    tracker_last_session = None
    try:
        from billing.pricing_models import AddonPricing
        addon = AddonPricing.objects.filter(
            slug='addon-session-recording').first()
        if addon:
            tracker_recording_included = addon.is_included_for(
                website.package)
    except Exception:
        tracker_recording_included = False
    try:
        from reporting.models import ConversionEvent, PageSession
        tracker_last_event = ConversionEvent.objects.filter(
            website_new=website).first()
        tracker_last_session = PageSession.objects.filter(
            website_new=website).first()
    except Exception:
        pass
    try:
        from reporting.models import SessionRecording
        tracker_recordings_count = SessionRecording.objects.filter(
            website_new=website).count()
    except Exception:
        tracker_recordings_count = 0

    # ── Monitoring & reporting accordion tools ──
    # Each lazy-loads its existing admin page in an iframe (?embed=1).
    # Session Recordings deliberately omitted — it's a media list with
    # its own page, linked from the Conversion Tracker card instead.
    #
    # Every URL below is already keyed on `website.id`; the whole list was
    # nonetheless gated on the legacy profile existing, so a client
    # created since the cutover saw an empty Monitoring accordion while
    # all six pages worked perfectly if you typed the URL.
    fresh_badge = ''
    if freshness_report and freshness_report.pages_needing_update:
        fresh_badge = f'{freshness_report.pages_needing_update} flagged'
    mon_tools = [
        {'label': 'Uptime',
         'url': reverse('admin_dashboard:website_uptime', args=[website.id])},
        {'label': 'Keywords',
         'url': reverse('admin_dashboard:website_keywords', args=[website.id])},
        {'label': 'Conversions',
         'url': reverse('admin_dashboard:website_conversions', args=[website.id])},
        {'label': 'Content Freshness', 'badge': fresh_badge,
         'url': reverse('admin_dashboard:website_freshness', args=[website.id])},
        {'label': 'Chatbot',
         'url': reverse('admin_dashboard:website_chatbot', args=[website.id])},
        {'label': 'Changelog',
         'url': reverse('admin_dashboard:website_changelog', args=[website.id])},
    ]

    return render(
        request, 'admin_dashboard/website_detail.html',
        _admin_context(
            'accounts',
            website=website,
            account=website.account,
            domains=domains,
            website_contracts=website_contracts,
            contract_sign_base=contract_sign_base,
            can_send_contract=can_send_contract,
            contract_signed=contract_signed,
            contract_pending=contract_pending,
            maintenance_tiers=maintenance_tiers,
            social_tiers=social_tiers,
            website_plans=website_plans,
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
            freshness_report=freshness_report,
            tracker_snippet=tracker_snippet,
            tracker_recording_active=tracker_recording_active,
            tracker_recording_included=tracker_recording_included,
            tracker_last_event=tracker_last_event,
            tracker_last_session=tracker_last_session,
            tracker_recordings_count=tracker_recordings_count,
            mon_tools=mon_tools,
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

    # 2. The site's IntakeResponse. This reached the intake through the
    #    account's legacy profile, so for a client with no legacy row the
    #    form was never marked complete — and on a two-build account it
    #    marked whichever intake hung off the profile, not this site's.
    intake = getattr(website, 'intake_new', None)
    if intake is not None and not intake.completed:
        intake.completed = True
        if intake.completed_at is None:
            intake.completed_at = timezone.now()
        intake.save(update_fields=[
            'completed', 'completed_at', 'updated_at'])

    # Step 3 used to flip the legacy profile's onboarding gate, because
    # `@client_required` read it to decide whether to bounce a logged-in
    # client to /portal/intake/. That decorator reads
    # `Account.onboarding_status` and `Website.onboarding_status` now, and
    # step 1 above already sets the site's — so writing the legacy column
    # only kept a soon-to-be-dropped mirror warm.

    # 3. Audit trail — same pattern as a stage change.
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

    Side effects (match the legacy client_change_stage view so both
    paths produce identical state during the Phase C transition):
      - Updates Website.stage + updated_at.
      - Mirrors to ClientProfile.stage so the client portal (which
        still reads the legacy field) shows the change. Drops once
        Phase D removes ClientProfile.
      - Writes a WebsiteStageLog row (new audit trail).
      - Writes a ProjectStageLog row against the legacy CP so the
        portal Activity Log still shows the transition.
      - Sends the branded stage-change email to the client unless
        the new stage has no copy (e.g. 'intake').
    """
    from django.contrib import messages

    from clients.account_models import Website, WebsiteStageLog
    from clients.emails import send_stage_change_email
    from clients.models import ProjectStageLog

    website = get_object_or_404(Website, id=website_id)
    new_stage = (request.POST.get('stage') or '').strip()
    note = (request.POST.get('note') or '').strip()
    setter = (request.user.get_full_name()
              or request.user.username
              or 'admin')

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

    # 1. Write to Website (new model).
    website.stage = new_stage
    website.save(update_fields=['stage', 'updated_at'])

    # 1b. Stage-driven billing + sales-lifecycle.
    #   → pre_launch: send the remaining build-balance invoice (if owed).
    #   → live: start the maintenance/social plans they opted into.
    #   else: builds in progress show as 'in_build' once past intake.
    if new_stage == 'pre_launch':
        _issue_website_final_invoice(website)
    elif new_stage == 'live':
        _start_website_live_plans(website)
    elif new_stage != 'intake' and website.lifecycle_status in (
            'deposit_paid', 'contract_signed', 'in_build'):
        website.lifecycle_status = 'in_build'
        website.save(update_fields=['lifecycle_status', 'updated_at'])

    # Step 2 used to mirror the stage onto the legacy ClientProfile
    # "because the client portal still reads from CP". It does not: the
    # portal reads `request.website`, so that mirror updated a column with
    # no reader — and on a two-build account it overwrote the account-wide
    # copy with whichever site moved last.

    # 2. Audit trail. One log, not two.
    #
    # This also wrote a ProjectStageLog "so the client portal Activity Log
    # shows the transition" — but the portal reads `stage_logs`, which is
    # WebsiteStageLog. The legacy mirror had no reader, and it was skipped
    # entirely for a client with no legacy profile, so the two logs
    # disagreed for exactly the clients created since the cutover.
    stage_log = WebsiteStageLog.objects.create(
        website=website,
        from_stage=from_stage,
        to_stage=new_stage,
        note=note,
        set_by=setter,
        client_notified=False,
    )

    # 4. Stage-change email — best-effort. Email failure does not
    #    roll back the stage save.
    # Addressed to the SITE being transitioned, unconditionally.
    #
    # This was `if legacy_cp is not None`, so a client with no legacy
    # profile — every client created since the cutover — was silently
    # never told their project had moved. No error, no log line: the
    # branch simply did not run, and the success message still said the
    # stage had moved. `send_stage_change_email` reads `stage`,
    # `staging_url` and `maintenance_active`, all of which are Website
    # fields, and resolves the address through `owner_recipient`.
    notify_ok = False
    try:
        send_stage_change_email(website, new_stage)
        notify_ok = True
    except Exception:
        logger.exception('stage-change email failed for %s', website.pk)
    if notify_ok:
        stage_log.client_notified = True
        stage_log.notification_sent_at = timezone.now()
        stage_log.save(update_fields=[
            'client_notified', 'notification_sent_at', 'updated_at'])

    messages.success(
        request,
        f'Stage moved {from_stage} → {new_stage}.'
        + (' Client emailed.' if notify_ok
           else ' (Client email skipped — no copy or no legacy CP.)'))
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


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_dmarc.py
# ──────────────────────────────────────────────────────────────────────────
from .views_dmarc import (  # noqa: E402,F401
    _format_seconds,
    dmarc_dashboard,
    dmarc_upload,
    redis_monitor,
)


# ────────────────────────────────────────────────────────────────────────────
# Outreach approvals queue
# ────────────────────────────────────────────────────────────────────────────
# Every cold email the sender generates and every reply the auto-drafter
# composes lands in EmailSent with status='pending_approval' OR is
# auto-promoted to 'approved' based on outreach.gating.should_queue_for_approval.
# This queue is the human-in-the-loop checkpoint — flip the dial in
# Settings to shrink/grow what arrives here.

@admin_required
@require_POST
def lead_generate_email(request, pk):
    """
    Generate the NEXT cold-outreach email for a single lead, on demand.

    Same Claude prompt as the scheduled cold sender but force-queues
    the result as ``pending_approval`` regardless of the current
    trust level — the operator clicked 'Generate' specifically to
    review/edit before sending, so we never auto-promote here.

    Idempotent: refuses to generate if a pending_approval row already
    exists for the same (lead, next_step). Pointer to the existing
    pending row goes in the flash so the operator can jump to it.
    """
    lead = get_object_or_404(Lead, pk=pk)
    from django.contrib import messages as _msg

    # Guards — mirror what the bulk sender checks, but flash a friendly
    # reason instead of silently skipping.
    if not lead.email:
        _msg.error(
            request, f'{lead.firm_name} has no email address — find one first.')
        return redirect('admin_dashboard:lead_detail', pk=lead.pk)
    if lead.unsubscribed:
        _msg.error(
            request, f'{lead.firm_name} has unsubscribed — cannot contact.')
        return redirect('admin_dashboard:lead_detail', pk=lead.pk)
    next_step = lead.sequence_step + 1
    if next_step > 4:
        _msg.error(
            request,
            f'{lead.firm_name} already received the full 4-step sequence.')
        return redirect('admin_dashboard:lead_detail', pk=lead.pk)
    existing = EmailSent.objects.filter(
        lead=lead, sequence_step=next_step,
        status='pending_approval',
    ).first()
    if existing:
        _msg.info(
            request,
            f'Step {next_step} is already pending for {lead.firm_name} '
            f'— review it in the Approvals queue (sidebar).',
        )
        return redirect('admin_dashboard:lead_detail', pk=lead.pk)

    # Generate copy.
    try:
        from outreach.sender import _generate_email_copy, _from_address
        subject, body = _generate_email_copy(lead, next_step)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'lead_generate_email: AI generation failed for lead %s', lead.pk)
        _msg.error(
            request,
            f'AI generation failed: {exc}. Try again or write the '
            f'email manually.')
        return redirect('admin_dashboard:lead_detail', pk=lead.pk)

    EmailSent.objects.create(
        lead=lead,
        kind='cold',
        status='pending_approval',     # force-queue — operator wants to review
        subject=subject,
        body=body,
        from_email=_from_address(),
        sequence_step=next_step,
    )

    # Advance sequence pointer so the scheduled sender doesn't generate
    # a duplicate at next tick. next_followup_at moves the same way the
    # bulk sender moves it — see _STEP_CADENCE_DAYS.
    from outreach.sender import _next_followup_at
    lead.sequence_step = next_step
    lead.next_followup_at = _next_followup_at(next_step, timezone.now())
    lead.save(update_fields=[
        'sequence_step', 'next_followup_at', 'updated_at'])

    _msg.success(
        request,
        f'Generated step {next_step} for {lead.firm_name}. '
        f'Review &amp; approve in the Approvals queue (sidebar).',
    )
    return redirect('admin_dashboard:lead_detail', pk=lead.pk)


@admin_required
def outreach_sent(request):
    """
    All sent outreach emails — searchable, sortable, with engagement
    chips (open / click / reply). Closest thing to a 'Sent folder' for
    the SendGrid-relayed mail that never touches Gmail.

    Filters:
      - q          : free-text search (subject, lead email, lead name)
      - kind       : 'cold' or 'reply'
      - engagement : 'opened' | 'clicked' | 'replied' | 'none'
      - window     : 'today' | '7d' | '30d' | 'all'  (default 30d)
    Sort:
      - sent_desc (default), sent_asc, recipient, subject
    """
    from django.db.models import Q

    qs = (
        EmailSent.objects.filter(status='sent')
        .select_related('lead', 'in_reply_to')
    )

    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(subject__icontains=q)
            | Q(lead__email__icontains=q)
            | Q(lead__firm_name__icontains=q)
        )

    kind = (request.GET.get('kind') or '').strip()
    if kind in ('cold', 'reply'):
        qs = qs.filter(kind=kind)

    engagement = (request.GET.get('engagement') or '').strip()
    if engagement == 'opened':
        qs = qs.filter(opened=True)
    elif engagement == 'clicked':
        qs = qs.filter(clicked=True)
    elif engagement == 'replied':
        qs = qs.filter(replied=True)
    elif engagement == 'none':
        qs = qs.filter(opened=False, clicked=False, replied=False)

    window = (request.GET.get('window') or '30d').strip()
    now = timezone.now()
    if window == 'today':
        qs = qs.filter(sent_at__date=timezone.localdate())
    elif window == '7d':
        qs = qs.filter(sent_at__gte=now - datetime.timedelta(days=7))
    elif window == '30d':
        qs = qs.filter(sent_at__gte=now - datetime.timedelta(days=30))
    # 'all' = no window filter

    sort = (request.GET.get('sort') or 'sent_desc').strip()
    sort_map = {
        'sent_desc': '-sent_at',
        'sent_asc':  'sent_at',
        'recipient': 'lead__firm_name',
        'subject':   'subject',
    }
    qs = qs.order_by(sort_map.get(sort, '-sent_at'))

    # Roll-up stats above the table. Computed against the FILTERED qs
    # so the percentages reflect what's on screen — flipping kind/window
    # updates the rates.
    total = qs.count()
    opens = qs.filter(opened=True).count()
    clicks = qs.filter(clicked=True).count()
    replies = qs.filter(replied=True).count()
    pct = lambda n: round(n * 100 / total) if total else 0  # noqa: E731

    paginator = Paginator(qs, 50)
    page = paginator.get_page(request.GET.get('page') or 1)

    # Preserve current filters across pagination links
    keep = ['q', 'kind', 'engagement', 'window', 'sort']
    filter_qs = '&'.join(
        f'{k}={request.GET.get(k)}' for k in keep if request.GET.get(k))
    filter_qs = ('&' + filter_qs) if filter_qs else ''

    return render(
        request, 'admin_dashboard/outreach_sent.html',
        _admin_context(
            active='outreach_sent',
            page=page,
            total=total,
            opens=opens, open_pct=pct(opens),
            clicks=clicks, click_pct=pct(clicks),
            replies=replies, reply_pct=pct(replies),
            q=q, kind=kind, engagement=engagement,
            window=window, sort=sort,
            filter_qs=filter_qs,
        ),
    )


@admin_required
def outreach_approvals(request):
    """
    List every pending email — cold and reply — with full body preview
    and approve/edit/reject actions. Newest first so the operator can
    triage as the queue grows during the day.
    """
    from outreach.gating import explain as _explain
    from outreach.models import OutreachSettings as _OS

    qs = (
        EmailSent.objects.filter(status='pending_approval')
        .select_related('lead', 'in_reply_to')
        .order_by('-created_at')
    )
    pending = list(qs)
    # Annotate each row with the policy reason — the gating helper is
    # the source of truth, not duplicated here. The attribute name
    # MUST NOT start with an underscore: Django's template engine
    # silently refuses to render those (security guard).
    for e in pending:
        e.gating_reason = _explain(
            e.kind,
            classification=getattr(e.in_reply_to, 'classification', None),
            needs_human=getattr(e.in_reply_to, 'needs_human', False),
        )

    config = _OS.load()
    today_sent = EmailSent.objects.filter(
        status='sent', sent_at__date=timezone.localdate()
    ).count()
    today_approved_pending = EmailSent.objects.filter(
        status='approved'
    ).count()

    return render(
        request, 'admin_dashboard/outreach_approvals.html',
        _admin_context(
            active='outreach_approvals',
            pending=pending,
            pending_count=len(pending),
            config=config,
            today_sent=today_sent,
            today_approved_pending=today_approved_pending,
        ),
    )


@admin_required
@require_POST
def outreach_approval_approve(request, pk):
    """
    Approve a pending email — flips status to 'approved' so the send
    drainer dispatches it on its next tick. Optional body edits from
    the form are saved first so the operator can tweak before approving.
    """
    email = get_object_or_404(
        EmailSent, pk=pk, status='pending_approval'
    )
    subject = (request.POST.get('subject') or '').strip()
    body = (request.POST.get('body') or '').strip()
    fields = ['status', 'approved_at', 'approved_by']
    if subject and subject != email.subject:
        email.subject = subject
        fields.append('subject')
    if body and body != email.body:
        email.body = body
        fields.append('body')
    email.status = 'approved'
    email.approved_at = timezone.now()
    email.approved_by = request.user if request.user.is_authenticated else None
    email.save(update_fields=fields)
    from django.contrib import messages as _messages
    _messages.success(
        request,
        f'Approved — will send to {email.lead.firm_name} on the next '
        f'send tick (within 30 min).')
    return redirect('admin_dashboard:outreach_approvals')


@admin_required
@require_POST
def outreach_approval_reject(request, pk):
    """
    Reject a pending email — flips to 'rejected' with an optional reason.
    Rejected rows stay in the table for audit; the sender will not pick
    the same lead/step again unless the operator manually queues a retry.
    """
    email = get_object_or_404(
        EmailSent, pk=pk, status='pending_approval'
    )
    reason = (request.POST.get('reason') or '').strip()[:255]
    email.status = 'rejected'
    email.rejected_reason = reason or 'Rejected by admin.'
    email.save(update_fields=['status', 'rejected_reason'])
    from django.contrib import messages as _messages
    _messages.info(
        request,
        f'Rejected — {email.lead.firm_name} stays at sequence step '
        f'{email.sequence_step - 1}. The sender will not regenerate '
        f'this step unless you manually queue a retry.')
    return redirect('admin_dashboard:outreach_approvals')


# ──────────────────────────────────────────────────────────────────────────
# Extracted to views_briefs.py
# ──────────────────────────────────────────────────────────────────────────
from .views_briefs import (  # noqa: E402,F401
    _enrichment_recent_activity,
    _enrichment_stats,
    _load_brief_template,
    ai_assistant_execute,
    ai_assistant_page,
    ai_assistant_parse,
    briefs_blank_builder,
    briefs_home,
    briefs_master_download,
    enrichment_status,
    enrichment_status_partial,
    outreach_approval_bulk_approve,
    scrape_job_delete,
    scrape_job_form,
    scrape_job_run_now,
    scrape_job_toggle_active,
    scrape_jobs_list,
)


# ──────────────────────────────────────────────────────────────────────────
# Re-exports for modules extracted from this file.
#
# urls.py resolves every view as `views.<name>`, so a module that
# is split out but not re-exported here disappears from the URL
# conf entirely. A later extraction whose section boundary spanned
# these blocks removed five of them at once, and the failure
# surfaced as AttributeError at URL-conf load.
# ──────────────────────────────────────────────────────────────────────────
from .views_case_studies import (  # noqa: E402,F401
    case_studies_list,
    case_study_ai_draft,
    case_study_edit,
    case_study_new,
    case_study_toggle_publish,
)
from .views_droplets import (  # noqa: E402,F401
    _droplet_rows,
    _fetch_ssh_metrics,
    _load_droplet_dashboard,
    droplet_destroy,
    droplet_link_to_website,
    droplet_list,
    droplet_metrics,
    droplet_new,
    droplet_power,
    droplet_table,
)
from .views_proposals import (  # noqa: E402,F401
    proposal_detail,
    proposal_generate,
    proposal_lead_autofill,
    proposal_new,
    proposal_send,
    proposal_set_status,
    proposals_list,
)
from .views_referrals import (  # noqa: E402,F401
    referral_mark_conversion,
    referral_toggle_active,
    referrals_list,
)
from .views_scans import (  # noqa: E402,F401
    _build_scan_rows,
    _build_tool_blocks,
    _format_duration,
    _scan_row_border,
    _ssl_grade_class,
    download_scan_pdf,
    generate_scan_pdf_view,
    run_scan,
    scan_cancel,
    scan_detail,
    scans_list,
    scans_table,
    send_scan_report,
    toggle_auto_send_scans,
    update_finding_status,
)

# ──────────────────────────────────────────────────────────────────────────
# Extracted to ai_employee_views.py — AI Employees cockpit (§8.2)
# ──────────────────────────────────────────────────────────────────────────
from .ai_employee_views import (  # noqa: E402,F401
    ai_action_decide,
    ai_chat_archive,
    ai_chat_decide,
    ai_chat_new,
    ai_chat_rename,
    ai_chat_live_fragment,
    ai_chat_send,
    ai_chat_thread_fragment,
    ai_employee_add_task,
    ai_employee_chat,
    ai_employee_detail,
    ai_employee_toggle_active,
    ai_employee_wake,
    ai_employees,
)
