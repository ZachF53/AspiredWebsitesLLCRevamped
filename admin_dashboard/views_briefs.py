"""
Claude Code brief/prompt-template builder.

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
import uuid
from outreach.models import EmailSent, Lead, ScrapeJob
from .forms import ScrapeJobForm

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Brief generator — Claude Code prompt-template builder
# ────────────────────────────────────────────────────────────────────────────
# Two templates live in admin_dashboard/brief_templates/:
#   - masterdesign.md  static; redesign an existing site by scanning it
#   - blankdesign.md   parametrized with {{placeholders}}; new-client intake
#
# The blank-builder page is a split view: form on the left, the rendered
# template with the operator's values stitched in on the right, updating
# live as they type. Download = client-side Blob, no server roundtrip.
#
# Field schema lives in BLANK_BUILDER_FIELDS — order, label, type, and
# section determine BOTH the form layout AND which placeholder gets
# replaced in the preview.

import pathlib as _pathlib

_BRIEF_TEMPLATES_DIR = _pathlib.Path(__file__).resolve().parent / 'brief_templates'


def _load_brief_template(name):
    """Read a brief template file from disk. Raises if missing."""
    return (_BRIEF_TEMPLATES_DIR / name).read_text(encoding='utf-8')


# Field schema for the blank-design builder. Each entry maps a {{key}}
# in blankdesign.md to a form input. Section headings drive the
# left-column grouping; order in this list is order on the page.
#
# type: text | textarea | select | bool_checkbox | hidden
# rows: textarea row count
# choices: select options [(value, label), ...]
# placeholder: input placeholder text
# help: 1-line hint below the input
BLANK_BUILDER_FIELDS = [
    # -- A1. Project basics --------------------------------------------
    {'section': 'A1. Project basics', 'name': 'business_name',
     'label': 'Business name', 'type': 'text',
     'placeholder': 'Aspired Websites LLC',
     'default': '[Claude Code: ASK THE HUMAN -- required for nav logo and project memory.]'},
    {'section': 'A1. Project basics', 'name': 'domain',
     'label': 'Domain / URL', 'type': 'text',
     'placeholder': 'https://example.com',
     'default': '[Claude Code: ASK THE HUMAN -- required for the build.]'},

    # -- A2. Brand colors ----------------------------------------------
    {'section': 'A2. Brand colors', 'name': 'color_primary_bg',
     'label': 'Primary background (darkest)', 'type': 'text',
     'placeholder': '#070614',
     'default': '[Claude Code: derive a darkest-background hex from the brand_personality below.]'},
    {'section': 'A2. Brand colors', 'name': 'color_secondary_bg',
     'label': 'Secondary background', 'type': 'text',
     'placeholder': '#0F172A',
     'default': '[Claude Code: a slightly-raised tint of the primary background.]'},
    {'section': 'A2. Brand colors', 'name': 'color_card',
     'label': 'Card / surface color', 'type': 'text',
     'default': '[Claude Code: derive a card surface from the secondary background.]'},
    {'section': 'A2. Brand colors', 'name': 'color_accent',
     'label': 'Brand accent (buttons, links)', 'type': 'text',
     'placeholder': '#E8650A',
     'default': '[Claude Code: pick a primary action colour that fits the brand_personality.]'},
    {'section': 'A2. Brand colors', 'name': 'color_accent_light',
     'label': 'Light tint of accent', 'type': 'text',
     'default': '[Claude Code: lighten the accent by ~30% for highlights.]'},
    {'section': 'A2. Brand colors', 'name': 'color_warm_secondary',
     'label': 'Warm secondary (gold / amber)', 'type': 'text',
     'default': '[Claude Code: choose a complementary warm tone -- gold/amber range.]'},
    {'section': 'A2. Brand colors', 'name': 'color_light_surface',
     'label': 'Light surface (cream / warm white)', 'type': 'text',
     'default': '[Claude Code: a cream or warm-white tint for image frames + bright sections.]'},
    {'section': 'A2. Brand colors', 'name': 'brand_personality',
     'label': 'Brand personality / vibe', 'type': 'textarea', 'rows': 3,
     'placeholder': 'dark and authoritative like a law firm',
     'default': '[Claude Code: ASK THE HUMAN -- drives every visual decision.]'},

    # -- A3. Typography ------------------------------------------------
    {'section': 'A3. Typography', 'name': 'font_heading',
     'label': 'Heading font', 'type': 'text',
     'placeholder': 'Merriweather',
     'default': '[Claude Code: pick a heading font fitting the brand_personality -- Merriweather is a safe serif default.]'},
    {'section': 'A3. Typography', 'name': 'font_body',
     'label': 'Body font', 'type': 'text',
     'placeholder': 'Inter',
     'default': '[Claude Code: pick a body font -- Inter is a safe sans default.]'},
    {'section': 'A3. Typography', 'name': 'font_accent',
     'label': 'Accent / label font', 'type': 'text',
     'placeholder': 'none -- use body at lighter weight',
     'default': '[None -- use body font at a lighter weight unless brand_personality calls for a display/handwritten accent.]'},

    # -- A4. Visual assets ---------------------------------------------
    {'section': 'A4. Visual assets', 'name': 'has_logo', 'label': 'Have a logo?',
     'type': 'select', 'choices': [
         ('', '-- pick --'),
         ('Yes', 'Yes -- file path below'),
         ('No', 'No -- use styled text logo'),
     ],
     'default': '[Claude Code: assume no logo -- use a styled text logo derived from business_name.]'},
    {'section': 'A4. Visual assets', 'name': 'logo_path',
     'label': 'Logo file path / URL', 'type': 'text',
     'default': '[None -- use styled text logo.]'},
    {'section': 'A4. Visual assets', 'name': 'logo_dark_compatible',
     'label': 'Logo works on dark background?', 'type': 'select', 'choices': [
         ('', '--'), ('Yes', 'Yes'),
         ('No -- need light/white version', 'No -- needs light version'),
     ],
     'default': '[Claude Code: assume yes; verify visually and add a light variant if needed.]'},
    {'section': 'A4. Visual assets', 'name': 'favicon_status',
     'label': 'Favicon', 'type': 'text',
     'placeholder': 'Have one at assets/favicon.ico, OR: derive from logo',
     'default': '[Claude Code: derive from the logo, or generate an initials-based favicon from business_name.]'},

    # -- A5. Pages & navigation ----------------------------------------
    {'section': 'A5. Pages & navigation', 'name': 'pages_list',
     'label': 'Pages (one per line, format: Name | URL)',
     'type': 'textarea', 'rows': 6,
     'placeholder': 'Home          | /\nAbout         | /about/\nServices      | /services/\nContact       | /contact/',
     'default': '[Claude Code: ASK THE HUMAN -- minimum required: Home + Contact.]'},
    {'section': 'A5. Pages & navigation', 'name': 'nav_link_order',
     'label': 'Nav link order (one per line)', 'type': 'textarea',
     'rows': 4,
     'placeholder': '1. Home\n2. Services\n3. About\n4. Contact',
     'default': '[Claude Code: match the order of pages_list above.]'},
    {'section': 'A5. Pages & navigation', 'name': 'cta_button_label',
     'label': 'Primary CTA button label', 'type': 'text',
     'placeholder': 'Get a Free Quote',
     'default': '[Claude Code: use a clear action label -- "Get a Free Quote" or "Contact Us" are safe defaults.]'},
    {'section': 'A5. Pages & navigation', 'name': 'cta_button_link',
     'label': 'Primary CTA button link', 'type': 'text',
     'placeholder': '/contact/ or #contact',
     'default': '[Claude Code: link to the Contact page from pages_list.]'},

    # -- A8. Brand voice -----------------------------------------------
    {'section': 'A8. Brand voice', 'name': 'tone_personality',
     'label': 'Tone -- one or more (Professional / Friendly / Bold / Warm / Minimal / Premium / Conversational)',
     'type': 'textarea', 'rows': 3,
     'default': '[Claude Code: derive a tone from the brand_personality above.]'},
    {'section': 'A8. Brand voice', 'name': 'value_statement',
     'label': 'One-sentence value statement (informs hero headline tone)',
     'type': 'textarea', 'rows': 2,
     'default': '[Claude Code: ASK THE HUMAN -- informs the hero headline tone.]'},

    # -- A9. Tech stack ------------------------------------------------
    {'section': 'A9. Tech stack', 'name': 'tech_stack',
     'label': 'Tech stack', 'type': 'select', 'choices': [
         ('', '--'),
         ('Plain HTML / CSS / JS -- static files, no backend', 'Plain HTML / CSS / JS'),
         ('Django -- Python backend, template system', 'Django'),
         ('WordPress -- PHP, theme-based', 'WordPress'),
         ('Other (specify)', 'Other'),
     ],
     'default': '[Claude Code: ASK THE HUMAN -- required, drives HOW the design system is applied.]'},

    # -- A11. Reference sites ------------------------------------------
    {'section': 'A11. Reference sites (optional)', 'name': 'reference_sites',
     'label': 'Sites you like and why (one per line)',
     'type': 'textarea', 'rows': 4,
     'placeholder': 'https://stripe.com -- clean type + lots of whitespace\nhttps://linear.app -- pixel-perfect dark theme',
     'default': '[None provided -- Claude Code uses its own judgment from brand_personality.]'},
    {'section': 'A11. Reference sites (optional)', 'name': 'avoid_design',
     'label': 'What you do NOT want', 'type': 'textarea', 'rows': 3,
     'default': '[None specified.]'},
]


@admin_required
def briefs_home(request):
    """Landing page — pick Master (existing site) or Blank (new build)."""
    return render(request, 'admin_dashboard/briefs_home.html',
                  _admin_context(active='briefs'))


@admin_required
def briefs_master_download(request):
    """Stream masterdesign.md back as a download — no rendering."""
    body = _load_brief_template('masterdesign.md')
    resp = HttpResponse(body, content_type='text/markdown; charset=utf-8')
    resp['Content-Disposition'] = (
        'attachment; filename="masterdesign.md"')
    return resp


@admin_required
def briefs_blank_builder(request):
    """
    Split-view builder — form left, live preview right. Template is
    embedded as a JSON string in a data attribute on the page so the
    JS can do the find-replace client-side without a server roundtrip
    on every keystroke. Download is also client-side (Blob).
    """
    template_text = _load_brief_template('blankdesign.md')

    # Group fields by section for the form layout
    sections = []
    current = None
    for f in BLANK_BUILDER_FIELDS:
        if current is None or current['title'] != f['section']:
            current = {'title': f['section'], 'fields': []}
            sections.append(current)
        current['fields'].append(f)

    return render(
        request, 'admin_dashboard/briefs_blank_builder.html',
        _admin_context(
            active='briefs',
            template_text=template_text,
            sections=sections,
            total_fields=len(BLANK_BUILDER_FIELDS),
        ),
    )


def _enrichment_stats():
    """
    Live counters for the enrichment-status page. One pass over Lead
    using aggregate counts rather than fetching rows — cheap enough
    to call from the 10s HTMX poller.

    Status definitions:
      - pending:    enrichment_attempted_at IS NULL
      - in_flight:  attempted in last 5 min, not yet completed
      - done_1h:    completed in the last hour
      - stuck:      attempted > 5 min ago, no completion
                    (Celery retry budget exhausted)
      - done_total: completed at any time
    """
    from django.db.models import Q

    now = timezone.now()
    inflight_cutoff = now - datetime.timedelta(minutes=5)
    hour_ago = now - datetime.timedelta(hours=1)

    base = Lead.objects.all()
    return {
        'total':       base.count(),
        'pending':     base.filter(enrichment_attempted_at__isnull=True).count(),
        'in_flight':   base.filter(
                            enrichment_attempted_at__gte=inflight_cutoff,
                            enrichment_completed_at__isnull=True).count(),
        'done_1h':     base.filter(
                            enrichment_completed_at__gte=hour_ago).count(),
        'stuck':       base.filter(
                            enrichment_attempted_at__lt=inflight_cutoff,
                            enrichment_completed_at__isnull=True).count(),
        'done_total':  base.filter(
                            enrichment_completed_at__isnull=False).count(),
        'as_of':       now,
    }


def _enrichment_recent_activity(limit=30):
    """
    Last N leads whose enrichment finished, most recent first.
    Each row carries a small set of derived flags (`got_*`) so the
    template can render outcome chips without re-querying.
    """
    rows = list(
        Lead.objects
        .filter(enrichment_completed_at__isnull=False)
        .order_by('-enrichment_completed_at')[:limit]
    )
    for r in rows:
        r.got_website = bool(r.website)
        r.got_pagespeed = r.website_mobile_score is not None
        r.got_social = bool(
            r.facebook_url or r.instagram_url or r.linkedin_url)
        r.got_ssl_check = r.has_ssl is not None
        r.duration_s = None
        if r.enrichment_attempted_at and r.enrichment_completed_at:
            delta = (r.enrichment_completed_at
                     - r.enrichment_attempted_at).total_seconds()
            r.duration_s = int(delta) if delta >= 0 else None
    return rows


@admin_required
def enrichment_status(request):
    """
    Full-page enrichment status — counters at the top, recent activity
    feed below. The counters auto-refresh every 10 seconds via HTMX so
    you can watch a fresh scrape's enrichment tick down to zero.
    """
    return render(request, 'admin_dashboard/enrichment_status.html',
                  _admin_context(
                      active='scrape',
                      stats=_enrichment_stats(),
                      activity=_enrichment_recent_activity(),
                  ))


@admin_required
def enrichment_status_partial(request):
    """
    HTMX partial — just the counter strip + activity feed. Returns
    minimal HTML so the 10s poll is cheap and the page doesn't
    re-render its chrome every tick.
    """
    return render(request, 'admin_dashboard/_enrichment_status.html', {
        'stats': _enrichment_stats(),
        'activity': _enrichment_recent_activity(),
    })


@admin_required
def scrape_jobs_list(request):
    """
    List every standing ScrapeJob. Active jobs run daily at 02:00 via
    ``outreach.tasks.run_scrape_jobs_task`` — toggling ``active`` here
    pauses without deleting (history of last_run_imported is kept).
    """
    jobs = ScrapeJob.objects.all()
    return render(request, 'admin_dashboard/scrape_jobs.html',
                  _admin_context(
                      active='scrape',
                      jobs=jobs,
                      total=jobs.count(),
                      active_count=jobs.filter(active=True).count(),
                  ))


@admin_required
def scrape_job_form(request, pk=None):
    """Create or edit a ScrapeJob — same template, same view."""
    job = get_object_or_404(ScrapeJob, pk=pk) if pk else None
    form = ScrapeJobForm(request.POST or None, instance=job)
    if request.method == 'POST' and form.is_valid():
        saved = form.save()
        from django.contrib import messages as _msg
        _msg.success(
            request,
            f'{"Updated" if job else "Created"} scrape job '
            f'"{saved.name}". Next run: tomorrow 02:00 server time.')
        return redirect('admin_dashboard:scrape_jobs')
    return render(request, 'admin_dashboard/scrape_job_form.html',
                  _admin_context(
                      active='scrape', form=form, job=job,
                  ))


@admin_required
@require_POST
def scrape_job_delete(request, pk):
    job = get_object_or_404(ScrapeJob, pk=pk)
    name = job.name
    job.delete()
    from django.contrib import messages as _msg
    _msg.info(
        request,
        f'Deleted scrape job "{name}". Leads it imported in the past stay.')
    return redirect('admin_dashboard:scrape_jobs')


@admin_required
@require_POST
def scrape_job_toggle_active(request, pk):
    """HTMX-style POST — flip the active flag without leaving the list."""
    job = get_object_or_404(ScrapeJob, pk=pk)
    job.active = not job.active
    job.save(update_fields=['active', 'updated_at'])
    return redirect('admin_dashboard:scrape_jobs')


@admin_required
@require_POST
def scrape_job_run_now(request, pk):
    """
    Fire one ScrapeJob immediately, off the beat schedule. Uses the
    same code path the beat task takes, but synchronously so the
    operator gets a result on the redirect.
    """
    job = get_object_or_404(ScrapeJob, pk=pk)
    from outreach.pipeline import import_leads
    from outreach.scraper import (
        scrape_georgia_bar_sync,
        scrape_google_maps_sync,
        scrape_texas_bar_sync,
    )

    err = ''
    imported = skipped = 0
    try:
        if job.source == 'google_maps':
            state_full = 'Texas' if job.state == 'TX' else 'Georgia'
            raw, _ = scrape_google_maps_sync(
                job.niche, job.city, state_full, job.max_results)
            summary = import_leads(
                raw, source='google_maps',
                business_type_override=job.niche.title())
        elif job.source == 'texas_bar':
            raw = scrape_texas_bar_sync(
                city=job.city, practice_area=job.niche,
                max_results=job.max_results)
            summary = import_leads(
                raw, source='state_bar',
                business_type_override=job.niche.title())
        else:
            raw = scrape_georgia_bar_sync(
                city=job.city, practice_area=job.niche,
                max_results=job.max_results)
            summary = import_leads(
                raw, source='state_bar',
                business_type_override=job.niche.title())
        imported = summary.get('imported', 0)
        skipped = summary.get('duplicates', 0)
    except Exception as exc:  # noqa: BLE001
        logger.exception('manual scrape job %s crashed', job.pk)
        err = str(exc)[:500]

    job.last_run_at = timezone.now()
    job.last_run_imported = imported
    job.last_run_skipped = skipped
    job.last_run_error = err
    job.save(update_fields=[
        'last_run_at', 'last_run_imported',
        'last_run_skipped', 'last_run_error', 'updated_at'])

    from django.contrib import messages as _msg
    if err:
        _msg.error(request, f'{job.name} failed: {err}')
    else:
        _msg.success(
            request,
            f'{job.name} — imported {imported} new lead'
            f'{"s" if imported != 1 else ""}, skipped {skipped} duplicate'
            f'{"s" if skipped != 1 else ""}.')
    return redirect('admin_dashboard:scrape_jobs')


@admin_required
@require_POST
def outreach_approval_bulk_approve(request):
    """
    Approve every pending email in one POST — for L1 operators clearing
    a batch they've already eyeballed. Per-row edits are not possible
    here; for tweaking, use the per-row approve form.
    """
    qs = EmailSent.objects.filter(status='pending_approval')
    n = qs.count()
    qs.update(
        status='approved',
        approved_at=timezone.now(),
        approved_by=request.user if request.user.is_authenticated else None,
    )
    from django.contrib import messages as _messages
    _messages.success(
        request,
        f'Approved {n} email{"s" if n != 1 else ""}. '
        f'They will dispatch on the next send tick.')
    return redirect('admin_dashboard:outreach_approvals')


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 — AI Assistant
# ─────────────────────────────────────────────────────────────────────────────

@admin_required
def ai_assistant_page(request):
    """Full-page assistant. Shows the command box + recent log entries.
    The actual parse/preview/execute interactions are HTMX-driven.
    """
    from admin_dashboard.models import AIAssistantLog
    recent = list(AIAssistantLog.objects.all().order_by('-created_at')[:20])
    return render(request, 'admin_dashboard/ai_assistant.html', {
        'active_nav': 'ai_assistant',
        'recent_logs': recent,
    })


@admin_required
@require_POST
def ai_assistant_parse(request):
    """HTMX endpoint — parse the typed command and return the preview
    card (or a clarify message). NO mutation here."""
    from admin_dashboard import ai_assistant
    from reporting.ai import AIError, AINotConfigured

    text = (request.POST.get('command') or '').strip()
    try:
        parsed = ai_assistant.parse_command(text)
    except AINotConfigured:
        return render(request, 'admin_dashboard/_ai_preview.html', {
            'error': ('Claude API key not configured on the server. '
                      'Add ANTHROPIC_API_KEY to .env and restart.'),
        })
    except AIError as exc:
        return render(request, 'admin_dashboard/_ai_preview.html', {
            'error': f'AI error: {exc}',
        })

    if 'clarify' in parsed:
        return render(request, 'admin_dashboard/_ai_preview.html', {
            'clarify': parsed['clarify'],
            'raw_command': text,
        })

    intent = parsed['intent']
    args = parsed['args'] or {}
    name_query = args.get('client') or ''
    try:
        profile = ai_assistant.resolve_client(name_query)
    except ai_assistant.ClientAmbiguous as exc:
        return render(request, 'admin_dashboard/_ai_preview.html', {
            'ambiguous': exc.matches,
            'raw_command': text,
            'intent': intent,
            'args': args,
        })
    except ai_assistant.ClientNotFound as exc:
        return render(request, 'admin_dashboard/_ai_preview.html', {
            'error': str(exc),
            'raw_command': text,
        })

    preview = ai_assistant.build_preview(intent, args, profile)
    preview['raw_command'] = text
    return render(request, 'admin_dashboard/_ai_preview.html', {
        'preview': preview,
    })


@admin_required
@require_POST
def ai_assistant_execute(request):
    """HTMX endpoint — operator confirmed. Run the service function
    via ai_assistant.execute and write an AIAssistantLog row."""
    from admin_dashboard import ai_assistant
    from admin_dashboard.models import AIAssistantLog
    from clients.models import ClientProfile
    import json as _json

    intent = (request.POST.get('intent') or '').strip()
    args_raw = request.POST.get('args') or '{}'
    raw_command = (request.POST.get('raw_command') or '').strip()
    client_id = (request.POST.get('client_id') or '').strip()
    try:
        args = _json.loads(args_raw)
    except ValueError:
        args = {}

    profile = None
    try:
        profile = ClientProfile.objects.filter(id=client_id).first()
    except Exception:
        profile = None
    if profile is None:
        return render(request, 'admin_dashboard/_ai_result.html', {
            'result': {'ok': False, 'message': 'Client no longer found.'},
        })

    set_by = (request.user.get_full_name()
              or request.user.username
              or 'admin')

    result = ai_assistant.execute(
        intent, args, profile, set_by=f'AI assistant ({set_by})')

    # Audit log — every executed command, success or failure.
    try:
        AIAssistantLog.objects.create(
            operator=request.user,
            client=profile,
            raw_command=raw_command,
            intent=intent,
            args=args,
            success=bool(result.get('ok')),
            result_message=result.get('message') or '',
        )
    except Exception:
        logger.exception('AIAssistantLog write failed')

    return render(request, 'admin_dashboard/_ai_result.html', {
        'result': result,
        'firm_name': profile.firm_name,
    })
