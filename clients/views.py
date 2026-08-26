"""Client portal views."""

import logging
from datetime import timedelta

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from vault.crypto import generate_salt, hash_client_pin, verify_client_pin

from .decorators import allow_pending_intake, client_required
from .emails import send_contract_signed_email
from .forms import (
    FileUploadForm,
    IntakeForm,
    RevisionForm,
    SettingsForm,
    SupportTicketForm,
)
from .models import (
    PROJECT_STAGES,
    ClientDocument,
    Contract,
    IntakePhoto,
    IntakeResponse,
)
from .portal_resolvers import (
    SESSION_KEY_ACTIVE_WEBSITE,
    resolve_account_for_user,
)
from .pdf_utils import render_contract_pdf
from clients.display import owner_label
from .vault_helpers import (
    get_client_vault_remaining_seconds,
    is_client_vault_unlocked,
    mark_client_vault_unlocked,
)

logger = logging.getLogger(__name__)

_STAGE_KEYS = [key for key, _ in PROJECT_STAGES]

# Client credentials PIN gate — 5 wrong tries → a 30-minute lockout.
CLIENT_PIN_MAX_ATTEMPTS = 5
CLIENT_PIN_LOCKOUT_MINUTES = 30


# ── Shared helpers ──────────────────────────────────────────────────────────

def _active_project(request):
    """
    Phase C shim — returns the per-request "project" object for portal
    views.

    Resolution order:
      1. ``request.website`` — the post-refactor Website the chooser
         picked. Source of truth for per-build state when set.
      2. ``request.client_profile`` — the legacy single ClientProfile.
         Falls back to this so single-website (and pre-backfill)
         accounts keep working unchanged.

    Why: an Account can own multiple Websites but only ONE legacy
    ClientProfile (User is OneToOne with CP). When the admin pushes
    stage on Website A, the CP.stage mirror immediately gets
    overwritten by the next push on Website B — same field, two
    writers. The portal needs per-pick state, not the shared CP.

    Returns whichever is non-None, or None if neither is set.

    Both Website and ClientProfile expose the same read attributes
    callers use (`.stage`, `.revisions`, `.stage_logs`, `.revision_count`,
    `.payment_status`, `.launch_date`, `.support_window_ends`, etc.)
    so existing call sites don't need per-type branching.
    """
    site = getattr(request, 'website', None)
    if site is not None:
        return site
    return getattr(request, 'account', None)


def _owner_filter(request):
    """Queryset filter scoping rows to whatever owns this request.

    Four portal views had each written this inline as

        ({'website_new': project} if getattr(request, 'website', None)
         else {'client': request.client_profile})

    which has a hole in it. Since Wave 1, `client_required` admits a user
    who has an Account but no legacy ClientProfile — that is the shape
    every client created after the cutover will have. For them the else
    branch produces `{'client': None}`, which does not scope to their
    data, it filters to rows owned by nobody.

    Resolution order, canonical first:
      1. the picked Website,
      2. the Account, when there is no per-website pick,
      3. the legacy profile, for accounts the backfill has not reached.

    When nothing owns the request the result matches no rows. That is
    deliberate: every call site splats this into `.filter(**flt)`, and the
    failure mode of an unscoped filter on a portal page is showing one
    client another client's records. Returning an impossible filter fails
    closed; returning `{}` would fail open.
    """
    site = getattr(request, 'website', None)
    if site is not None:
        return {'website_new': site}
    account = getattr(request, 'account', None)
    if account is not None:
        return {'website_new__account': account}
    profile = getattr(request, 'client_profile', None)
    if profile is not None:
        return {'client': profile}
    return {'pk__in': ()}


def _owns(request, obj):
    """Whether the requesting client owns `obj`.

    Checks the canonical owner first and never dereferences a legacy
    profile that may be absent. The previous inline version read
    `request.client_profile.id` as the first term of an `or`, so it
    raised AttributeError for an Account-only client before the
    canonical branch could answer — on an access-control check.
    """
    account = getattr(request, 'account', None)
    website_id = getattr(obj, 'website_new_id', None)
    if website_id and account is not None:
        site = getattr(obj, 'website_new', None)
        if site is not None and site.account_id == account.id:
            return True

    account_id = getattr(obj, 'account_new_id', None) or getattr(
        obj, 'account_id', None)
    if account_id and account is not None and account_id == account.id:
        return True

    # Nothing left to fall back to: `request.client_profile` is gone, so
    # an object that matched neither the site nor the account above is
    # simply not this request's. Failing closed is the only safe answer
    # for an ownership check.
    return False


def _intake_for(profile, site):
    """Resolve (or create) the IntakeResponse for a site.

    Keyed on the website: an intake describes one build, and a client
    with two builds owes an intake for each. `profile` is the site too —
    both arguments are the same object now — and is kept in the signature
    only because several callers pass it positionally.
    """
    from .models import IntakeResponse
    site = site or profile
    if site is None:
        return None
    obj = IntakeResponse.objects.filter(website_new=site).first()
    if obj is None:
        obj = IntakeResponse.objects.create(website_new=site)
    return obj


def _portal_context(request, active_nav, **extra):
    """Common context for every portal page — drives the sidebar + badges."""
    account = getattr(request, 'account', None)
    website = getattr(request, 'website', None)

    # Scope the badge queries to whatever owns this request — the active
    # Website first, then the Account, then the legacy profile. Shared
    # with the portal list views so one rule decides scoping everywhere,
    # and an Account-only client (no legacy profile) is scoped to their
    # own data rather than to `client=None`.
    scope = website or account
    flt = _owner_filter(request)

    from .models import (
        IntakeResponse, RevisionRequest, SiteChangelogEntry, SupportTicket,
    )
    intake = IntakeResponse.objects.filter(**flt).first()
    pending_revisions = RevisionRequest.objects.filter(
        **flt, status='pending').count()
    open_tickets = SupportTicket.objects.filter(
        **flt, status__in=['open', 'in_progress']).count()
    # Green-dot badge on the Activity Log nav item — new entries in last 7 days.
    changelog_has_new = SiteChangelogEntry.objects.filter(
        **flt,
        is_client_visible=True,
        created_at__gte=timezone.now() - timedelta(days=7),
    ).exists()

    # Red-dot badge on the Security nav item — any open critical/high
    # finding on the latest completed scan. One short query; never N+1.
    security_has_open = False
    try:
        from reporting.models import VulnerabilityScan
        latest_scan = (
            VulnerabilityScan.objects
            .filter(**flt, status='complete')
            .order_by('-completed_at').first()
        )
        if latest_scan:
            security_has_open = latest_scan.findings.filter(
                status='open',
                severity__in=('critical', 'high'),
            ).exists()
    except Exception:
        # Reporting app may not have migrated on this env yet — fall
        # back to no badge rather than crash every portal page.
        security_has_open = False

    # Orange-dot badge on the Recommendations nav item — any
    # suggestion the client has been sent but hasn't responded to.
    portal_suggestions_pending = False
    try:
        from .models import IntelligenceSuggestion
        portal_suggestions_pending = IntelligenceSuggestion.objects.filter(
            **flt, status='sent_to_client',
        ).exists()
    except Exception:
        # IntelligenceSuggestion table may not exist on a fresh
        # checkout — never break the chrome over a missing table.
        portal_suggestions_pending = False

    # Intake-only mode — true while the build is in `pending_intake`.
    # Drives the portal base template's nav: when set, only the Intake
    # Form link and Sign Out are rendered. Prevents the confusing UX
    # of links that immediately redirect back to /portal/intake/.
    intake_only = (
        getattr(scope, 'onboarding_status', '') == 'pending_intake')

    # Namecheap sandbox mode — when on, every domain action goes to
    # the sandbox registry (not real, not permanent). Surfaced on
    # every portal page so a client in the middle of testing knows
    # nothing they do binds them to anything.
    namecheap_sandbox_mode = False
    try:
        from domains.models import NamecheapConfig
        namecheap_sandbox_mode = NamecheapConfig.is_sandbox()
    except Exception:
        # domains app might not be migrated yet (fresh clone) —
        # never break portal chrome over a missing config row.
        namecheap_sandbox_mode = False

    # Phase C — surface Account + Website + the account's website
    # list so templates can render the "viewing: <site name>" header,
    # a switch link, and account-vs-website nav distinctions. (account +
    # website already resolved at the top of this function.)
    if account is not None:
        websites_list = list(account.websites.all().order_by('name'))
    else:
        websites_list = []
    multi_website = len(websites_list) > 1

    ctx = {
        # Templates read {{ profile.name }} for the client's own name and
        # {{ profile.contact_name }} for the person, both account-level.
        'profile': account,
        # `project` is an alias used by every template that reads
        # {{ project.stage }} / {{ project.revisions }} / etc. It is the
        # active Website: those are per-build facts, and on a multi-site
        # account the chooser decides which build is being looked at.
        'project': website or account,
        'intake': intake,
        'active_portal_nav': active_nav,
        'intake_incomplete': intake is None or not intake.completed,
        'pending_revisions': pending_revisions,
        'open_tickets': open_tickets,
        'changelog_has_new': changelog_has_new,
        'security_has_open': security_has_open,
        'portal_suggestions_pending': portal_suggestions_pending,
        # Tier 2 — only show the Recordings nav link when the addon
        # is active for this client.
        'session_recording_nav_visible': bool(
            getattr(scope, 'session_recording_enabled', False)),
        'intake_only': intake_only,
        'namecheap_sandbox_mode': namecheap_sandbox_mode,
        # Phase C — Account / Website context.
        'account': account,
        'website': website,
        'websites_list': websites_list,
        'multi_website': multi_website,
    }
    ctx.update(extra)
    return ctx


def _stage_steps(project):
    """Return the 8 stages tagged completed / current / upcoming."""
    current = _STAGE_KEYS.index(project.stage) if project.stage in _STAGE_KEYS else 0
    steps = []
    for i, (key, label) in enumerate(PROJECT_STAGES):
        if i < current:
            status = 'completed'
        elif i == current:
            status = 'current'
        else:
            status = 'upcoming'
        steps.append({'key': key, 'label': label, 'status': status})
    return steps


def _project_timeline(project):
    """Stage steps annotated with the date + note from ProjectStageLog."""
    logs_by_stage = {}
    for log in project.stage_logs.all():  # ordered -created_at
        if log.to_stage and log.to_stage not in logs_by_stage:
            logs_by_stage[log.to_stage] = log
    steps = _stage_steps(project)
    for step in steps:
        log = logs_by_stage.get(step['key'])
        step['date'] = log.created_at if log else None
        step['note'] = log.note if log else ''
    return steps


_INTAKE_STEP_LABELS = [
    'Brand', 'Photos', 'Website Copy', 'References',
    'Domain & Social', 'Review & Submit',
]


def _intake_steps(intake):
    """Per-step completion flags for the 6-step intake form."""
    if intake is None:
        done = [False] * 6
    else:
        # Step 2 — also counts as done when a photo has been uploaded
        # (not only when the checkbox + note are set), since the upload
        # is the meatier action.
        has_photos = (
            intake.photos.exists()
            if hasattr(intake, 'photos') else False)
        # Step 5 — domain or ANY of the split social fields constitutes
        # progress; freeform `social_links` blob still counts.
        social_any = any([
            intake.facebook_url, intake.instagram_url,
            intake.linkedin_url, intake.twitter_url,
            intake.google_business_url, intake.social_links,
        ])
        done = [
            bool(intake.brand_colors or intake.brand_fonts or intake.logo),
            bool(intake.photos_provided or intake.photos_note or has_photos),
            bool(intake.about_copy or intake.practice_areas or intake.attorney_bios),
            bool(intake.reference_sites or intake.competitors),
            bool(intake.domain_name or intake.domain_registrar or social_any),
            bool(intake.completed),
        ]
    steps = [
        {'number': i + 1, 'label': label, 'done': done[i]}
        for i, label in enumerate(_INTAKE_STEP_LABELS)
    ]
    completed = sum(done)
    percent = round(completed / 6 * 100)
    return steps, completed, percent


# ── Page 1: Dashboard ───────────────────────────────────────────────────────

@client_required
def dashboard(request):
    project = _active_project(request)

    next_invoice = None
    stage_steps = []
    activity = []
    if project:
        stage_steps = _stage_steps(project)
        activity = list(project.stage_logs.all()[:5])
        contract = project.contracts.order_by('-created_at').first()
        if contract:
            if project.payment_status == 'awaiting_deposit':
                next_invoice = {'label': 'Deposit (50%)', 'amount': contract.deposit_amount}
            elif project.payment_status == 'deposit_paid':
                next_invoice = {'label': 'Final payment', 'amount': contract.final_amount}

    from reporting.uptime_helpers import (
        get_avg_response_time, get_uptime_percentage,
    )

    # ── D5 — gather per-service rows for the dashboard cards ──
    # The base template (clients/dashboard.html) renders these via
    # the portal-services context processor (has_website etc.) — here
    # we just pull the actual rows so each card has data.
    maintenance_plans = []
    social_media_plans = []
    droplets = []
    try:
        account = getattr(request.user, 'account', None)
        if account is not None:
            maintenance_plans = list(
                account.maintenance_plans.select_related('website')
                .order_by('-started_at')[:3])
            social_media_plans = list(
                account.social_media_plans.prefetch_related('channels')
                .order_by('-started_at')[:2])
            droplets = list(
                account.droplets.select_related('website')
                .order_by('-provisioned_at')[:5])
    except Exception:
        pass

    ctx = _portal_context(
        request, 'dashboard',
        stage_steps=stage_steps,
        activity=activity,
        next_invoice=next_invoice,
        uptime_30=get_uptime_percentage(project, 30) if project else None,
        uptime_avg_response=(
            get_avg_response_time(project, 30) if project else None),
        maintenance_plans=maintenance_plans,
        social_media_plans=social_media_plans,
        droplets=droplets,
    )
    return render(request, 'clients/dashboard.html', ctx)


# ── Page: Social Channels (portal-side read-only view) ─────────────────────

@client_required
def social_channels(request):
    """Client-portal social page — read-only view of their active
    SocialMediaPlan(s), connected channels, and recent posts.

    Channel management (OAuth, composing, scheduling) lives on the
    admin side at /admin-dashboard/social/. This page just shows the
    client what's connected and what's been published recently.
    """
    from clients.account_models import Account

    account = request.account

    plans = []
    recent_posts = []
    if account is not None:
        plans = list(
            account.social_media_plans
            .prefetch_related('channels')
            .order_by('-started_at')
        )
        # Pull the last 10 posts across every channel on this account
        # (via channel -> plan -> account).
        from social.models import ScheduledPost
        recent_posts = list(
            ScheduledPost.objects
            .filter(channel__plan__account=account)
            .select_related('channel')
            .order_by('-scheduled_for', '-created_at')[:10]
        )

    ctx = _portal_context(
        request, 'social',
        plans=plans,
        recent_posts=recent_posts,
    )
    return render(request, 'clients/social.html', ctx)


# ── Page 2: My Project ──────────────────────────────────────────────────────

@client_required
def project_detail(request):
    project = _active_project(request)

    timeline = []
    revisions = []
    support_window_left = None
    final_pay_url = ''
    final_due = None
    if project:
        timeline = _project_timeline(project)
        revisions = list(project.revisions.all())
        if project.stage == 'live' and project.support_window_ends:
            delta = (project.support_window_ends - timezone.localdate()).days
            support_window_left = delta
        # Remaining-balance pay button — shown when the deposit is in but
        # the final invoice is outstanding (issued at Pre-Launch).
        if (getattr(project, 'payment_status', '') == 'deposit_paid'
                and getattr(project, 'final_invoice_url', '')):
            final_pay_url = project.final_invoice_url
            from clients.models import Contract
            c = (Contract.objects.filter(website_new=project, signed=True)
                 .order_by('-created_at').first())
            if c is not None and c.build_price:
                final_due = c.final_amount

    from reporting.uptime_helpers import (
        get_current_status, get_uptime_chart_data, get_uptime_percentage,
    )
    uptime_chart = get_uptime_chart_data(project, 30) if project else []
    peak_ms = max(
        (d['avg_response_ms'] or 0 for d in uptime_chart), default=0) or 1
    for day in uptime_chart:
        day['bar_h'] = round((day['avg_response_ms'] or 0) / peak_ms * 100)

    ctx = _portal_context(
        request, 'project',
        timeline=timeline,
        revisions=revisions,
        support_window_left=support_window_left,
        final_pay_url=final_pay_url,
        final_due=final_due,
        uptime_status=get_current_status(project) if project else None,
        uptime_30=get_uptime_percentage(project, 30) if project else None,
        uptime_90=get_uptime_percentage(project, 90) if project else None,
        uptime_chart=uptime_chart,
    )
    return render(request, 'clients/project.html', ctx)


# ── Page 3: Intake Form ─────────────────────────────────────────────────────

def _intake_unlocked(client, project):
    """
    Whether the client can fill in the intake form.

    Unlocked when ANY of:
      1. NEW admin-invoice flow — onboarding_status has moved off
         pending_setup (so password + PIN are done). The Project may or
         may not exist yet; the caller is responsible for materialising
         one on demand.
      2. NEW admin-invoice flow — a Stripe invoice has been issued
         (stripe_invoice_id set) and the profile is active. Catches the
         edge where onboarding_status is somehow still pending_setup
         but the rest of the state says they're past it.
      3. OLD contract-signing flow — a Project exists and payment_status
         is deposit_paid or fully_paid (the original gate, preserved).
    """
    onboarding = getattr(client, 'onboarding_status', '') or ''
    if onboarding in ('pending_intake', 'onboarding_complete'):
        return True
    if (getattr(client, 'status', '') == 'active'
            and getattr(client, 'stripe_invoice_id', '')):
        return True
    if project and getattr(project, 'payment_status', '') in (
            'deposit_paid', 'fully_paid'):
        return True
    return False


def _intake_missing_required(intake_obj):
    """
    Return a list of human-readable labels for required intake fields
    that are still empty. Matches the wizard's per-step rules in
    intake_form.js so a bypass-JS submit gets the same gate.

    Step 1 — Brand: brand_colors, brand_fonts, logo
    Step 2 — Photos: if photos_provided, at least one IntakePhoto +
                      a photos_note
    Step 3 — Website Copy: about_copy, practice_areas, attorney_bios
    Step 4 — References: reference_sites, competitors
    Step 5 — Domain: domain_name, domain_registrar; if registrar ==
                      "other", domain_registrar_other
    """
    missing = []

    # Step 1
    if not (intake_obj.brand_colors or '').strip():
        missing.append('Brand colors')
    if not (intake_obj.brand_fonts or '').strip():
        missing.append('Brand fonts')
    if not intake_obj.logo and not intake_obj.no_logo_yet:
        missing.append('Logo upload (or check "I don\'t have a logo yet")')

    # Step 2 — photos are entirely OPTIONAL. The section never blocks
    # submission; the client can add photos now if they have them or
    # skip the step entirely. (Earlier iterations required either a
    # Yes/No answer + uploads — dropped per spec.)

    # Step 3
    if not (intake_obj.about_copy or '').strip():
        missing.append('About your firm')
    if not (intake_obj.practice_areas or '').strip():
        missing.append('Practice areas / services')
    if not (intake_obj.attorney_bios or '').strip():
        missing.append('Attorney / team bios')

    # Step 4
    if not (intake_obj.reference_sites or '').strip():
        missing.append('Sites you like the look of')
    if not (intake_obj.competitors or '').strip():
        missing.append('Your competitors')

    # Step 5
    if not (intake_obj.domain_name or '').strip():
        missing.append('Your domain')
    if not (intake_obj.domain_registrar or '').strip():
        missing.append('Where the domain is registered')
    if intake_obj.domain_registrar == 'other':
        if not (intake_obj.domain_registrar_other or '').strip():
            missing.append('Registrar name (since you picked "Other")')

    return missing


def _ensure_project_for_unlocked_intake(client):
    """
    Lazily create the IntakeResponse + ClientVault for a new-flow
    client who has reached the intake page without a real Stripe
    webhook having fired (test/demo path, or a webhook that silently
    failed and was never retried).

    Post-2026-05-25 refactor: Project no longer exists as a separate
    row — the former Project fields live on ClientProfile directly.
    This function now just ensures the row that HOLDS the intake
    answers exists, and seeds reasonable defaults on the client
    (stage='intake', payment_status='fully_paid' for new-flow).
    Returns the client (formerly returned the Project) so callers
    that pass the result around still get something truthy.

    Idempotent.
    """
    fields_to_save = []
    if not client.stage:
        client.stage = 'intake'
        fields_to_save.append('stage')
    if not client.payment_status or client.payment_status == 'awaiting_deposit':
        client.payment_status = 'fully_paid'
        client.final_paid_at = timezone.now()
        fields_to_save += ['payment_status', 'final_paid_at']
    if fields_to_save:
        fields_to_save.append('updated_at')
        client.save(update_fields=fields_to_save)

    IntakeResponse.objects.get_or_create(client=client)
    try:
        from vault.models import ClientVault
        ClientVault.objects.get_or_create(client=client)
    except Exception:
        logger.exception(
            'Auto-create of ClientVault failed for %s', client.pk)
    return client


@client_required
@allow_pending_intake
def intake(request):
    # The intake describes a build, so the subject is the site.
    profile = getattr(request, 'website', None)
    project = _active_project(request)

    if not _intake_unlocked(profile, project):
        ctx = _portal_context(request, 'intake', intake_locked=True)
        return render(request, 'clients/intake.html', ctx)

    # Unlocked but no Project yet — materialise it now. Covers the
    # new-flow test path where no real Stripe webhook ever fired (so
    # the webhook-side _on_onboarding_invoice_paid hook never ran).
    if project is None:
        project = _ensure_project_for_unlocked_intake(profile)

    intake_obj = _intake_for(profile, getattr(request, 'website', None))

    if request.method == 'POST':
        # Final submission — fields are already auto-saved; this just
        # finalises after a server-side completeness check (mirrors the
        # wizard JS's per-step gating, so JS-bypass submits get the
        # same answer the UI would have given).
        if not intake_obj.completed:
            missing = _intake_missing_required(intake_obj)
            if missing:
                messages.error(
                    request,
                    'Please finish these required fields before '
                    'submitting: ' + ', '.join(missing) + '.')
                return redirect('clients:intake')
            intake_obj.completed = True
            intake_obj.completed_at = timezone.now()
            intake_obj.save(update_fields=['completed', 'completed_at', 'updated_at'])
            _on_intake_submitted(profile, project)
            _notify_admin_intake_complete(profile)
            messages.success(request, 'Intake form submitted — thank you!')
        return redirect('clients:intake')

    form = IntakeForm(instance=intake_obj)
    steps, completed, percent = _intake_steps(intake_obj)
    ctx = _portal_context(
        request, 'intake',
        form=form,
        intake_steps=steps,
        intake_completed_count=completed,
        intake_percent=percent,
    )
    return render(request, 'clients/intake.html', ctx)


@client_required
@allow_pending_intake
def intake_save(request):
    """HTMX auto-save endpoint — persists the intake form on every change."""
    # The intake describes a build, so the subject is the site.
    profile = getattr(request, 'website', None)
    project = _active_project(request)
    if request.method != 'POST' or not _intake_unlocked(profile, project):
        return redirect('clients:intake')

    # Belt-and-suspenders: intake() materialises the Project on first
    # GET, but if auto-save somehow fires first (HTMX kicks in on field
    # change), do the same lazy create here.
    if project is None:
        project = _ensure_project_for_unlocked_intake(profile)

    intake_obj = _intake_for(profile, getattr(request, 'website', None))

    # Step 2 radios POST `photos_provided=yes|no`. Django's
    # CheckboxInput.value_from_datadict treats both as truthy (any
    # non-empty string), so we have to translate explicitly:
    #   yes  -> 'true'  (checkbox parses as True)
    #   no   -> absent  (checkbox parses as False)
    #   ''   -> absent  (initial state; left as False)
    post = request.POST.copy()
    val = (post.get('photos_provided') or '').strip().lower()
    if val == 'yes':
        post['photos_provided'] = 'true'
    elif val == 'no' or val == '':
        post.pop('photos_provided', None)

    form = IntakeForm(post, request.FILES, instance=intake_obj)
    if form.is_valid():
        form.save()
        intake_obj.refresh_from_db()
    steps, completed, percent = _intake_steps(intake_obj)
    # Return the progress bar (primary swap) PLUS the Step-6 review summary
    # as an out-of-band swap, so the review always reflects the latest saved
    # answers instead of the empty page-load snapshot.
    from django.template.loader import render_to_string
    progress_html = render_to_string('clients/_intake_progress.html', {
        'intake_steps': steps,
        'intake_completed_count': completed,
        'intake_percent': percent,
        'saved_at': timezone.now(),
    }, request=request)
    review_html = render_to_string('clients/_intake_review.html', {
        'intake': intake_obj,
        'oob': True,
    }, request=request)
    return HttpResponse(progress_html + review_html)


# ── Intake photos (step 2) ──────────────────────────────────────────────────


def _photo_gallery_response(request, intake_obj):
    """Render the photo gallery partial — used by upload + delete."""
    return render(request, 'clients/_intake_photos.html', {
        'intake': intake_obj,
        'photos': intake_obj.photos.all(),
    })


@client_required
@allow_pending_intake
@require_POST
def intake_photo_upload(request):
    """
    HTMX endpoint: accept one OR MANY files at once from the photo step.
    Validates type + size per-file, creates an IntakePhoto for each
    accepted upload, returns the refreshed gallery partial.

    The file input on the wizard has `multiple` — desktop browsers send
    several files in a single POST under the same `file` field name.
    `request.FILES.getlist('file')` handles both the single and the
    many case uniformly.
    """
    # The intake describes a build, so the subject is the site.
    profile = getattr(request, 'website', None)
    project = _active_project(request)
    if not _intake_unlocked(profile, project):
        return HttpResponse(status=403)
    if project is None:
        project = _ensure_project_for_unlocked_intake(profile)
    intake_obj = _intake_for(profile, getattr(request, 'website', None))

    files = request.FILES.getlist('file')
    if not files:
        return _photo_gallery_response(request, intake_obj)

    label = (request.POST.get('label') or '').strip()
    too_big = 0
    wrong_type = 0
    saved = 0
    for uploaded in files:
        # 50MB cap — same as the Files page (FileUploadForm).
        if uploaded.size > 50 * 1024 * 1024:
            too_big += 1
            continue
        ctype = (uploaded.content_type or '').lower()
        if not ctype.startswith('image/'):
            wrong_type += 1
            continue
        IntakePhoto.objects.create(
            intake=intake_obj, file=uploaded, label=label)
        saved += 1

    # Auto-flag photos_provided=True when at least one photo lands so
    # downstream "do they have photos?" checks (admin views, derived
    # state) don't need to look at the gallery count themselves.
    if saved and not intake_obj.photos_provided:
        intake_obj.photos_provided = True
        intake_obj.save(update_fields=['photos_provided', 'updated_at'])

    if too_big:
        messages.error(
            request,
            f'{too_big} file(s) skipped — photos must be 50MB or smaller.')
    if wrong_type:
        messages.error(
            request,
            f'{wrong_type} file(s) skipped — only image files allowed.')

    return _photo_gallery_response(request, intake_obj)


@client_required
@allow_pending_intake
@require_POST
def intake_photo_delete(request, photo_id):
    """HTMX endpoint: remove one IntakePhoto, return the refreshed gallery."""
    # The intake describes a build, so the subject is the site.
    profile = getattr(request, 'website', None)
    project = _active_project(request)
    if project is None or not _intake_unlocked(profile, project):
        return HttpResponse(status=403)
    intake_obj = _intake_for(profile, getattr(request, 'website', None))

    photo = (IntakePhoto.objects
             .filter(id=photo_id, intake=intake_obj).first())
    if photo:
        # Best-effort delete of the underlying file too — never crash the
        # request if storage cleanup fails (the row going away is what
        # matters from the client's point of view).
        try:
            photo.file.delete(save=False)
        except Exception:
            logger.exception(
                'IntakePhoto file delete failed for %s', photo.pk)
        photo.delete()
    return _photo_gallery_response(request, intake_obj)


def _notify_admin_intake_complete(profile):
    from django.conf import settings
    from django.core.mail import send_mail
    send_mail(
        subject=f'New intake: {profile.firm_name} — review and confirm timeline',
        message=(
            f'{profile.firm_name} has submitted their intake form.\n\n'
            f'Review and confirm their project start date:\n'
            f'{settings.SITE_BASE_URL}/admin-dashboard/clients/'
            f'{profile.id}/\n'
        ),
        from_email=settings.EMAIL_FROM_NO_REPLY,
        recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
        fail_silently=True,
    )


def _copy_intake_files_to_documents(profile, project):
    """
    On intake submission, every file the client uploaded (the Logo on
    Step 1 + each IntakePhoto on Step 2) gets copied into a
    `ClientDocument` row so it shows up on the portal Files page.

    File bytes are re-written into the docs upload path
    (`portal/clients/<id>/docs/<filename>`) — not just linked — so the
    intake row can be cleaned up later without orphaning the docs.
    Idempotent at the row level: if a ClientDocument with a matching
    `label` already exists for this client we skip it, so a re-run
    (e.g. a future "redo intake" flow) doesn't pile up duplicates.
    """
    import os

    from django.core.files import File

    intake_obj = getattr(project, 'intake', None)
    if intake_obj is None:
        return

    def _make_doc(label, file_field):
        """Copy file_field into a new ClientDocument unless one with
        this label already exists for the client."""
        if not file_field:
            return
        if ClientDocument.objects.filter(
                website_new=profile, label=label).exists():
            return
        try:
            file_field.open('rb')
            ClientDocument.objects.create(
                website_new=profile,
                direction='from_client',
                label=label,
                description='Uploaded via intake form.',
                uploaded_by=profile.user,
                file=File(file_field, name=os.path.basename(
                    file_field.name)),
            )
        finally:
            try:
                file_field.close()
            except Exception:
                pass

    # ── Logo ──
    if intake_obj.logo:
        _make_doc('Intake — Logo', intake_obj.logo)

    # ── Photos ──
    for photo in intake_obj.photos.all():
        base = os.path.basename(photo.file.name) if photo.file else ''
        label = photo.label or f'Intake — Photo ({base})' or 'Intake — Photo'
        _make_doc(label, photo.file)


def _on_intake_submitted(profile, project):
    """
    Post-intake hook (Part 6) — flips onboarding state to complete,
    logs the milestone to the changelog, enqueues Droplet provisioning,
    and emails the client a confirmation.

    Best-effort everywhere — a Celery hiccup or SendGrid outage must not
    leave the intake stuck "submitted but not registered" from the
    client's point of view.
    """
    from datetime import date

    from .emails import send_intake_received_email

    profile.onboarding_status = 'onboarding_complete'
    profile.onboarding_complete = True
    # Flag the admin Needs You queue so the human review step is
    # tracked alongside the existing email-reply triage. Cleared
    # by the Mark Reviewed button in admin_dashboard.
    profile.needs_admin_review_at = timezone.now()
    profile.admin_reviewed_at = None
    profile.save(update_fields=[
        'onboarding_status', 'onboarding_complete',
        'needs_admin_review_at', 'admin_reviewed_at',
        'updated_at',
    ])

    # Mirror the intake-complete flag onto the per-website record. The
    # ClientProfile above gates the portal, but the Website's OWN
    # onboarding_status is what the admin Website page shows — without
    # this it stays "Pending Intake" forever after the client submits.
    # Only advance from pending_intake, never downgrade a later state.
    try:
        from .account_models import Website
        if isinstance(project, Website) and (
                project.onboarding_status == 'pending_intake'):
            project.onboarding_status = 'intake_complete'
            project.save(update_fields=['onboarding_status', 'updated_at'])
    except Exception:
        logger.exception(
            'Website onboarding_status sync failed for %s', profile.pk)

    # Copy any client-uploaded intake files (logo + photos) into the
    # portal Files page so they live alongside everything else the
    # client has sent us. Best-effort — never block intake on file
    # plumbing.
    try:
        _copy_intake_files_to_documents(profile, project)
    except Exception:
        logger.exception(
            'intake -> Files copy failed for %s', profile.pk)

    # Internal changelog entry (staff-only — surfaces in admin client
    # detail). SiteChangelogEntry import is local so a missing model
    # never breaks intake submission.
    try:
        from .models import SiteChangelogEntry
        SiteChangelogEntry.objects.create(
            website_new=profile,
            change_type='other',
            title='Intake form submitted',
            description=(
                'Client completed intake form. Project started.'),
            date_of_change=date.today(),
            is_client_visible=False,
        )
    except Exception:
        logger.exception(
            'changelog entry failed for %s', profile.pk)

    # Enqueue Droplet provisioning — moved here from the webhook
    # (was previously triggered on deposit_paid, now waits for intake
    # so we don't waste a Droplet on a paid-but-stalled client).
    try:
        from billing.tasks import provision_droplet_task
        provision_droplet_task.delay(str(profile.id))
    except Exception:
        logger.exception(
            'Droplet provisioning enqueue failed for %s', profile.pk)

    # Auto-provision a GA4 property for this build (best-effort). `project`
    # is the active Website; its Measurement ID lands in the build.
    try:
        if project is not None and getattr(project, 'id', None):
            from reporting.tasks import provision_ga4_task
            provision_ga4_task.delay(str(project.id))
    except Exception:
        logger.exception(
            'GA4 provisioning enqueue failed for %s', profile.pk)

    # Google Business Profile — based on the intake answer, email the client
    # the right step-by-step + drop a setup task. 'decline' → do nothing.
    try:
        intake_obj = getattr(profile, 'intake', None)
        gmb = getattr(intake_obj, 'gmb_status', '') if intake_obj else ''
        if gmb in ('have', 'need'):
            from .emails import (
                send_gmb_add_manager_email, send_gmb_create_email,
            )
            try:
                if gmb == 'have':
                    send_gmb_add_manager_email(profile)
                else:
                    send_gmb_create_email(profile)
            except Exception:
                logger.exception('GMB email failed for %s', profile.pk)
            if profile.user_id:
                from onboarding.todo_models import SetupTodo
                title = (
                    'Add Aspired Websites as a manager on your Google '
                    'Business Profile' if gmb == 'have' else
                    'Create your Google Business Profile + add us as manager')
                if not SetupTodo.objects.filter(
                        user=profile.user, task_type='google_access',
                        credential_type='gmb_manager').exists():
                    SetupTodo.objects.create(
                        user=profile.user, task_type='google_access',
                        credential_type='gmb_manager', title=title[:120],
                        description='See the email we just sent for '
                                    'step-by-step instructions.')
    except Exception:
        logger.exception('GMB intake follow-up failed for %s', profile.pk)

    # Confirmation email.
    try:
        send_intake_received_email(profile)
    except Exception:
        logger.exception(
            'intake-received email failed for %s', profile.pk)


# ── Page 4: Files ───────────────────────────────────────────────────────────

@client_required
def files(request):
    project = _active_project(request)
    docs = list(project.documents.all()) if project else []
    ctx = _portal_context(
        request, 'files',
        docs_to_client=[d for d in docs if d.direction == 'to_client'],
        docs_from_client=[d for d in docs if d.direction == 'from_client'],
        upload_form=FileUploadForm(),
    )
    return render(request, 'clients/files.html', ctx)


@client_required
def file_upload(request):
    project = _active_project(request)
    if request.method == 'POST':
        form = FileUploadForm(request.POST, request.FILES)
        if form.is_valid():
            doc = form.save(commit=False)
            doc.website_new = getattr(request, 'website', None)
            doc.direction = 'from_client'
            doc.uploaded_by = request.user
            doc.save()
            messages.success(request, 'File uploaded.')
            return redirect('clients:files')
        ctx = _portal_context(request, 'files', upload_form=form,
                              docs_to_client=[], docs_from_client=[])
        docs = list(project.documents.all()) if project else []
        ctx['docs_to_client'] = [d for d in docs if d.direction == 'to_client']
        ctx['docs_from_client'] = [d for d in docs if d.direction == 'from_client']
        return render(request, 'clients/files.html', ctx)
    return redirect('clients:files')


# ── Page 5: Revisions ───────────────────────────────────────────────────────

def _on_essentials(account):
    """True when the account holds an active Essentials maintenance plan
    (replaces the deprecated ClientProfile.package == 'maintenance_essentials'
    check now that plans are per-Account rows)."""
    if account is None:
        return False
    try:
        return account.maintenance_plans.filter(
            status='active', tier_slug='maintenance-essentials').exists()
    except Exception:
        return False


def _hourly_rate():
    """The out-of-scope hourly rate, from billing AddonPricing (DB-driven)."""
    from billing.pricing_models import AddonPricing
    addon = AddonPricing.objects.filter(slug='addon-hourly').first()
    return f'${addon.price_min:,.0f}' if addon else '$85'


@client_required
def revisions(request):
    account = request.account
    project = _active_project(request)
    revision_list = list(project.revisions.all()) if project else []
    ctx = _portal_context(
        request, 'revisions',
        revision_list=revision_list,
        form=RevisionForm(),
        hourly_rate=_hourly_rate(),
        # Phase 1.4 — surface the work-blocking banner when applicable.
        work_blocked=account.has_unpaid_out_of_scope(),
    )
    return render(request, 'clients/revisions.html', ctx)


@client_required
def revision_new(request):
    account = request.account
    project = _active_project(request)
    if project is None:
        messages.error(request, 'You need an active project to request a revision.')
        return redirect('clients:revisions')

    # Phase 1.4 — work-blocking. CLAUDE.md rule: scope creep generates a
    # MiniInvoice and work is blocked until status == 'paid'. Hard-stop
    # any new major-revision request while any MiniInvoice for this
    # client is pending or sent.
    if account.has_unpaid_out_of_scope():
        messages.error(
            request,
            'You have an unpaid out-of-scope invoice. Please pay it '
            'before submitting another revision request.')
        return redirect('clients:revisions')

    if request.method == 'POST':
        form = RevisionForm(request.POST)
        if form.is_valid():
            revision = form.save(commit=False)
            revision.website_new = getattr(request, 'website', None)
            revision.source = 'aspired_portal'
            revision.counts_against_limit = revision.is_major
            revision.save()

            if revision.is_major:
                project.revision_count += 1
                project.save(update_fields=[
                    'revision_count', 'updated_at'])

            if project.revision_count > project.revision_limit:
                # Out of scope — bill it before work begins.
                revision.status = 'out_of_scope'
                revision.save(update_fields=['status', 'updated_at'])
                _create_revision_mini_invoice(
                    getattr(request, 'website', None), revision,
                    account=account, website=getattr(request, 'website', None))
                messages.warning(
                    request,
                    'This exceeds your included revisions. An out-of-scope '
                    'invoice will be sent before work begins.',
                )
            else:
                messages.success(request, 'Revision request submitted.')

            _notify_admin_revision(
                getattr(request, 'website', None), revision)
            return redirect('clients:revisions')

        ctx = _portal_context(
            request, 'revisions', form=form,
            revision_list=list(project.revisions.all()),
            hourly_rate=_hourly_rate(),
        )
        return render(request, 'clients/revisions.html', ctx)
    return redirect('clients:revisions')


def _create_revision_mini_invoice(profile, revision, account=None,
                                  website=None):
    from billing.models import MiniInvoice
    MiniInvoice.objects.create(
        account_new=account,
        website_new=website,
        revision=revision,
        description=f'Out-of-scope revision: {revision.description[:120]}',
        amount=0,
        hours=0,
        status='pending',
    )


def _notify_admin_revision(profile, revision):
    from django.conf import settings
    from django.core.mail import send_mail
    send_mail(
        subject=f'New revision request — {profile.firm_name}',
        message=f'{profile.firm_name} submitted a revision:\n\n{revision.description}',
        from_email=settings.EMAIL_FROM_NO_REPLY,
        recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
        fail_silently=True,
    )


# ── Page 6: Support ─────────────────────────────────────────────────────────

@client_required
def support(request):
    project = _active_project(request)
    ctx = _portal_context(
        request, 'support',
        tickets=list(project.tickets.all()) if project else [],
        form=SupportTicketForm(),
    )
    return render(request, 'clients/support.html', ctx)


@client_required
def support_new(request):
    project = _active_project(request)
    if request.method == 'POST':
        form = SupportTicketForm(request.POST)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.website_new = getattr(request, 'website', None)
            ticket.account_new = getattr(request, 'account', None)
            ticket.save()
            _notify_admin_ticket(ticket.website_new or ticket.account_new,
                                 ticket)
            messages.success(request, 'Support ticket submitted.')
            return redirect('clients:support')
        ctx = _portal_context(
            request, 'support', form=form,
            tickets=list(project.tickets.all()) if project else [])
        return render(request, 'clients/support.html', ctx)
    return redirect('clients:support')


def _notify_admin_ticket(profile, ticket):
    from django.conf import settings
    from django.core.mail import send_mail
    send_mail(
        subject=f'New support ticket — {profile.firm_name}: {ticket.subject}',
        message=f'Priority: {ticket.get_priority_display()}\n\n{ticket.description}',
        from_email=settings.EMAIL_FROM_NO_REPLY,
        recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
        fail_silently=True,
    )


# ── Page 7: Invoices ────────────────────────────────────────────────────────

@client_required
def invoices(request):
    """Billing history — read from the PaymentRecord ledger (written by the
    Stripe webhooks for every payment: one-time build deposits/finals AND
    recurring subscription charges). Durable + no live Stripe dependency."""
    account = request.account
    records = list(account.payment_records.all())  # ordered -paid_at (Meta)
    invoice_list = [{
        'id': r.id,
        'description': r.description or r.get_kind_display(),
        'kind_label': r.get_kind_display(),
        'amount': r.amount,
        'status': r.status,
        'created': r.paid_at,
        'pdf_url': r.receipt_url,
        'is_open': r.status == 'open',
    } for r in records]
    ctx = _portal_context(
        request, 'invoices',
        invoice_list=invoice_list,
        stripe_error=None,
    )
    return render(request, 'clients/invoices.html', ctx)


@client_required
def invoice_receipt(request, record_id):
    """View/download a branded PDF receipt for one of the client's own
    payments. Scoped to the logged-in client's records (ownership enforced
    via the related manager). Inline so it opens in-browser; the viewer's
    Save still downloads it."""
    from billing.receipt_pdf import render_payment_receipt
    record = get_object_or_404(
        request.account.payment_records, id=record_id)
    content, is_pdf = render_payment_receipt(record)
    short = str(record.id)[:8]
    if is_pdf:
        resp = HttpResponse(content, content_type='application/pdf')
        resp['Content-Disposition'] = (
            f'inline; filename="receipt-{short}.pdf"')
        return resp
    return HttpResponse(content, content_type='text/html')


# ── Page 9: Credentials (PIN-gated client vault) ────────────────────────────

def _client_visible_credentials(account):
    """The credentials staff have shared with this client, ordered for display."""
    from vault.models import ClientVault
    vault = ClientVault.objects.filter(account_new=account).first()
    if not vault:
        return []
    return list(
        vault.credentials.filter(visible_to_client=True)
        .order_by('category', 'sort_order', 'label')
    )


def _valid_pin(raw):
    return raw.isdigit() and len(raw) == 4


def _collect_pin(request):
    """Read the 4 digit boxes (d1..d4), falling back to a single 'pin' field."""
    pin = ''.join((request.POST.get(f'd{i}') or '') for i in range(1, 5)).strip()
    return pin or (request.POST.get('pin') or '').strip()


def _lock_client_pin(account, now):
    """Begin a lockout window after too many failed attempts."""
    account.client_pin_lockout_until = now + timedelta(
        minutes=CLIENT_PIN_LOCKOUT_MINUTES)
    account.client_pin_failed_attempts = 0
    account.save(update_fields=[
        'client_pin_lockout_until', 'client_pin_failed_attempts', 'updated_at',
    ])


@client_required
def portal_credentials(request):
    """
    The client credentials page — gated by a per-client 4-digit PIN.

    First visit: the client sets a PIN. Thereafter the PIN unlocks a
    15-minute viewing window; five wrong PINs trigger a 30-minute lockout.
    This PIN is entirely separate from the admin vault PIN.
    """
    account = request.account
    now = timezone.now()

    # ── First-time PIN setup ──
    if not account.client_pin_set:
        if request.method == 'POST':
            pin = _collect_pin(request)
            confirm = (request.POST.get('pin_confirm') or '').strip()
            error = None
            if not _valid_pin(pin):
                error = 'PIN must be exactly 4 digits.'
            elif pin != confirm:
                error = 'The two PINs do not match.'
            if error:
                ctx = _portal_context(request, 'credentials', pin_error=error)
                return render(request, 'clients/vault_setup_pin.html', ctx)
            salt = generate_salt()
            account.client_pin_salt = salt
            account.client_pin_hash = hash_client_pin(pin, salt)
            account.client_pin_set = True
            account.client_pin_failed_attempts = 0
            account.client_pin_lockout_until = None
            account.save(update_fields=[
                'client_pin_salt', 'client_pin_hash', 'client_pin_set',
                'client_pin_failed_attempts', 'client_pin_lockout_until',
                'updated_at',
            ])
            mark_client_vault_unlocked(request)
            return redirect('clients:credentials')
        ctx = _portal_context(request, 'credentials')
        return render(request, 'clients/vault_setup_pin.html', ctx)

    # ── Locked out? ──
    if account.client_pin_lockout_until and account.client_pin_lockout_until > now:
        ctx = _portal_context(
            request, 'credentials',
            lockout_until=account.client_pin_lockout_until.isoformat(),
        )
        return render(request, 'clients/vault_locked.html', ctx)

    # ── PIN entry ──
    if request.method == 'POST':
        pin = _collect_pin(request)
        salt = bytes(account.client_pin_salt or b'')
        if verify_client_pin(pin, account.client_pin_hash, salt):
            account.client_pin_failed_attempts = 0
            account.client_pin_lockout_until = None
            account.save(update_fields=[
                'client_pin_failed_attempts', 'client_pin_lockout_until',
                'updated_at',
            ])
            mark_client_vault_unlocked(request)
            return redirect('clients:credentials')

        # Wrong PIN.
        account.client_pin_failed_attempts += 1
        if account.client_pin_failed_attempts >= CLIENT_PIN_MAX_ATTEMPTS:
            _lock_client_pin(account, now)
            ctx = _portal_context(
                request, 'credentials',
                lockout_until=account.client_pin_lockout_until.isoformat(),
            )
            return render(request, 'clients/vault_locked.html', ctx)
        account.save(update_fields=['client_pin_failed_attempts', 'updated_at'])
        remaining = CLIENT_PIN_MAX_ATTEMPTS - account.client_pin_failed_attempts
        ctx = _portal_context(
            request, 'credentials',
            pin_error=(f'Incorrect PIN — {remaining} attempt'
                       f'{"" if remaining == 1 else "s"} remaining before a '
                       f'{CLIENT_PIN_LOCKOUT_MINUTES}-minute lockout.'),
        )
        return render(request, 'clients/vault_enter_pin.html', ctx)

    # ── Unlocked — show the credentials ──
    if is_client_vault_unlocked(request):
        import json as _json
        from vault.models import TYPES_BY_CATEGORY, VaultCredential
        all_creds = _client_visible_credentials(account)
        ctx = _portal_context(
            request, 'credentials',
            credentials=all_creds,  # kept for any existing template refs
            shared_credentials=[c for c in all_creds
                                if not c.created_by_client],
            own_credentials=[c for c in all_creds if c.created_by_client],
            cred_categories=VaultCredential.CATEGORY_CHOICES,
            cred_types_json=_json.dumps(TYPES_BY_CATEGORY),
            remaining_seconds=get_client_vault_remaining_seconds(request),
        )
        return render(request, 'clients/vault_credentials.html', ctx)

    # ── Locked — ask for the PIN ──
    ctx = _portal_context(request, 'credentials')
    return render(request, 'clients/vault_enter_pin.html', ctx)


@client_required
@require_POST
def portal_credentials_reauth(request):
    """
    HTMX re-auth from the session-expiry overlay on the credentials page.

    On success: refreshes the 15-minute window and fires HX-Trigger
    'vaultReauthed' so the page reloads. On lockout (or no PIN set):
    HX-Redirect back to the credentials page, which then renders the right
    screen (locked / setup).
    """
    account = request.account
    now = timezone.now()
    credentials_url = reverse('clients:credentials')

    def _redirect():
        resp = HttpResponse(status=204)
        resp['HX-Redirect'] = credentials_url
        return resp

    if not account.client_pin_set:
        return _redirect()
    if account.client_pin_lockout_until and account.client_pin_lockout_until > now:
        return _redirect()

    pin = _collect_pin(request)
    salt = bytes(account.client_pin_salt or b'')
    if verify_client_pin(pin, account.client_pin_hash, salt):
        account.client_pin_failed_attempts = 0
        account.client_pin_lockout_until = None
        account.save(update_fields=[
            'client_pin_failed_attempts', 'client_pin_lockout_until',
            'updated_at',
        ])
        mark_client_vault_unlocked(request)
        resp = HttpResponse(status=204)
        resp['HX-Trigger'] = 'vaultReauthed'
        return resp

    # Wrong PIN.
    account.client_pin_failed_attempts += 1
    if account.client_pin_failed_attempts >= CLIENT_PIN_MAX_ATTEMPTS:
        _lock_client_pin(account, now)
        return _redirect()
    account.save(update_fields=['client_pin_failed_attempts', 'updated_at'])
    remaining = CLIENT_PIN_MAX_ATTEMPTS - account.client_pin_failed_attempts
    return render(request, 'clients/_vault_reauth_error.html', {
        'pin_error': (f'Incorrect PIN — {remaining} attempt'
                      f'{"" if remaining == 1 else "s"} left.'),
    })


@client_required
@require_POST
def portal_credentials_add(request):
    """
    Client adds their OWN credential from the portal — same idea as the
    admin vault add, mirrored for the client.

    Sensitive fields are encrypted with the server key (VAULT_SERVER_SECRET),
    so the admin's re-encrypt-on-unlock pulls them into the admin vault
    automatically — staff ALWAYS see client-added creds. `created_by_client`
    keeps them separate from staff-shared creds in both views. Plain copies
    are stored for the PIN-gated portal display, mirroring shared creds.

    The post_save signal on VaultCredential auto-completes a matching
    onboarding SetupTodo when the credential_type lines up.
    """
    from vault.models import (
        ClientVault, VaultCredential, TYPES_BY_CATEGORY)
    from vault.crypto import derive_server_key, encrypt_value, make_hint

    account = request.account
    # Adding requires the vault to be unlocked (PIN), same as viewing.
    if not is_client_vault_unlocked(request):
        return redirect('clients:credentials')

    category = (request.POST.get('category') or 'other').strip()
    cred_type = (request.POST.get('credential_type') or 'other').strip()
    label = (request.POST.get('label') or '').strip()
    custom_label = (request.POST.get('custom_label') or '').strip()
    username = (request.POST.get('username') or '').strip()
    password = (request.POST.get('password') or '')
    url = (request.POST.get('url') or '').strip()
    notes = (request.POST.get('notes') or '').strip()

    if category not in dict(VaultCredential.CATEGORY_CHOICES):
        category = 'other'
    valid_types = {t for t, _ in TYPES_BY_CATEGORY.get(category, [])}
    if cred_type not in valid_types:
        cred_type = 'other'
    if not label:
        label = custom_label or dict(
            TYPES_BY_CATEGORY.get(category, [])).get(cred_type, 'Credential')
    if not (username or password or url):
        messages.error(
            request, 'Add at least a username, password, or URL.')
        return redirect('clients:credentials')

    vault = ClientVault.objects.filter(account_new=account).first()
    if vault is None:
        vault = ClientVault.objects.filter(
            account_new=account).first()
    if vault is None:
        vault = ClientVault.objects.create(account_new=account)
    elif vault.account_new_id is None:
        vault.account_new = account
        vault.save(update_fields=['account_new', 'updated_at'])
    key = derive_server_key()
    cred = VaultCredential(
        vault=vault,
        label=label,
        category=category,
        credential_type=cred_type,
        custom_label=custom_label,
        visible_to_client=True,
        created_by_client=True,
        encrypted_with_server_key=True,
        username_encrypted=encrypt_value(username, key),
        password_encrypted=encrypt_value(password, key),
        url_encrypted=encrypt_value(url, key),
        notes_encrypted=encrypt_value(notes, key),
        username_hint=make_hint(username),
        client_username_plain=username,
        client_password_plain=password,
        client_url_plain=url,
        client_notes_plain=notes,
    )
    cred.save()  # post_save signal auto-completes a matching SetupTodo
    messages.success(
        request, 'Credential added — we can see it on our end too.')
    return redirect('clients:credentials')


# ── Page 10: Activity Log (client site changelog) ───────────────────────────

@client_required
def portal_changelog(request):
    """The client-facing site changelog — grouped by month, month-filterable."""
    project = _active_project(request)
    visible = (project.changelog_entries.filter(is_client_visible=True)
               if project else [])

    # Month options from the full visible set (newest-first via model Meta).
    month_options = []
    seen = set()
    for change_date in visible.values_list('date_of_change', flat=True):
        key = change_date.strftime('%Y-%m')
        if key not in seen:
            seen.add(key)
            month_options.append({
                'value': key,
                'label': change_date.strftime('%B %Y'),
            })

    month_filter = request.GET.get('month', '')
    entries = visible
    if month_filter:
        try:
            year, mon = month_filter.split('-')
            entries = entries.filter(
                date_of_change__year=int(year),
                date_of_change__month=int(mon),
            )
        except (ValueError, TypeError):
            month_filter = ''

    # Group the (already date-ordered) entries by calendar month.
    grouped = []
    current = None
    for entry in entries:
        key = entry.date_of_change.strftime('%Y-%m')
        if current is None or current['key'] != key:
            current = {
                'key': key,
                'label': entry.date_of_change.strftime('%B %Y'),
                'entries': [],
            }
            grouped.append(current)
        current['entries'].append(entry)

    ctx = _portal_context(
        request, 'changelog',
        changelog_months=grouped,
        month_options=month_options,
        month_filter=month_filter,
    )
    return render(request, 'clients/portal_changelog.html', ctx)


# ── Page 11: SEO & Conversions ──────────────────────────────────────────────

@client_required
def portal_seo(request):
    """
    Keyword rankings + conversion activity + Tier 1 visitor
    analytics (page views, time on page, scroll depth, top pages)
    for the client.
    """
    project = _active_project(request)
    account = getattr(request, 'account', None)
    from reporting.analytics_helpers import (
        exit_intent_insight, overview_stats, scroll_insight,
        top_pages,
    )
    from reporting.conversion_helpers import (
        conversion_6month_chart, conversion_counts,
    )
    from reporting.keyword_helpers import (
        build_keyword_rows, keyword_insight,
    )

    rows = build_keyword_rows(project, active_only=True)
    overview = overview_stats(project)

    ctx = _portal_context(
        request, 'seo',
        keyword_rows=rows,
        keyword_insight=keyword_insight(rows),
        conversion_counts=conversion_counts(project),
        conversion_chart=conversion_6month_chart(project),
        analytics_overview=overview,
        analytics_top_pages=top_pages(project, limit=5),
        scroll_insight=scroll_insight(overview['avg_scroll_depth']),
        exit_intent_insight=exit_intent_insight(
            overview['exit_intent_rate']),
        session_recording_enabled=bool(
            getattr(project, 'session_recording_enabled', False)),
        on_essentials=_on_essentials(account),
    )
    return render(request, 'clients/portal_seo.html', ctx)


# ── Page 12: Monthly Reports ────────────────────────────────────────────────

@client_required
def portal_reports(request):
    """
    Monthly performance reports plus the year-in-review (Phase 7
    Part 4) annual reports the client can download once they're
    `ready` or `sent`.
    """
    project = _active_project(request)
    flt = _owner_filter(request)
    from reporting.models import MonthlyReport
    from .models import AnnualReport
    reports = list(MonthlyReport.objects.filter(**flt, status='sent'))
    annual_reports = list(AnnualReport.objects.filter(
        **flt, status__in=['ready', 'sent']
    ).order_by('-report_year'))
    ctx = _portal_context(
        request, 'reports',
        reports=reports,
        latest=reports[0] if reports else None,
        annual_reports=annual_reports,
    )
    return render(request, 'clients/portal_reports.html', ctx)


@client_required
def portal_recordings(request):
    """
    Client-facing list of their own site's session recordings.
    Same data the operator sees in /admin-dashboard/clients/<id>/
    recordings/, with the prominent retention notice on top.

    When session_recording_enabled=False we still render the page,
    but it shows the upgrade prompt instead of the table — keeps
    the nav link from 404'ing on a deep-link.
    """
    from datetime import timedelta

    from django.db.models import Avg, Count, Sum

    from reporting.models import SessionRecording

    project = _active_project(request)
    account = getattr(request, 'account', None)
    enabled = bool(getattr(project, 'session_recording_enabled', False))

    flt = _owner_filter(request)
    recordings = SessionRecording.objects.filter(**flt)
    stats = recordings.aggregate(
        total=Count('id'),
        avg_dur=Avg('duration_seconds'),
        total_kb=Sum('estimated_size_kb'),
    )
    expiring_soon = recordings.filter(
        expires_at__lte=timezone.now() + timedelta(days=7),
    ).count()

    # Most-visited page across this client's recordings.
    top = (recordings.values('page_url')
           .annotate(n=Count('id')).order_by('-n').first())
    most_visited = top['page_url'] if top else ''

    ctx = _portal_context(
        request, 'recordings',
        enabled=enabled,
        on_essentials=_on_essentials(account),
        recordings=recordings.order_by('-created_at')[:100],
        total_recordings=stats['total'] or 0,
        avg_duration_display=_format_seconds_simple(stats['avg_dur']),
        most_visited=most_visited,
        expiring_soon=expiring_soon,
    )
    return render(request, 'clients/portal_recordings.html', ctx)


def _format_seconds_simple(s):
    if not s:
        return '—'
    s = int(round(s))
    if s < 60:
        return f'{s}s'
    return f'{s // 60}m {s % 60}s'


@client_required
def portal_recording_download(request, rec_id):
    """Client-side download — same self-contained HTML as the admin."""
    from pathlib import Path

    from django.conf import settings as _s
    from django.http import HttpResponse

    from reporting.models import SessionRecording

    rec = get_object_or_404(
        SessionRecording, id=rec_id,
        website_new__account=request.account)

    static_root = Path(_s.BASE_DIR) / 'core' / 'static' / 'js'
    try:
        rrweb_js = (static_root / 'rrweb.min.js').read_text(
            encoding='utf-8')
    except OSError:
        rrweb_js = ''

    import json as _json
    events_json = _json.dumps(rec.get_all_events(), default=str)

    safe_page = (rec.page_url or '').replace(
        'https://', '').replace('http://', '').replace('/', '_')[:60]
    safe_page = safe_page or 'page'
    filename = (f'recording-{rec.created_at:%Y%m%d-%H%M}-'
                f'{safe_page}.html')

    body = render(request, 'admin_dashboard/recording_download.html', {
        'website': rec.website_new,
        'recording': rec,
        'rrweb_js': rrweb_js,
        'events_json': events_json,
    }).content

    resp = HttpResponse(body, content_type='text/html')
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@client_required
def portal_recording_replay(request, rec_id):
    """Client-facing replay viewer."""
    import json as _json

    from django.core.serializers.json import DjangoJSONEncoder

    from reporting.models import SessionRecording

    rec = get_object_or_404(
        SessionRecording, id=rec_id,
        website_new__account=request.account)

    events = rec.get_all_events()
    first_event_type = (events[0].get('type')
                        if events and isinstance(events[0], dict)
                        else None)
    has_full_snapshot = any(
        isinstance(e, dict) and e.get('type') == 2 for e in events)

    ctx = _portal_context(
        request, 'recordings',
        recording=rec,
        events_json=_json.dumps(events, cls=DjangoJSONEncoder),
        event_count=len(events),
        first_event_type=first_event_type,
        has_full_snapshot=has_full_snapshot,
    )
    return render(request, 'clients/portal_recording_replay.html', ctx)


@client_required
def portal_annual_report_download(request, report_id):
    """Serve an annual report PDF to the client who owns it."""
    import os

    from django.conf import settings
    from django.http import FileResponse, Http404

    from .models import AnnualReport
    report = get_object_or_404(
        AnnualReport, id=report_id,
        website_new__account=request.account,
        status__in=['ready', 'sent'],
    )
    abs_path = os.path.join(settings.MEDIA_ROOT, report.pdf_path or '')
    if not report.pdf_path or not os.path.exists(abs_path):
        raise Http404('Annual report file not found.')
    return FileResponse(
        open(abs_path, 'rb'), as_attachment=True,
        filename=os.path.basename(abs_path),
    )


@client_required
def portal_security(request):
    """
    The client's security scan history — completed VulnerabilityScan
    records ordered newest-first, with the most-recent one called out
    in a prominent card.
    """
    from reporting.models import VulnerabilityScan

    flt = _owner_filter(request)
    scans = list(
        VulnerabilityScan.objects
        .filter(**flt, status='complete')
        .order_by('-completed_at')
    )
    latest = scans[0] if scans else None
    older = scans[1:]

    open_critical_or_high = False
    if latest:
        open_critical_or_high = latest.findings.filter(
            status='open', severity__in=('critical', 'high')
        ).exists()

    ctx = _portal_context(
        request, 'security',
        scans=scans,
        latest=latest,
        older_scans=older,
        open_critical_or_high=open_critical_or_high,
    )
    return render(request, 'clients/portal_security.html', ctx)


@client_required
def portal_scan_download(request, scan_id):
    """
    Serve a completed scan's PDF to the client who owns it. 404 on
    any cross-client access attempt. `pdf_path` is RELATIVE to MEDIA_ROOT.
    """
    import os

    from django.conf import settings
    from django.http import FileResponse, Http404

    from reporting.models import VulnerabilityScan

    scan = get_object_or_404(
        VulnerabilityScan,
        id=scan_id, website_new__account=request.account, status='complete',
    )
    if not scan.pdf_path:
        raise Http404('Report not generated yet.')
    abs_path = os.path.join(settings.MEDIA_ROOT, scan.pdf_path)
    if not os.path.exists(abs_path):
        raise Http404('Report file not found on disk.')
    return FileResponse(
        open(abs_path, 'rb'),
        as_attachment=True,
        filename=os.path.basename(abs_path),
    )


@client_required
def portal_report_download(request, report_id):
    """Serve a monthly report file to the client who owns it."""
    import os

    from django.conf import settings
    from django.http import FileResponse, Http404

    from reporting.models import MonthlyReport

    report = get_object_or_404(
        MonthlyReport, id=report_id,
        website_new__account=request.account)
    abs_path = os.path.join(settings.MEDIA_ROOT, report.pdf_path or '')
    if not report.pdf_path or not os.path.exists(abs_path):
        raise Http404('Report file not found.')
    if not report.opened:
        report.opened = True
        report.opened_at = timezone.now()
        report.save(update_fields=['opened', 'opened_at', 'updated_at'])
    return FileResponse(
        open(abs_path, 'rb'), as_attachment=True,
        filename=os.path.basename(abs_path))


# ── Page 8: Settings ────────────────────────────────────────────────────────

@client_required
def settings_page(request):
    account = request.account
    if request.method == 'POST':
        form = SettingsForm(request.POST, instance=account)
        if form.is_valid():
            form.save()
            messages.success(request, 'Settings saved.')
            return redirect('clients:settings')
    else:
        form = SettingsForm(instance=account)
    ctx = _portal_context(request, 'settings', form=form)
    return render(request, 'clients/settings.html', ctx)


# ── Contract signing (token-gated, no login required) ───────────────────────

def contract_sign(request, contract_token):
    """
    Show a contract and capture the client's signature.

    Auth is the unguessable UUID token in the URL — the client reaches this
    page from an emailed link and may not be logged in.
    """
    contract = get_object_or_404(Contract, contract_token=contract_token)

    if contract.signed:
        return render(request, 'clients/contract_sign.html', {
            'contract': contract,
            'already_signed': True,
        })

    error = None
    if request.method == 'POST':
        signed_name = (request.POST.get('signed_name') or '').strip()
        agreed = request.POST.get('agree') == 'on'
        if not signed_name:
            error = 'Please type your full legal name to sign.'
        elif not agreed:
            error = 'You must check the box agreeing to the terms before signing.'
        else:
            import hashlib as _hashlib
            contract.signed = True
            contract.signed_at = timezone.now()
            contract.signed_ip = request.META.get('REMOTE_ADDR')
            contract.signed_name = signed_name
            # Phase 2.3 — audit-trail hardening for ESIGN/UETA defence.
            # We capture both the browser user-agent AND a SHA-256 of
            # the contract text at signing time. Re-hashing the stored
            # contract_text must reproduce signed_content_hash, proving
            # nothing changed in the document after the signature.
            contract.signed_user_agent = (
                request.META.get('HTTP_USER_AGENT') or '')[:400]
            contract.signed_content_hash = _hashlib.sha256(
                (contract.contract_text or '').encode('utf-8')).hexdigest()
            contract.pdf_path = render_contract_pdf(contract)
            contract.save()

            # Build contracts flow straight into the inline payment page
            # (sign → pay → account setup → intake). Maintenance/social-only
            # contracts just record the signed agreement — billing for those
            # recurring plans is handled separately (self-serve checkout or
            # operator setup).
            if contract.includes_build:
                # Set the build fields on the client (post-2026-05-25 the
                # former Project fields live directly on ClientProfile).
                client = contract.client
                client.package = (
                    contract.package or client.package or '')
                client.stage = 'intake'
                client.payment_status = 'awaiting_deposit'
                client.save(update_fields=[
                    'package', 'stage', 'payment_status', 'updated_at'])
                # Drive the per-website sales lifecycle (source of truth).
                web = contract.website_new
                if web is not None:
                    web.lifecycle_status = 'contract_signed'
                    web.payment_status = 'awaiting_deposit'
                    web.save(update_fields=[
                        'lifecycle_status', 'payment_status', 'updated_at'])
                # → choose deposit / pay-in-full, then the Stripe page.
                return redirect(
                    'clients:contract_pay',
                    contract_token=contract.contract_token)

            send_contract_signed_email(contract)
            return redirect('clients:contract_signed')

    return render(request, 'clients/contract_sign.html', {
        'contract': contract,
        'error': error,
    })


def contract_signed(request):
    """Post-signing thank-you page (non-build contracts)."""
    return render(request, 'clients/contract_signed.html', {})


def contract_pay(request, contract_token):
    """
    Deposit / pay-in-full choice for a signed build contract, then hand the
    client off to the inline Stripe Elements page.

    GET  → show the two amounts (50% deposit or pay in full).
    POST → create the OnboardingInvoice + PaymentIntent for the chosen
           amount and redirect to /pay/<token>/. After payment the client
           flows into account setup and then the intake form.
    """
    from decimal import Decimal

    from django.contrib import messages

    contract = get_object_or_404(Contract, contract_token=contract_token)

    # Guard: only signed build contracts reach the payment step.
    if not contract.signed:
        return redirect('clients:contract_sign',
                        contract_token=contract_token)
    if not contract.includes_build:
        return redirect('clients:contract_signed')

    full = contract.build_price or Decimal('0')
    deposit = contract.deposit_amount or (full / 2)
    remaining = full - deposit  # balance invoiced on delivery

    if request.method == 'POST':
        pay_in_full = request.POST.get('amount_choice') == 'full'
        amount = full if pay_in_full else deposit
        from billing.stripe_helpers import start_contract_payment
        try:
            invoice = start_contract_payment(
                contract, amount, is_deposit=not pay_in_full)
        except Exception:
            logger.exception(
                'start_contract_payment failed for contract %s', contract.pk)
            invoice = None
        if invoice is None:
            messages.error(
                request,
                'We could not start the payment right now. Please contact '
                'us at zacherylong@aspiredwebsites.com.')
            return redirect('clients:contract_signed')
        return redirect('pay_invoice', token=invoice.payment_token)

    return render(request, 'clients/contract_pay.html', {
        'contract': contract,
        'deposit': deposit,
        'full': full,
        'remaining': remaining,
    })


# ── Phase 7 Part 2 — Public referral + proposal tracking endpoints ─────────

def _hash_ip(request):
    """Sha-256 the visitor IP for dedup tracking. Never store raw IP."""
    import hashlib
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    ip = (xff.split(',')[0].strip() if xff
          else request.META.get('REMOTE_ADDR', '') or '')
    return hashlib.sha256(ip.encode()).hexdigest() if ip else ''


def referral_click(request, code):
    """
    Public ``/ref/<code>/`` — counts a click, stores the referral
    code in the session, and redirects to the home page with the
    code as a query param so analytics can see it.

    De-dupes clicks by hashed IP within a 24-hour window.
    """
    from datetime import timedelta

    from .models import ReferralEvent, ReferralLink

    try:
        link = ReferralLink.objects.get(
            code=code.upper(), is_active=True)
    except ReferralLink.DoesNotExist:
        return redirect('/')

    ip_hash = _hash_ip(request)
    recent = ReferralEvent.objects.filter(
        referral_link=link,
        ip_hash=ip_hash,
        event_type='click',
        created_at__gte=timezone.now() - timedelta(hours=24),
    ).exists()

    if not recent:
        ReferralEvent.objects.create(
            referral_link=link,
            event_type='click',
            ip_hash=ip_hash,
        )
        link.clicks = (link.clicks or 0) + 1
        link.save(update_fields=['clicks', 'updated_at'])

    # Carry the code through to the contact form's Lead creation.
    request.session['referral_code'] = code.upper()
    return redirect(f'/?ref={code.upper()}')


def credit_referral_for_lead(lead, code):
    """
    Called from `public.views.contact` after a Lead is saved. Resolves
    the code to a ReferralLink, stamps the lead, increments counters,
    and records the ReferralEvent. Best-effort — never raises into the
    contact-form happy path.
    """
    from .models import ReferralEvent, ReferralLink

    if not (code and lead and lead.pk):
        return
    try:
        link = ReferralLink.objects.get(
            code=code.upper(), is_active=True)
    except ReferralLink.DoesNotExist:
        return

    if not lead.referral_code:
        lead.referral_code = link.code
        lead.save(update_fields=['referral_code', 'updated_at'])

    link.leads_generated = (link.leads_generated or 0) + 1
    link.save(update_fields=['leads_generated', 'updated_at'])

    ReferralEvent.objects.create(
        referral_link=link,
        event_type='lead',
        lead=lead,
    )


def proposal_view_tracking(request, token):
    """
    Public ``/proposals/view/<uuid>/`` — records the open, then serves
    the PDF inline. If the PDF doesn't exist yet we redirect to a
    branded fallback so the prospect always sees something.
    """
    from pathlib import Path

    from django.conf import settings
    from django.http import FileResponse, HttpResponseNotFound

    from .models import Proposal

    try:
        proposal = Proposal.objects.get(tracking_token=token)
    except (Proposal.DoesNotExist, ValueError):
        return HttpResponseNotFound('Proposal not found.')

    proposal.view_count = (proposal.view_count or 0) + 1
    if proposal.viewed_at is None:
        proposal.viewed_at = timezone.now()
    if proposal.status == 'sent':
        proposal.status = 'viewed'
    proposal.save(update_fields=[
        'view_count', 'viewed_at', 'status', 'updated_at',
    ])

    if not proposal.pdf_path:
        return render(request, 'clients/proposal_pending.html',
                      {'proposal': proposal})

    abs_path = Path(settings.MEDIA_ROOT) / proposal.pdf_path
    if not abs_path.exists():
        return render(request, 'clients/proposal_pending.html',
                      {'proposal': proposal})

    content_type = ('application/pdf' if abs_path.suffix.lower() == '.pdf'
                    else 'text/html')
    return FileResponse(open(abs_path, 'rb'),
                        content_type=content_type)


# ── Phase 7 Part 3 — Website Intelligence approve/decline + portal ─────────

# Statuses the client portal lists — everything they've been notified
# about or have already acted on.
_PORTAL_INTEL_STATUSES = (
    'sent_to_client', 'client_approved', 'client_declined',
    'out_of_scope_offered', 'in_scope', 'implemented',
)


def _intel_record_response(suggestion, action):
    """Stamp the suggestion + send the admin notification email."""
    suggestion.client_responded_at = timezone.now()
    suggestion.status = (
        'client_approved' if action == 'approve' else 'client_declined')
    suggestion.save(update_fields=[
        'status', 'client_responded_at', 'updated_at'])

    try:
        from django.conf import settings as _s
        from django.core.mail import send_mail as _send_mail
        verb = 'APPROVED' if action == 'approve' else 'DECLINED'
        _send_mail(
            subject=(f'[Intelligence] {owner_label(suggestion)} '
                     f'{verb}: {suggestion.title[:60]}'),
            message=(
                f'{owner_label(suggestion)} {verb.lower()} the '
                f'suggestion "{suggestion.title}" '
                f'(${suggestion.one_time_fee}).\n\n'
                f'Review: {_s.SITE_BASE_URL}/admin-dashboard/'
                f'intelligence/suggestions/{suggestion.id}/\n'),
            from_email=getattr(_s, 'EMAIL_FROM_NO_REPLY',
                               _s.DEFAULT_FROM_EMAIL),
            recipient_list=[_s.LEAD_NOTIFICATION_EMAIL],
            fail_silently=True,
        )
    except Exception:
        logger.exception('admin alert for intel response failed')


def _intel_respond(request, token, action):
    """Record an approve/decline and bounce back into the portal
    Recommendations page with a flash message.

    Used by BOTH the email link (a GET that's now login-gated by
    @client_required, so the client signs in first) and the in-portal
    Approve / Not Now buttons (a POST). Either way the client stays
    inside the portal shell — no standalone page. The response is scoped
    to the logged-in client's own account, so a leaked token can't be
    used to act on someone else's recommendation.
    """
    from .models import IntelligenceSuggestion

    s = IntelligenceSuggestion.objects.filter(response_token=token).first()
    if s is None:
        messages.error(
            request,
            "That recommendation link is invalid or has been removed.")
        return redirect('clients:portal_suggestions')

    owned = _owns(request, s)
    if not owned:
        messages.error(
            request, "That recommendation isn't on your account.")
        return redirect('clients:portal_suggestions')

    if s.client_responded_at is not None:
        messages.info(request, f'You already responded to "{s.title}".')
        return redirect('clients:portal_suggestions')

    _intel_record_response(s, action)
    if action == 'approve':
        messages.success(
            request,
            f'Approved "{s.title}". We\'ll be in touch about next steps.')
    else:
        messages.success(
            request,
            f'Got it — we\'ve marked "{s.title}" as not now.')
    return redirect('clients:portal_suggestions')


@client_required
def intelligence_approve(request, token):
    """Approve a recommendation. Login-gated: an email click signs in
    first, then lands back in the portal."""
    return _intel_respond(request, token, 'approve')


@client_required
def intelligence_decline(request, token):
    """Decline a recommendation. Login-gated like the approve view."""
    return _intel_respond(request, token, 'decline')


# ── Phase 7 Part 3 — Client portal suggestions list ────────────────────────

@client_required
def portal_suggestions(request):
    """Portal page that mirrors what the client received via email."""
    from .models import IntelligenceSuggestion

    flt = _owner_filter(request)
    suggestions = (
        IntelligenceSuggestion.objects
        .filter(**flt, status__in=_PORTAL_INTEL_STATUSES)
        .order_by('-sent_to_client_at', '-generated_at')
    )
    pending_response = any(
        s.is_actionable_by_client for s in suggestions)

    return render(request, 'clients/portal_suggestions.html',
                  _portal_context(
                      request, 'suggestions',
                      suggestions=suggestions,
                      pending_response=pending_response,
                  ))


# ── Portal subscriptions + payment methods ─────────────────────────────────

def _ts_to_dt(value):
    """Convert a Stripe unix timestamp to a datetime so Django's |date filter
    renders it. Returns None for falsy input."""
    if not value:
        return None
    try:
        from datetime import datetime, timezone as _dttz
        return datetime.fromtimestamp(int(value), tz=_dttz.utc)
    except Exception:
        return None


def _subscription_discounted(stripe_sub, amount):
    """Apply the subscription's live discount to a list-price amount.

    A discounted subscription otherwise renders at list price: a plan
    Stripe bills at $149.50 showed "$299/month" on the client's own
    billing page, because the amount came straight off
    `price.unit_amount` and nothing ever looked at the coupon. Handles
    the `discounts` array and the legacy singular `discount`; entries
    that arrive unexpanded (bare ids) are skipped rather than guessed at.
    """
    entries = list(getattr(stripe_sub, 'discounts', None) or [])
    if not entries:
        legacy = getattr(stripe_sub, 'discount', None)
        entries = [legacy] if legacy is not None else []
    for entry in entries:
        if isinstance(entry, str):
            continue
        coupon = getattr(entry, 'coupon', None)
        if coupon is None:
            continue
        percent_off = getattr(coupon, 'percent_off', None)
        amount_off = getattr(coupon, 'amount_off', None)
        if percent_off:
            amount = amount * (1 - float(percent_off) / 100.0)
        elif amount_off:
            amount = amount - (float(amount_off) / 100.0)
    return max(round(amount, 2), 0)


def _subscription_card(stripe_sub):
    """Normalise a Stripe Subscription into a flat dict for the template.

    Stripe Python v8+ removed dict-like .get() on StripeObject — every
    field is attribute-only. Wrap each access with getattr+default so a
    missing or unexpanded field doesn't 500 the page."""
    if stripe_sub is None:
        return None

    items_obj = getattr(stripe_sub, 'items', None)
    items_data = list(items_obj.data) if (
        items_obj is not None and hasattr(items_obj, 'data')) else []
    price = items_data[0].price if items_data else None

    list_amount = (getattr(price, 'unit_amount', 0) or 0) / 100 if price else 0
    amount = _subscription_discounted(stripe_sub, list_amount)
    recurring = getattr(price, 'recurring', None) if price else None
    interval = getattr(recurring, 'interval', '') if recurring else ''

    product_name = ''
    product_ref = getattr(price, 'product', None) if price else None
    if product_ref:
        try:
            import stripe as _stripe
            from django.conf import settings as _s
            _stripe.api_key = _s.STRIPE_SECRET_KEY
            prod = _stripe.Product.retrieve(product_ref)
            product_name = getattr(prod, 'name', '') or ''
        except Exception:
            pass

    return {
        'id': getattr(stripe_sub, 'id', ''),
        'status': getattr(stripe_sub, 'status', ''),
        'amount': amount,
        # List price, kept so a template can strike it through when a
        # discount is in play. Equal to `amount` when there's no discount.
        'list_amount': list_amount,
        'is_discounted': amount != list_amount,
        'interval': interval,
        'product_name': product_name,
        'cancel_at_period_end': getattr(
            stripe_sub, 'cancel_at_period_end', False),
        'current_period_end': _ts_to_dt(
            getattr(stripe_sub, 'current_period_end', None)),
        'trial_end': _ts_to_dt(getattr(stripe_sub, 'trial_end', None)),
        # Phase C4 — Stripe's own per-subscription payment-method
        # override. None / '' means the subscription uses the
        # customer-level default. The portal renders a dropdown that
        # writes back via portal_subscription_payment_method.
        'default_payment_method': (
            getattr(stripe_sub, 'default_payment_method', '') or ''),
    }


@client_required
def portal_subscriptions(request):
    """
    Client-facing subscriptions + payment-methods page. Lists active
    recurring charges (hosting, maintenance, domain when wired) and
    every saved card on the Stripe customer. The default card is what
    drives every renewal; the client can add/remove/set-default here.
    """
    import stripe as _stripe
    from django.conf import settings as _s

    # `Account` is read below for the comp-tier labels and was never
    # imported here, so this page raised NameError for any client with a
    # comped package — the branch only runs when `comp_build_package` is
    # set, which is why it survived: nothing in the suite renders this
    # page for a comped account.
    from clients.account_models import Account
    from billing.stripe_helpers import (
        get_customer_default_payment_method,
        list_customer_payment_methods,
    )

    account = request.account
    _stripe.api_key = _s.STRIPE_SECRET_KEY

    subscriptions = []
    if account.stripe_customer_id:
        # Map every Stripe subscription id across the account's maintenance
        # + social plans and per-website hosting subs to a friendly label
        # (Stripe test-mode product names are unreliable, so the plan tier
        # is the authoritative title).
        label_by_sub = {}
        # Queued downgrades (tier_slug stays current; the lower tier is
        # scheduled at period end). Surfaced on the card as a "scheduled"
        # note so the client sees the upcoming switch + no charge until then.
        pending_by_sub = {}
        for plan in account.maintenance_plans.exclude(
                stripe_subscription_id=''):
            label_by_sub[plan.stripe_subscription_id] = (
                f'Maintenance — {plan.get_tier_slug_display()}')
            if plan.pending_tier_slug:
                pending_by_sub[plan.stripe_subscription_id] = {
                    'tier': plan.pending_tier_display,
                    'date': plan.pending_tier_effective,
                }
        for plan in account.social_media_plans.filter(
                status__in=('active', 'cancelled'),
                ).exclude(stripe_subscription_id=''):
            label_by_sub[plan.stripe_subscription_id] = (
                f'Social Media — {plan.get_tier_slug_display()}')
            if plan.pending_tier_slug:
                pending_by_sub[plan.stripe_subscription_id] = {
                    'tier': plan.pending_tier_display,
                    'date': plan.pending_tier_effective,
                }
        for w in account.websites.exclude(stripe_hosting_subscription_id=''):
            label_by_sub[w.stripe_hosting_subscription_id] = (
                f'Hosting — {w.name}')
        for sub_id in label_by_sub:
            try:
                # `discounts` must be expanded or it comes back as bare
                # ids and the card can't tell a discounted plan from a
                # full-price one.
                sub = _stripe.Subscription.retrieve(
                    sub_id, expand=['discounts'])
                if getattr(sub, 'status', '') in (
                        'active', 'trialing', 'past_due', 'unpaid'):
                    card = _subscription_card(sub)
                    if card is not None:
                        # Authoritative title from the plan tier; fall back
                        # to the Stripe product name only if unmapped.
                        card['product_name'] = (
                            label_by_sub.get(sub_id)
                            or card.get('product_name') or 'Subscription')
                        card['pending_change'] = pending_by_sub.get(sub_id)
                        subscriptions.append(card)
            except Exception:
                logger.exception(
                    'Subscription fetch failed for %s', sub_id)

    # Phase 5d — surface operator-granted comp tiers as pseudo-cards
    # alongside Stripe subscriptions. The template renders these with a
    # "Comped" badge instead of pricing + Stripe controls.
    comp_subscriptions = []
    if account.comp_build_package:
        label = dict(Account.BUILD_COMP_CHOICES).get(
            account.comp_build_package, account.comp_build_package)
        comp_subscriptions.append({
            'id': f'comp-build-{account.id}',
            'product_name': label,
            'status': 'comped',
            'amount': 0,
            'interval': '',
            'kind': 'build',
            'is_comped': True,
            'comp_notes': account.comp_notes,
        })
    if account.comp_maintenance_package:
        label = dict(Account.MAINTENANCE_COMP_CHOICES).get(
            account.comp_maintenance_package,
            account.comp_maintenance_package)
        comp_subscriptions.append({
            'id': f'comp-maint-{account.id}',
            'product_name': label,
            'status': 'comped',
            'amount': 0,
            'interval': 'month',
            'kind': 'maintenance',
            'is_comped': True,
            'comp_notes': account.comp_notes,
        })
    if (account.comp_social_tier
            and not account.social_media_plans.filter(
                status='active').exclude(stripe_subscription_id='').exists()):
        label = dict(Account.SOCIAL_COMP_CHOICES).get(
            account.comp_social_tier, account.comp_social_tier)
        comp_subscriptions.append({
            'id': f'comp-social-{account.id}',
            'product_name': label,
            'status': 'comped',
            'amount': 0,
            'interval': 'month',
            'kind': 'social_media',
            'is_comped': True,
            'comp_notes': account.comp_notes,
        })

    payment_methods = []
    default_pm_id = ''
    if account.stripe_customer_id:
        try:
            payment_methods = list_customer_payment_methods(
                account.stripe_customer_id)
            default_pm_id = get_customer_default_payment_method(
                account.stripe_customer_id)
            # Always show the default card first.
            payment_methods = sorted(
                payment_methods,
                key=lambda pm: getattr(pm, 'id', '') != default_pm_id)
        except Exception:
            logger.exception(
                'Payment method fetch failed for account %s', account.pk)

    # Maintenance upsell — show a pitch card on the subscriptions page
    # whenever the account has no active maintenance subscription. The
    # card itself does the "stronger pitch once project is live"
    # styling switch in template; the view passes the raw state.
    upsell_state = _maintenance_upsell_state(account)

    # Also surface whether a maintenance sub is set to cancel at period
    # end so we can render a Resume button.
    maint_sub_ids = set(
        account.maintenance_plans.exclude(stripe_subscription_id='')
        .values_list('stripe_subscription_id', flat=True))
    social_sub_ids = set(
        account.social_media_plans.exclude(stripe_subscription_id='')
        .values_list('stripe_subscription_id', flat=True))
    maintenance_cancel_pending = any(
        sub for sub in subscriptions
        if sub and sub.get('cancel_at_period_end')
        and sub.get('id') in maint_sub_ids
    )

    # Pending maintenance opt-in — the client selected a maintenance plan
    # (with the 10%-off-first-month promise) when booking, but it hasn't
    # started yet (it auto-charges when we push the site to Live). Show a
    # locked confirmation instead of the plan picker; changes go through us.
    pending_maintenance = None
    account = getattr(request, 'account', None)
    if account is not None:
        from decimal import ROUND_HALF_UP, Decimal

        from billing.pricing_models import ServiceTier
        for w in account.websites.all():
            slug = (w.opted_in_maintenance_tier or '').strip()
            if not slug:
                continue
            if w.maintenance_plans.filter(
                    status__in=('active', 'awaiting_payment')).exists():
                continue  # already started / billing — not pending anymore
            tier = ServiceTier.objects.filter(
                slug=slug, category='maintenance').first()
            if tier is None:
                continue
            full = Decimal(tier.price)
            discounted = (full * Decimal('0.90')).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP)
            pending_maintenance = {
                'website_name': w.name,
                'tier_name': tier.name,
                'full_price': full,
                'discounted_price': discounted,
                'interval': tier.billing_interval or 'month',
            }
            break

    # Top-3 maintenance tiers for the upsell card's mini-comparison.
    # Suppressed when a plan is already pending (they've chosen — no picker).
    upsell_tiers = []
    if upsell_state['show_upsell'] and pending_maintenance is None:
        upsell_tiers = list(_maintenance_tiers())

    ctx = _portal_context(
        request, 'subscriptions',
        subscriptions=subscriptions,
        comp_subscriptions=comp_subscriptions,
        payment_methods=payment_methods,
        default_pm_id=default_pm_id,
        stripe_publishable_key=getattr(
            _s, 'STRIPE_PUBLISHABLE_KEY', ''),
        upsell_state=upsell_state,
        upsell_tiers=upsell_tiers,
        maintenance_cancel_pending=maintenance_cancel_pending,
        maintenance_sub_ids=list(maint_sub_ids),
        social_sub_ids=list(social_sub_ids),
        pending_maintenance=pending_maintenance,
    )
    return render(request, 'clients/portal_subscriptions.html', ctx)


@client_required
@require_POST
def portal_payment_method_add(request):
    """
    Begin the add-card flow: create a SetupIntent for the customer +
    return its client_secret + a fresh page URL.

    HTMX call from the subscriptions page returns JSON; the page's JS
    hands the client_secret to Stripe Elements.
    """
    from django.http import JsonResponse

    from billing.stripe_helpers import (
        StripeNotConfigured, create_setup_intent_for_customer,
    )

    account = request.account
    if not account.stripe_customer_id:
        return JsonResponse(
            {'error': 'No Stripe customer on file. '
                      'Pay an invoice first to seed the customer.'},
            status=400)
    try:
        intent = create_setup_intent_for_customer(
            account.stripe_customer_id)
    except StripeNotConfigured as exc:
        return JsonResponse({'error': str(exc)}, status=500)
    except Exception as exc:  # noqa: BLE001
        logger.exception('SetupIntent create failed')
        return JsonResponse({'error': str(exc)[:200]}, status=500)

    return JsonResponse({
        'client_secret': intent.client_secret,
    })


@client_required
@require_POST
def portal_payment_method_remove(request, pm_id):
    """Remove (detach) a saved card."""
    from billing.stripe_helpers import (
        detach_payment_method, list_customer_payment_methods,
    )

    account = request.account
    # Sanity check — the PM must belong to this client's customer.
    methods = list_customer_payment_methods(account.stripe_customer_id)
    if not any(m['id'] == pm_id for m in methods):
        messages.error(request, 'That card is not on your account.')
        return redirect('clients:portal_subscriptions')
    try:
        detach_payment_method(pm_id)
        messages.success(request, 'Card removed.')
    except Exception as exc:  # noqa: BLE001
        logger.exception('Detach payment method failed')
        messages.error(request, f'Could not remove card: {exc}')
    return redirect('clients:portal_subscriptions')


@client_required
@require_POST
def portal_payment_method_default(request, pm_id):
    """Set the named card as the default for invoice payments."""
    from billing.stripe_helpers import (
        list_customer_payment_methods,
        set_customer_default_payment_method,
    )

    account = request.account
    methods = list_customer_payment_methods(account.stripe_customer_id)
    if not any(m['id'] == pm_id for m in methods):
        messages.error(request, 'That card is not on your account.')
        return redirect('clients:portal_subscriptions')
    try:
        set_customer_default_payment_method(
            account.stripe_customer_id, pm_id)
        messages.success(request, 'Default payment method updated.')
    except Exception as exc:  # noqa: BLE001
        logger.exception('Set-default payment method failed')
        messages.error(request, f'Could not update default: {exc}')
    return redirect('clients:portal_subscriptions')


# ── Maintenance upsell + signup ────────────────────────────────────────────

# Slugs the client portal explicitly knows about — keeps this view safe
# against arbitrary slug injection in the URL.
_MAINTENANCE_TIER_SLUGS = (
    'maintenance-essentials',
    'maintenance-growth',
    'maintenance-dominant',
)


def _maintenance_tiers():
    """Active maintenance tiers + features, sorted for display."""
    from billing.pricing_models import ServiceTier
    return (
        ServiceTier.objects
        .filter(category='maintenance', is_active=True)
        .order_by('sort_order', 'price')
        .prefetch_related('features')
    )


def _tier_change_direction(current_tier_slug, target_tier):
    """Compare the current tier to the target by price.

    Returns 'upgrade' | 'downgrade' | 'same' | '' (unknown / not a change).
    Used by the confirmation screens so the copy matches what will
    actually happen before the client commits.
    """
    if not current_tier_slug or target_tier is None:
        return ''
    from billing.pricing_models import ServiceTier
    current = ServiceTier.objects.filter(slug=current_tier_slug).first()
    if current is None or current.price is None or target_tier.price is None:
        return ''
    if target_tier.price > current.price:
        return 'upgrade'
    if target_tier.price < current.price:
        return 'downgrade'
    return 'same'


def _tier_change_message(tier, result, label):
    """Word a subscription tier-change confirmation by direction.

    `result` is the dict from change_*_subscription_tier:
    {'direction': 'upgrade'|'downgrade'|'same', 'effective_ts': ts|None}.
    `label` is 'maintenance' or 'social media'.
    """
    direction = (result or {}).get('direction', 'upgrade')
    if direction == 'same':
        return f'You\'re already on the {tier.name} {label} plan.'
    if direction == 'downgrade':
        ts = (result or {}).get('effective_ts')
        when = 'the end of your current billing period'
        if ts:
            import datetime
            dt = datetime.datetime.fromtimestamp(
                ts, tz=datetime.timezone.utc)
            when = dt.strftime('%B %d, %Y').replace(' 0', ' ')
        return (
            f'Your {label} plan will switch to {tier.name} on {when}. '
            f'You keep your current plan until then — no charge now, and '
            f'your next invoice will be at the new rate.')
    # upgrade
    return (
        f'Upgraded to the {tier.name} {label} plan. We\'ve charged the '
        f'prorated difference for the rest of this billing period to your '
        f'card on file; future invoices bill at the new rate.')


def _maintenance_upsell_state(account):
    """
    Return a small dict describing the upsell state for an account. Used
    by both the subscriptions page (to render the upsell card) and the
    /portal/maintenance/ page (to gate the "subscribe" CTA).

    Keys:
      show_upsell    — bool, render the pitch card on /portal/subscriptions/
      is_subscribed  — bool, account already has an active maintenance plan
      project_live   — bool, any website has reached the 'live' stage
      days_since_live — int or None
      current_tier_slug — '' or the active tier slug
    """
    websites = list(account.websites.all()) if account else []
    project_live = any(getattr(w, 'stage', '') == 'live' for w in websites)
    days_since_live = None
    launch_dates = [w.launch_date for w in websites
                    if getattr(w, 'launch_date', None)]
    if launch_dates:
        delta = timezone.now().date() - min(launch_dates)
        days_since_live = max(delta.days, 0)

    # A maintenance "tier" is earned via an active MaintenancePlan or an
    # operator-granted comp tier on the Account. Either suppresses the
    # upsell and shows "Your plan" on the matching tier card.
    active_plan = (account.maintenance_plans.filter(status='active').first()
                   if account else None)
    paid_slug = active_plan.tier_slug if active_plan else ''
    comp_slug = (account.comp_maintenance_package.replace('_', '-')
                 if account and account.comp_maintenance_package else '')

    current_tier_slug = paid_slug or comp_slug
    is_subscribed = bool(current_tier_slug)
    is_comped = bool(not paid_slug and comp_slug)

    return {
        'show_upsell': not is_subscribed,
        'is_subscribed': is_subscribed,
        'is_comped': is_comped,
        'project_live': project_live,
        'days_since_live': days_since_live,
        'current_tier_slug': current_tier_slug,
        'pending_tier_display': (
            active_plan.pending_tier_display if active_plan else ''),
        'pending_tier_effective': (
            active_plan.pending_tier_effective if active_plan else None),
    }


@client_required
def portal_maintenance(request):
    """
    Tier comparison + signup landing page. Shows all maintenance tiers
    with their feature bullets and a Subscribe/Switch button per tier.
    If the client already has maintenance, the matching tier shows as
    Current and the others offer Upgrade/Downgrade.
    """
    tiers = list(_maintenance_tiers())
    state = _maintenance_upsell_state(request.account)

    ctx = _portal_context(
        request, 'maintenance',
        tiers=tiers,
        upsell_state=state,
    )
    return render(request, 'clients/portal_maintenance.html', ctx)


@client_required
def portal_maintenance_start(request, slug):
    """
    GET  — confirmation screen for subscribing to a maintenance tier.
    POST — actually create the Stripe subscription using the customer's
           default payment method. No Stripe Elements step needed
           because the card is already on file.

    If the client has no default payment method, redirect them to the
    subscriptions page to add one (with a flash banner explaining why).

    If the client already has an active maintenance subscription on a
    DIFFERENT tier, route through `change_maintenance_subscription_tier`
    (proration + same subscription ID) instead of creating a new one.
    """
    from billing.stripe_helpers import (
        StripeNotConfigured,
        change_maintenance_subscription_tier,
        create_maintenance_subscription,
        get_customer_default_payment_method,
        get_maintenance_tier,
        list_customer_payment_methods,
    )

    if slug not in _MAINTENANCE_TIER_SLUGS:
        messages.error(request, 'Unknown maintenance plan.')
        return redirect('clients:portal_maintenance')

    # Billing is account-level: one customer, one card.
    profile = getattr(request, 'account', None)

    try:
        tier = get_maintenance_tier(slug)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('clients:portal_maintenance')

    # Resolve current state up front so both GET render + POST validation
    # use the same source of truth.
    state = _maintenance_upsell_state(request.account)
    is_change = state['is_subscribed']
    is_same_tier = is_change and state['current_tier_slug'] == slug

    # Pull the default card so the confirmation page can show "Charged to
    # Visa •••• 4242" without a second round trip on POST.
    default_card = None
    if request.account.stripe_customer_id:
        try:
            pm_id = get_customer_default_payment_method(
                request.account.stripe_customer_id)
            if pm_id:
                methods = list_customer_payment_methods(
                    request.account.stripe_customer_id)
                for m in methods:
                    if getattr(m, 'id', '') == pm_id:
                        default_card = {
                            'brand': getattr(
                                m.card, 'brand', '').upper(),
                            'last4': getattr(m.card, 'last4', ''),
                            'exp_month': getattr(m.card, 'exp_month', ''),
                            'exp_year': getattr(m.card, 'exp_year', ''),
                        }
                        break
        except Exception:
            logger.exception(
                'Default-card lookup failed for client %s', profile.pk)

    if request.method == 'POST':
        if is_same_tier:
            messages.info(
                request,
                f'You\'re already subscribed to the {tier.name} plan.')
            return redirect('clients:portal_maintenance')

        # Card required for both new subs and tier changes (Stripe may
        # need to charge proration immediately on an upgrade).
        if not default_card:
            messages.error(
                request,
                'Add a payment method first — your maintenance '
                'subscription needs a card on file to renew.')
            return redirect('clients:portal_subscriptions')

        try:
            if is_change:
                result = change_maintenance_subscription_tier(profile, slug)
                messages.success(
                    request, _tier_change_message(tier, result, 'maintenance'))
            else:
                create_maintenance_subscription(profile, slug)
                messages.success(
                    request,
                    f'You\'re subscribed to {tier.name}. Welcome aboard.')
        except StripeNotConfigured as exc:
            logger.exception('Stripe not configured for maintenance signup')
            messages.error(
                request,
                'Our payment processor is temporarily unavailable. '
                'Try again in a few minutes or email us.')
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('clients:portal_maintenance')
        except Exception as exc:  # noqa: BLE001
            logger.exception('Maintenance subscription failed')
            messages.error(
                request,
                f'We couldn\'t complete that subscription: {exc}')
            return redirect('clients:portal_maintenance')

        return redirect(
            f'{reverse("clients:portal_maintenance_success")}?tier={slug}')

    # GET — render the confirmation page.
    ctx = _portal_context(
        request, 'subscriptions',
        tier=tier,
        tier_features=list(tier.features.all().order_by('sort_order')),
        default_card=default_card,
        is_change=is_change,
        is_same_tier=is_same_tier,
        current_tier_slug=state['current_tier_slug'],
        change_direction=(
            _tier_change_direction(state['current_tier_slug'], tier)
            if is_change else ''),
    )
    return render(request, 'clients/portal_maintenance_confirm.html', ctx)


@client_required
def portal_maintenance_success(request):
    """Thank-you page shown after a successful maintenance signup."""
    slug = request.GET.get('tier', '') or ''
    tier = None
    if slug in _MAINTENANCE_TIER_SLUGS:
        from billing.pricing_models import ServiceTier
        tier = ServiceTier.objects.filter(slug=slug).first()
    ctx = _portal_context(
        request, 'subscriptions',
        tier=tier,
    )
    return render(request, 'clients/portal_maintenance_success.html', ctx)


@client_required
@require_POST
def portal_maintenance_cancel(request):
    """
    Cancel the client's maintenance subscription at period end. They
    keep service through the end of the cycle they've already paid for.
    """
    from billing.stripe_helpers import (
        StripeNotConfigured, cancel_maintenance_subscription,
    )

    # Billing is account-level: one customer, one card.
    profile = getattr(request, 'account', None)
    reason = (request.POST.get('reason') or '').strip()
    try:
        result = cancel_maintenance_subscription(profile, reason=reason)
        if result is None:
            messages.info(
                request, 'No active maintenance subscription to cancel.')
        else:
            messages.success(
                request,
                'Maintenance subscription set to cancel at the end of '
                'the current period. You can resume any time before '
                'then.')
    except StripeNotConfigured:
        messages.error(
            request,
            'Our payment processor is temporarily unavailable. '
            'Try again in a few minutes.')
    except Exception as exc:  # noqa: BLE001
        logger.exception('Maintenance cancel failed')
        messages.error(request, f'Could not cancel: {exc}')
    return redirect('clients:portal_subscriptions')


@client_required
@require_POST
def portal_maintenance_resume(request):
    """Undo a pending cancel-at-period-end."""
    from billing.stripe_helpers import (
        StripeNotConfigured, resume_maintenance_subscription,
    )

    # Billing is account-level: one customer, one card.
    profile = getattr(request, 'account', None)
    try:
        resume_maintenance_subscription(profile)
        messages.success(
            request, 'Maintenance subscription resumed. No change to '
            'your renewal date.')
    except StripeNotConfigured:
        messages.error(
            request,
            'Our payment processor is temporarily unavailable. Try '
            'again in a few minutes.')
    except Exception as exc:  # noqa: BLE001
        logger.exception('Maintenance resume failed')
        messages.error(request, f'Could not resume: {exc}')
    return redirect('clients:portal_subscriptions')


# ── Social media plans — comparison + signup (mirrors maintenance) ─────────

_SOCIAL_TIER_SLUGS = (
    'social-basic',
    'social-standard',
    'social-full',
)
_SOCIAL_PACKAGE_TO_TIER = {
    'social-basic':    'social-basic',
    'social-standard': 'social-standard',
    'social-full':     'social-full',
}


def _social_tiers():
    """Active social_media tiers, sorted for display."""
    from billing.pricing_models import ServiceTier
    return (
        ServiceTier.objects
        .filter(category='social_media', is_active=True)
        .order_by('sort_order', 'price')
        .prefetch_related('features')
    )


def _active_social_plan(account, website=None):
    """Return the active SocialMediaPlan for (account, website) — or None.
    Per-Website scope: one Account can hold N plans, one per business.
    When `website` is None we fall back to the account-wide row
    (website IS NULL)."""
    if account is None:
        return None
    return (
        account.social_media_plans
        .filter(status='active', website=website)
        .order_by('-started_at')
        .first()
    )


def _social_upsell_state(account, website=None):
    """Dict describing the upsell state for social plans on `website`.
    Parallel to _maintenance_upsell_state — every key is scoped to the
    single Website passed in, so an Account with three businesses gets
    three independent upsell states.

    Keys:
      show_upsell        — bool, render the pitch card
      is_subscribed      — bool, has an active SocialMediaPlan (paid OR comped)
      is_comped          — bool, active via comp not Stripe
      current_tier_slug  — '' or the active tier slug
    """
    active_plan = _active_social_plan(account, website=website)
    is_subscribed = active_plan is not None
    # Comp on the Account is account-wide — applies to every Website.
    # A paid plan wins over the comp for the "is_comped" flag.
    has_comp = bool(getattr(account, 'comp_social_tier', '') if account else '')
    has_paid = bool(active_plan and active_plan.stripe_subscription_id)
    is_comped = has_comp and not has_paid
    current_tier_slug = ''
    if active_plan is not None:
        current_tier_slug = active_plan.tier_slug
    elif has_comp:
        current_tier_slug = account.comp_social_tier
    return {
        'show_upsell': not (is_subscribed or has_comp),
        'is_subscribed': is_subscribed or has_comp,
        'is_comped': is_comped,
        'current_tier_slug': current_tier_slug,
        'pending_tier_display': (
            active_plan.pending_tier_display if active_plan else ''),
        'pending_tier_effective': (
            active_plan.pending_tier_effective if active_plan else None),
    }


@client_required
def portal_social_plans(request):
    """Tier comparison + signup landing page for social media plans.

    Per-Website: an Account with multiple businesses sees one block
    per Website. The chooser-picked Website (request.website) renders
    first; the others come below it. If the Account has zero Websites
    (rare — pre-build / migrated-in client), we render a single
    legacy account-wide block (website=None).
    """
    tiers = list(_social_tiers())

    account = getattr(request, 'account', None)
    websites = []
    if account is not None:
        websites = list(account.websites.all())

    # Build per-Website blocks. Each block carries its own upsell state
    # so the template can render Subscribe / Current / Switch correctly
    # per business.
    blocks = []
    if websites:
        # Put the chooser-picked Website first so it's the focus.
        picked = getattr(request, 'website', None)
        ordered = (
            [picked] + [w for w in websites if w.id != picked.id]
        ) if picked is not None else websites
        for w in ordered:
            blocks.append({
                'website': w,
                'upsell_state': _social_upsell_state(account, website=w),
            })
    else:
        blocks.append({
            'website': None,
            'upsell_state': _social_upsell_state(account, website=None),
        })

    ctx = _portal_context(
        request, 'social_plans',
        tiers=tiers,
        blocks=blocks,
        multi_business=len(blocks) > 1,
    )
    return render(request, 'clients/portal_social_plans.html', ctx)


@client_required
def portal_social_plans_start(request, slug):
    """GET — confirmation screen. POST — create / change the Stripe sub
    using the customer's default payment method on file.

    Scoped to whichever Website the user clicked Subscribe on:
    request.website is set by the @client_required decorator from
    either ?website=<slug> (set by the per-business Subscribe button),
    the chooser session, or the account's sole website."""
    from billing.stripe_helpers import (
        StripeNotConfigured,
        change_social_subscription_tier,
        create_social_subscription,
        get_customer_default_payment_method,
        get_social_tier,
        list_customer_payment_methods,
    )

    if slug not in _SOCIAL_TIER_SLUGS:
        messages.error(request, 'Unknown social media plan.')
        return redirect('clients:portal_social_plans')

    # Billing is account-level: one customer, one card.
    profile = getattr(request, 'account', None)
    website = getattr(request, 'website', None)
    # ?w=<slug> takes precedence over the chooser pick. The per-business
    # Subscribe buttons on /portal/social/plans/ pass it so a multi-
    # business account can target the right Website without first
    # switching the chooser.
    w_slug = (request.GET.get('w')
              or request.POST.get('w') or '').strip()
    if w_slug:
        account = getattr(request, 'account', None)
        if account is not None:
            override = account.websites.filter(slug=w_slug).first()
            if override is not None:
                website = override

    try:
        tier = get_social_tier(slug)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('clients:portal_social_plans')

    state = _social_upsell_state(request.account, website=website)
    # is_change must NOT be true for a purely-comped client — they have
    # no Stripe sub to mutate, just an upsell to a paid plan.
    existing_plan = _active_social_plan(request.account, website=website)
    is_change = bool(
        existing_plan and existing_plan.stripe_subscription_id)
    is_same_tier = is_change and state['current_tier_slug'] == slug

    default_card = None
    if request.account.stripe_customer_id:
        try:
            pm_id = get_customer_default_payment_method(
                request.account.stripe_customer_id)
            if pm_id:
                methods = list_customer_payment_methods(
                    request.account.stripe_customer_id)
                for m in methods:
                    if getattr(m, 'id', '') == pm_id:
                        default_card = {
                            'brand': getattr(
                                m.card, 'brand', '').upper(),
                            'last4': getattr(m.card, 'last4', ''),
                            'exp_month': getattr(m.card, 'exp_month', ''),
                            'exp_year': getattr(m.card, 'exp_year', ''),
                        }
                        break
        except Exception:
            logger.exception(
                'Default-card lookup failed for account %s',
                request.account.pk)

    if request.method == 'POST':
        if is_same_tier:
            messages.info(
                request,
                f'You\'re already subscribed to the {tier.name} plan.')
            return redirect('clients:portal_social_plans')

        if not default_card:
            messages.error(
                request,
                'Add a payment method first — your social media '
                'subscription needs a card on file to renew.')
            return redirect('clients:portal_subscriptions')

        try:
            if is_change:
                result = change_social_subscription_tier(
                    profile, slug, website=website)
                messages.success(
                    request,
                    _tier_change_message(tier, result, 'social media'))
            else:
                create_social_subscription(
                    profile, slug, website=website)
                messages.success(
                    request,
                    f'You\'re subscribed to {tier.name}. Welcome aboard.')
        except StripeNotConfigured:
            logger.exception('Stripe not configured for social signup')
            messages.error(
                request,
                'Our payment processor is temporarily unavailable. '
                'Try again in a few minutes or email us.')
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('clients:portal_social_plans')
        except Exception as exc:  # noqa: BLE001
            logger.exception('Social subscription failed')
            messages.error(
                request,
                f'We couldn\'t complete that subscription: {exc}')
            return redirect('clients:portal_social_plans')

        return redirect('clients:portal_social_plans')

    ctx = _portal_context(
        request, 'social_plans',
        tier=tier,
        tier_features=list(tier.features.all().order_by('sort_order')),
        default_card=default_card,
        is_change=is_change,
        is_same_tier=is_same_tier,
        current_tier_slug=state['current_tier_slug'],
        change_direction=(
            _tier_change_direction(state['current_tier_slug'], tier)
            if is_change else ''),
        target_website=website,
    )
    return render(
        request, 'clients/portal_social_plans_confirm.html', ctx)


@client_required
@require_POST
def portal_social_cancel(request):
    """Cancel social sub at period end — scoped to request.website."""
    from billing.stripe_helpers import (
        StripeNotConfigured, cancel_social_subscription,
    )
    # Billing is account-level: one customer, one card.
    profile = getattr(request, 'account', None)
    website = getattr(request, 'website', None)
    try:
        cancel_social_subscription(
            profile, reason=request.POST.get('reason', ''),
            website=website)
        messages.success(
            request,
            'Social media subscription will cancel at the end of your '
            'current billing period.')
    except StripeNotConfigured:
        messages.error(
            request,
            'Our payment processor is temporarily unavailable. Try '
            'again in a few minutes.')
    except Exception as exc:  # noqa: BLE001
        logger.exception('Social cancel failed')
        messages.error(request, f'Could not cancel: {exc}')
    return redirect('clients:portal_subscriptions')


@client_required
@require_POST
def portal_social_resume(request):
    """Undo a pending cancel-at-period-end — scoped to request.website."""
    from billing.stripe_helpers import (
        StripeNotConfigured, resume_social_subscription,
    )
    # Billing is account-level: one customer, one card.
    profile = getattr(request, 'account', None)
    website = getattr(request, 'website', None)
    try:
        resume_social_subscription(profile, website=website)
        messages.success(
            request,
            'Social media subscription resumed.')
    except StripeNotConfigured:
        messages.error(
            request,
            'Our payment processor is temporarily unavailable. Try '
            'again in a few minutes.')
    except Exception as exc:  # noqa: BLE001
        logger.exception('Social resume failed')
        messages.error(request, f'Could not resume: {exc}')
    return redirect('clients:portal_subscriptions')


# ── Phase 7 Part 2 — Client portal referral page ───────────────────────────

@client_required
def portal_referral(request):
    """Client-facing referral link + stats page."""
    from .models import ReferralLink, generate_referral_code

    account = request.account
    link = ReferralLink.objects.filter(account_new=account).first()
    if link is None:
        link = ReferralLink.objects.filter(
            account_new=account).first()
    if link is None:
        link = ReferralLink.objects.create(
            account_new=account,
            code=generate_referral_code(account.name))
    elif link.account_new_id is None:
        link.account_new = account
        link.save(update_fields=['account_new', 'updated_at'])

    return render(request, 'clients/portal_referral.html',
                  _portal_context(request, 'referral',
                                  link=link,
                                  referral_url=link.get_referral_url()))


# ── Onboarding setup page ───────────────────────────────────────────────────


def _onboarding_first_name(client):
    """First name for the setup page greeting; falls back to firm name."""
    raw = (client.contact_name or client.firm_name or '').strip()
    return raw.split(' ')[0] if raw else 'there'


def onboarding_setup(request, token):
    """
    Public account-setup landing page hit from the email setup-link.

    The UUID `token` authenticates the request — no Django login required
    coming in. On a valid POST we set the user's password, the client's
    4-digit portal PIN, mark the token used, log the user in, and redirect
    them to the intake form (the only portal page they can reach in the
    `pending_intake` state).

    Re-visits after the token is consumed show an "already set up" page
    with a Sign-In CTA — never an error.
    """
    from django.contrib.auth import login

    from .emails import send_account_setup_complete_email
    from .models import OnboardingToken

    onboarding_token = (
        OnboardingToken.objects
        .select_related('client', 'client__user')
        .filter(token=token)
        .first()
    )
    if onboarding_token is None:
        return render(
            request,
            'clients/onboarding_setup_invalid.html',
            {},
            status=404,
        )

    client = onboarding_token.client
    user = client.user

    if onboarding_token.used:
        return render(
            request,
            'clients/onboarding_setup_used.html',
            {'client': client},
        )

    if request.method == 'POST':
        password = (request.POST.get('password') or '').strip()
        password_confirm = (request.POST.get(
            'password_confirm') or '').strip()
        pin = ''.join(
            (request.POST.get(f'pin_{i}') or '').strip()
            for i in range(1, 5)
        )
        pin_confirm = ''.join(
            (request.POST.get(f'pin_confirm_{i}') or '').strip()
            for i in range(1, 5)
        )

        errors = []
        if len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        if not any(c.isdigit() for c in password):
            errors.append('Password must contain a number.')
        if password != password_confirm:
            errors.append('Passwords do not match.')
        if not (pin.isdigit() and len(pin) == 4):
            errors.append('PIN must be exactly 4 digits.')
        if pin != pin_confirm:
            errors.append('PINs do not match.')

        if errors:
            return render(
                request,
                'clients/onboarding_setup.html',
                {
                    'client': client,
                    'first_name': _onboarding_first_name(client),
                    'email': user.email,
                    'token': onboarding_token,
                    'errors': errors,
                },
            )

        # Activate + set password.
        user.set_password(password)
        user.is_active = True
        user.save()

        # Set the client portal PIN (same crypto path as the
        # in-portal setup flow — `vault.crypto.hash_client_pin`).
        salt = generate_salt()
        client.client_pin_salt = salt
        client.client_pin_hash = hash_client_pin(pin, salt)
        client.client_pin_set = True
        client.client_pin_failed_attempts = 0
        client.client_pin_lockout_until = None
        client.onboarding_status = 'pending_intake'
        client.save(update_fields=[
            'client_pin_salt', 'client_pin_hash', 'client_pin_set',
            'client_pin_failed_attempts', 'client_pin_lockout_until',
            'onboarding_status', 'updated_at',
        ])

        # Burn the token so the link can't be re-used.
        onboarding_token.used = True
        onboarding_token.used_at = timezone.now()
        onboarding_token.save(update_fields=[
            'used', 'used_at', 'updated_at'])

        # Log them in and send them to the intake form (the only
        # portal page they can reach in pending_intake).
        login(request, user,
              backend='django.contrib.auth.backends.ModelBackend')
        try:
            send_account_setup_complete_email(client)
        except Exception:
            logger.exception(
                'setup-complete email failed for %s', client.pk)
        messages.success(
            request,
            "Account set up! Please complete your intake form below.")
        return redirect('clients:intake')

    return render(
        request,
        'clients/onboarding_setup.html',
        {
            'client': client,
            'first_name': _onboarding_first_name(client),
            'email': user.email,
            'token': onboarding_token,
            'errors': [],
        },
    )


# ── Phase C — Website chooser ──────────────────────────────────────────────

def chooser(request):
    """
    Shown on every fresh login (per spec). Lists every Website the
    account owns plus account-wide quick links. An account with
    exactly one Website auto-redirects to its dashboard so a chooser
    with a single card never becomes a useless click.

    Account-only legacy clients (e.g. the auxiliary vault profile)
    see only the account-wide quick links — no Website cards.

    Uses the bare base template chrome — no portal sidebar so the
    page is visibly the "choose what to enter" gate, not a normal
    portal page.
    """
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.path)

    account = resolve_account_for_user(request.user)
    if account is None:
        # Authenticated but no Account row — staff / pre-backfill envs.
        if request.user.is_staff:
            return redirect('admin_dashboard:home')
        # No account — bounce to legacy dashboard so they're not stranded.
        return redirect('clients:dashboard')

    websites = list(account.websites.all().order_by('name'))

    # Single-website accounts: skip the chooser entirely. Lock the
    # session pick + go straight to the dashboard. Same behaviour as
    # if they'd clicked the lone card.
    if len(websites) == 1:
        request.session[SESSION_KEY_ACTIVE_WEBSITE] = websites[0].slug
        return redirect('clients:dashboard')

    return render(request, 'clients/chooser.html', {
        'account': account,
        'websites': websites,
        'meta_title': 'Choose a Site — Aspired Websites',
    })


@client_required
@require_POST
def portal_subscription_payment_method(request, sub_id):
    """
    Phase C4 — set the per-subscription Stripe default payment method.

    Stripe natively supports ``Subscription.default_payment_method``
    which takes precedence over the customer-level default. Posting
    an empty string clears it (revert to account default).

    Also mirrors the choice locally into
    ``SubscriptionPaymentMethod`` so the admin tooling can see which
    cards are pinned to which subs without round-tripping Stripe.
    """
    import stripe as _stripe
    from django.conf import settings as _s

    from billing.stripe_helpers import list_customer_payment_methods
    from clients.account_models import SubscriptionPaymentMethod

    account = request.account
    _stripe.api_key = _s.STRIPE_SECRET_KEY

    pm_id = (request.POST.get('payment_method_id') or '').strip()

    # Guard: the customer must own both the sub and the PM. Defensive
    # against a crafted POST trying to bind someone else's card.
    if not account.stripe_customer_id:
        messages.error(request, 'No billing account on file.')
        return redirect('clients:portal_subscriptions')

    try:
        sub = _stripe.Subscription.retrieve(sub_id)
        if getattr(sub, 'customer', '') != account.stripe_customer_id:
            messages.error(request, 'That subscription is not on your account.')
            return redirect('clients:portal_subscriptions')
    except Exception:
        logger.exception('Failed to retrieve sub %s for PM update', sub_id)
        messages.error(request, 'Subscription lookup failed. Try again.')
        return redirect('clients:portal_subscriptions')

    if pm_id:
        valid_ids = {p['id'] for p in list_customer_payment_methods(
            account.stripe_customer_id)}
        if pm_id not in valid_ids:
            messages.error(request, 'That card is not on your account.')
            return redirect('clients:portal_subscriptions')

    # Push the change to Stripe. Empty string → clear (use customer default).
    try:
        _stripe.Subscription.modify(
            sub_id,
            default_payment_method=pm_id or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('Stripe update failed for sub %s', sub_id)
        messages.error(request, f'Could not update card: {exc}')
        return redirect('clients:portal_subscriptions')

    # Mirror locally for the admin view + so we have a record without
    # a Stripe round-trip. The label/kind are best-effort; the source
    # of truth is always Stripe.
    if account is not None:
        kind_guess = 'other'
        # Match the sub against the website's known IDs so the local
        # row's `kind` lines up with what the admin reads.
        for ws in account.websites.all():
            if ws.stripe_hosting_subscription_id == sub_id:
                kind_guess = 'hosting'
                break
            if ws.stripe_maintenance_subscription_id == sub_id:
                kind_guess = 'maintenance'
                break
        SubscriptionPaymentMethod.objects.update_or_create(
            stripe_subscription_id=sub_id,
            defaults={
                'account': account,
                'payment_method_id': pm_id,
                'kind': kind_guess,
            },
        )

    if pm_id:
        messages.success(
            request, 'Saved. This subscription now uses the selected card.')
    else:
        messages.success(
            request,
            'Saved. This subscription now uses your default card.')
    return redirect('clients:portal_subscriptions')


@require_POST
def chooser_pick(request, slug):
    """
    Persist the website pick in the session, then bounce to the
    dashboard. POST-only so the action is CSRF-protected and never
    triggered by a stray GET / bot crawl.
    """
    if not request.user.is_authenticated:
        from django.contrib.auth.views import redirect_to_login
        return redirect_to_login(request.path)

    account = resolve_account_for_user(request.user)
    if account is None:
        return redirect('public:login')

    # Verify the slug belongs to this account before persisting — same
    # privacy guard as resolve_website().
    ws = account.websites.filter(slug=slug).first()
    if ws is None:
        messages.error(request, 'That website is no longer available.')
        return redirect('clients:chooser')

    request.session[SESSION_KEY_ACTIVE_WEBSITE] = ws.slug
    return redirect('clients:dashboard')
