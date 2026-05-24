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
    Project,
)
from .pdf_utils import render_contract_pdf
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

def _active_project(profile):
    return profile.projects.order_by('-created_at').first()


def _portal_context(request, active_nav, **extra):
    """Common context for every portal page — drives the sidebar + badges."""
    profile = request.client_profile
    project = _active_project(profile)
    intake = getattr(project, 'intake', None) if project else None
    pending_revisions = (
        project.revisions.filter(status='pending').count() if project else 0
    )
    open_tickets = profile.tickets.filter(
        status__in=['open', 'in_progress']
    ).count()
    # Green-dot badge on the Activity Log nav item — new entries in last 7 days.
    changelog_has_new = profile.changelog_entries.filter(
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
            .filter(client=profile, status='complete')
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
            client=profile, status='sent_to_client',
        ).exists()
    except Exception:
        # IntelligenceSuggestion table may not exist on a fresh
        # checkout — never break the chrome over a missing table.
        portal_suggestions_pending = False

    # Intake-only mode — true while the client is in `pending_intake`.
    # Drives the portal base template's nav: when set, only the Intake
    # Form link and Sign Out are rendered. Prevents the confusing UX
    # of links that immediately redirect back to /portal/intake/.
    intake_only = (
        getattr(profile, 'onboarding_status', '') == 'pending_intake')

    ctx = {
        'profile': profile,
        'project': project,
        'intake': intake,
        'active_portal_nav': active_nav,
        'intake_incomplete': bool(project) and (intake is None or not intake.completed),
        'pending_revisions': pending_revisions,
        'open_tickets': open_tickets,
        'changelog_has_new': changelog_has_new,
        'security_has_open': security_has_open,
        'portal_suggestions_pending': portal_suggestions_pending,
        # Tier 2 — only show the Recordings nav link when the addon
        # is active for this client.
        'session_recording_nav_visible': bool(
            profile.session_recording_enabled),
        'intake_only': intake_only,
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
    profile = request.client_profile
    project = _active_project(profile)

    next_invoice = None
    stage_steps = []
    activity = []
    if project:
        stage_steps = _stage_steps(project)
        activity = list(project.stage_logs.all()[:5])
        contract = profile.contracts.order_by('-created_at').first()
        if contract:
            if project.payment_status == 'awaiting_deposit':
                next_invoice = {'label': 'Deposit (50%)', 'amount': contract.deposit_amount}
            elif project.payment_status == 'deposit_paid':
                next_invoice = {'label': 'Final payment', 'amount': contract.final_amount}

    from reporting.uptime_helpers import (
        get_avg_response_time, get_uptime_percentage,
    )
    ctx = _portal_context(
        request, 'dashboard',
        stage_steps=stage_steps,
        activity=activity,
        next_invoice=next_invoice,
        uptime_30=get_uptime_percentage(profile, 30),
        uptime_avg_response=get_avg_response_time(profile, 30),
    )
    return render(request, 'clients/dashboard.html', ctx)


# ── Page 2: My Project ──────────────────────────────────────────────────────

@client_required
def project_detail(request):
    profile = request.client_profile
    project = _active_project(profile)

    timeline = []
    revisions = []
    support_window_left = None
    if project:
        timeline = _project_timeline(project)
        revisions = list(project.revisions.all())
        if project.stage == 'live' and project.support_window_ends:
            delta = (project.support_window_ends - timezone.localdate()).days
            support_window_left = delta

    from reporting.uptime_helpers import (
        get_current_status, get_uptime_chart_data, get_uptime_percentage,
    )
    uptime_chart = get_uptime_chart_data(profile, 30)
    peak_ms = max(
        (d['avg_response_ms'] or 0 for d in uptime_chart), default=0) or 1
    for day in uptime_chart:
        day['bar_h'] = round((day['avg_response_ms'] or 0) / peak_ms * 100)

    ctx = _portal_context(
        request, 'project',
        timeline=timeline,
        revisions=revisions,
        support_window_left=support_window_left,
        uptime_status=get_current_status(profile),
        uptime_30=get_uptime_percentage(profile, 30),
        uptime_90=get_uptime_percentage(profile, 90),
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
    Lazily create the Project + IntakeResponse + ClientVault for a
    new-flow client who has reached the intake page without a real
    Stripe webhook having fired (test/demo path, or a webhook that
    silently failed and was never retried).

    Idempotent — returns the existing Project if one already exists.
    """
    project = _active_project(client)
    if project is not None:
        # Ensure the row that holds intake answers exists.
        IntakeResponse.objects.get_or_create(project=project)
        return project

    package = (
        client.package
        if client.package in ('essential_build', 'premium_build')
        else '')
    project = Project.objects.create(
        client=client,
        stage='intake',
        package=package,
        payment_status='fully_paid',
        final_paid_at=timezone.now(),
    )
    IntakeResponse.objects.get_or_create(project=project)
    try:
        from vault.models import ClientVault
        ClientVault.objects.get_or_create(client=client)
    except Exception:
        # Vault models import path may be unavailable in tests — never
        # break intake over the vault row not materialising.
        logger.exception(
            'Auto-create of ClientVault failed for %s', client.pk)
    return project


@client_required
@allow_pending_intake
def intake(request):
    profile = request.client_profile
    project = _active_project(profile)

    if not _intake_unlocked(profile, project):
        ctx = _portal_context(request, 'intake', intake_locked=True)
        return render(request, 'clients/intake.html', ctx)

    # Unlocked but no Project yet — materialise it now. Covers the
    # new-flow test path where no real Stripe webhook ever fired (so
    # the webhook-side _on_onboarding_invoice_paid hook never ran).
    if project is None:
        project = _ensure_project_for_unlocked_intake(profile)

    intake_obj, _ = IntakeResponse.objects.get_or_create(project=project)

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
    profile = request.client_profile
    project = _active_project(profile)
    if request.method != 'POST' or not _intake_unlocked(profile, project):
        return redirect('clients:intake')

    # Belt-and-suspenders: intake() materialises the Project on first
    # GET, but if auto-save somehow fires first (HTMX kicks in on field
    # change), do the same lazy create here.
    if project is None:
        project = _ensure_project_for_unlocked_intake(profile)

    intake_obj, _ = IntakeResponse.objects.get_or_create(project=project)

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
    return render(request, 'clients/_intake_progress.html', {
        'intake_steps': steps,
        'intake_completed_count': completed,
        'intake_percent': percent,
        'saved_at': timezone.now(),
    })


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
    profile = request.client_profile
    project = _active_project(profile)
    if not _intake_unlocked(profile, project):
        return HttpResponse(status=403)
    if project is None:
        project = _ensure_project_for_unlocked_intake(profile)
    intake_obj, _ = IntakeResponse.objects.get_or_create(project=project)

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
    profile = request.client_profile
    project = _active_project(profile)
    if project is None or not _intake_unlocked(profile, project):
        return HttpResponse(status=403)
    intake_obj, _ = IntakeResponse.objects.get_or_create(project=project)

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
                client=profile, label=label).exists():
            return
        try:
            file_field.open('rb')
            ClientDocument.objects.create(
                client=profile,
                project=project,
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
            client=profile,
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

    # Confirmation email.
    try:
        send_intake_received_email(profile)
    except Exception:
        logger.exception(
            'intake-received email failed for %s', profile.pk)


# ── Page 4: Files ───────────────────────────────────────────────────────────

@client_required
def files(request):
    profile = request.client_profile
    docs = profile.documents.all()
    ctx = _portal_context(
        request, 'files',
        docs_to_client=[d for d in docs if d.direction == 'to_client'],
        docs_from_client=[d for d in docs if d.direction == 'from_client'],
        upload_form=FileUploadForm(),
    )
    return render(request, 'clients/files.html', ctx)


@client_required
def file_upload(request):
    profile = request.client_profile
    if request.method == 'POST':
        form = FileUploadForm(request.POST, request.FILES)
        if form.is_valid():
            doc = form.save(commit=False)
            doc.client = profile
            doc.project = _active_project(profile)
            doc.direction = 'from_client'
            doc.uploaded_by = request.user
            doc.save()
            messages.success(request, 'File uploaded.')
            return redirect('clients:files')
        ctx = _portal_context(request, 'files', upload_form=form,
                              docs_to_client=[], docs_from_client=[])
        docs = profile.documents.all()
        ctx['docs_to_client'] = [d for d in docs if d.direction == 'to_client']
        ctx['docs_from_client'] = [d for d in docs if d.direction == 'from_client']
        return render(request, 'clients/files.html', ctx)
    return redirect('clients:files')


# ── Page 5: Revisions ───────────────────────────────────────────────────────

def _hourly_rate():
    """The out-of-scope hourly rate, from billing AddonPricing (DB-driven)."""
    from billing.pricing_models import AddonPricing
    addon = AddonPricing.objects.filter(slug='addon-hourly').first()
    return f'${addon.price_min:,.0f}' if addon else '$85'


@client_required
def revisions(request):
    profile = request.client_profile
    project = _active_project(profile)
    revision_list = list(project.revisions.all()) if project else []
    ctx = _portal_context(
        request, 'revisions',
        revision_list=revision_list,
        form=RevisionForm(),
        hourly_rate=_hourly_rate(),
    )
    return render(request, 'clients/revisions.html', ctx)


@client_required
def revision_new(request):
    profile = request.client_profile
    project = _active_project(profile)
    if project is None:
        messages.error(request, 'You need an active project to request a revision.')
        return redirect('clients:revisions')

    if request.method == 'POST':
        form = RevisionForm(request.POST)
        if form.is_valid():
            revision = form.save(commit=False)
            revision.project = project
            revision.source = 'aspired_portal'
            revision.counts_against_limit = revision.is_major
            revision.save()

            if revision.is_major:
                project.revision_count += 1
                project.save(update_fields=['revision_count', 'updated_at'])

            if project.revision_count > project.revision_limit:
                # Out of scope — bill it before work begins.
                revision.status = 'out_of_scope'
                revision.save(update_fields=['status', 'updated_at'])
                _create_revision_mini_invoice(profile, project, revision)
                messages.warning(
                    request,
                    'This exceeds your included revisions. An out-of-scope '
                    'invoice will be sent before work begins.',
                )
            else:
                messages.success(request, 'Revision request submitted.')

            _notify_admin_revision(profile, revision)
            return redirect('clients:revisions')

        ctx = _portal_context(
            request, 'revisions', form=form,
            revision_list=list(project.revisions.all()),
            hourly_rate=_hourly_rate(),
        )
        return render(request, 'clients/revisions.html', ctx)
    return redirect('clients:revisions')


def _create_revision_mini_invoice(profile, project, revision):
    from billing.models import MiniInvoice
    MiniInvoice.objects.create(
        client=profile,
        project=project,
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
    profile = request.client_profile
    ctx = _portal_context(
        request, 'support',
        tickets=list(profile.tickets.all()),
        form=SupportTicketForm(),
    )
    return render(request, 'clients/support.html', ctx)


@client_required
def support_new(request):
    profile = request.client_profile
    if request.method == 'POST':
        form = SupportTicketForm(request.POST)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.client = profile
            ticket.project = _active_project(profile)
            ticket.save()
            _notify_admin_ticket(profile, ticket)
            messages.success(request, 'Support ticket submitted.')
            return redirect('clients:support')
        ctx = _portal_context(request, 'support', form=form,
                              tickets=list(profile.tickets.all()))
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
    profile = request.client_profile
    invoice_list = []
    stripe_error = None
    if profile.stripe_customer_id:
        try:
            from django.conf import settings
            import stripe
            if not settings.STRIPE_SECRET_KEY:
                raise RuntimeError('Stripe not configured')
            stripe.api_key = settings.STRIPE_SECRET_KEY
            result = stripe.Invoice.list(customer=profile.stripe_customer_id, limit=24)
            for inv in result.get('data', []):
                invoice_list.append({
                    'description': (inv.get('description')
                                    or f'Invoice {inv.get("number") or inv.get("id")}'),
                    'amount': (inv.get('amount_due') or 0) / 100,
                    'status': inv.get('status'),
                    'created': inv.get('created'),
                    'pdf_url': inv.get('hosted_invoice_url') or inv.get('invoice_pdf'),
                    'is_open': inv.get('status') == 'open',
                })
        except Exception:
            logger.exception('Could not load Stripe invoices for %s', profile.pk)
            stripe_error = 'Invoices are temporarily unavailable. Please try again later.'
    ctx = _portal_context(
        request, 'invoices',
        invoice_list=invoice_list,
        stripe_error=stripe_error,
    )
    return render(request, 'clients/invoices.html', ctx)


# ── Page 9: Credentials (PIN-gated client vault) ────────────────────────────

def _client_visible_credentials(profile):
    """The credentials staff have shared with this client, ordered for display."""
    from vault.models import ClientVault
    vault = ClientVault.objects.filter(client=profile).first()
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


def _lock_client_pin(profile, now):
    """Begin a lockout window after too many failed attempts."""
    profile.client_pin_lockout_until = now + timedelta(
        minutes=CLIENT_PIN_LOCKOUT_MINUTES)
    profile.client_pin_failed_attempts = 0
    profile.save(update_fields=[
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
    profile = request.client_profile
    now = timezone.now()

    # ── First-time PIN setup ──
    if not profile.client_pin_set:
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
            profile.client_pin_salt = salt
            profile.client_pin_hash = hash_client_pin(pin, salt)
            profile.client_pin_set = True
            profile.client_pin_failed_attempts = 0
            profile.client_pin_lockout_until = None
            profile.save(update_fields=[
                'client_pin_salt', 'client_pin_hash', 'client_pin_set',
                'client_pin_failed_attempts', 'client_pin_lockout_until',
                'updated_at',
            ])
            mark_client_vault_unlocked(request)
            return redirect('clients:credentials')
        ctx = _portal_context(request, 'credentials')
        return render(request, 'clients/vault_setup_pin.html', ctx)

    # ── Locked out? ──
    if profile.client_pin_lockout_until and profile.client_pin_lockout_until > now:
        ctx = _portal_context(
            request, 'credentials',
            lockout_until=profile.client_pin_lockout_until.isoformat(),
        )
        return render(request, 'clients/vault_locked.html', ctx)

    # ── PIN entry ──
    if request.method == 'POST':
        pin = _collect_pin(request)
        salt = bytes(profile.client_pin_salt or b'')
        if verify_client_pin(pin, profile.client_pin_hash, salt):
            profile.client_pin_failed_attempts = 0
            profile.client_pin_lockout_until = None
            profile.save(update_fields=[
                'client_pin_failed_attempts', 'client_pin_lockout_until',
                'updated_at',
            ])
            mark_client_vault_unlocked(request)
            return redirect('clients:credentials')

        # Wrong PIN.
        profile.client_pin_failed_attempts += 1
        if profile.client_pin_failed_attempts >= CLIENT_PIN_MAX_ATTEMPTS:
            _lock_client_pin(profile, now)
            ctx = _portal_context(
                request, 'credentials',
                lockout_until=profile.client_pin_lockout_until.isoformat(),
            )
            return render(request, 'clients/vault_locked.html', ctx)
        profile.save(update_fields=['client_pin_failed_attempts', 'updated_at'])
        remaining = CLIENT_PIN_MAX_ATTEMPTS - profile.client_pin_failed_attempts
        ctx = _portal_context(
            request, 'credentials',
            pin_error=(f'Incorrect PIN — {remaining} attempt'
                       f'{"" if remaining == 1 else "s"} remaining before a '
                       f'{CLIENT_PIN_LOCKOUT_MINUTES}-minute lockout.'),
        )
        return render(request, 'clients/vault_enter_pin.html', ctx)

    # ── Unlocked — show the credentials ──
    if is_client_vault_unlocked(request):
        ctx = _portal_context(
            request, 'credentials',
            credentials=_client_visible_credentials(profile),
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
    profile = request.client_profile
    now = timezone.now()
    credentials_url = reverse('clients:credentials')

    def _redirect():
        resp = HttpResponse(status=204)
        resp['HX-Redirect'] = credentials_url
        return resp

    if not profile.client_pin_set:
        return _redirect()
    if profile.client_pin_lockout_until and profile.client_pin_lockout_until > now:
        return _redirect()

    pin = _collect_pin(request)
    salt = bytes(profile.client_pin_salt or b'')
    if verify_client_pin(pin, profile.client_pin_hash, salt):
        profile.client_pin_failed_attempts = 0
        profile.client_pin_lockout_until = None
        profile.save(update_fields=[
            'client_pin_failed_attempts', 'client_pin_lockout_until',
            'updated_at',
        ])
        mark_client_vault_unlocked(request)
        resp = HttpResponse(status=204)
        resp['HX-Trigger'] = 'vaultReauthed'
        return resp

    # Wrong PIN.
    profile.client_pin_failed_attempts += 1
    if profile.client_pin_failed_attempts >= CLIENT_PIN_MAX_ATTEMPTS:
        _lock_client_pin(profile, now)
        return _redirect()
    profile.save(update_fields=['client_pin_failed_attempts', 'updated_at'])
    remaining = CLIENT_PIN_MAX_ATTEMPTS - profile.client_pin_failed_attempts
    return render(request, 'clients/_vault_reauth_error.html', {
        'pin_error': (f'Incorrect PIN — {remaining} attempt'
                      f'{"" if remaining == 1 else "s"} left.'),
    })


# ── Page 10: Activity Log (client site changelog) ───────────────────────────

@client_required
def portal_changelog(request):
    """The client-facing site changelog — grouped by month, month-filterable."""
    profile = request.client_profile
    visible = profile.changelog_entries.filter(is_client_visible=True)

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
    profile = request.client_profile
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

    rows = build_keyword_rows(profile, active_only=True)
    overview = overview_stats(profile)

    ctx = _portal_context(
        request, 'seo',
        keyword_rows=rows,
        keyword_insight=keyword_insight(rows),
        conversion_counts=conversion_counts(profile),
        conversion_chart=conversion_6month_chart(profile),
        analytics_overview=overview,
        analytics_top_pages=top_pages(profile, limit=5),
        scroll_insight=scroll_insight(overview['avg_scroll_depth']),
        exit_intent_insight=exit_intent_insight(
            overview['exit_intent_rate']),
        session_recording_enabled=bool(
            profile.session_recording_enabled),
        on_essentials=(profile.package == 'maintenance_essentials'),
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
    profile = request.client_profile
    from reporting.models import MonthlyReport
    from .models import AnnualReport
    reports = list(MonthlyReport.objects.filter(
        client=profile, status='sent'))
    annual_reports = list(AnnualReport.objects.filter(
        client=profile, status__in=['ready', 'sent']
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

    profile = request.client_profile
    enabled = bool(profile.session_recording_enabled)

    recordings = SessionRecording.objects.filter(client=profile)
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
        on_essentials=(profile.package == 'maintenance_essentials'),
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
        SessionRecording, id=rec_id, client=request.client_profile)

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
        'client': request.client_profile,
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
        SessionRecording, id=rec_id, client=request.client_profile)

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
        client=request.client_profile,
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

    profile = request.client_profile
    scans = list(
        VulnerabilityScan.objects
        .filter(client=profile, status='complete')
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
        id=scan_id, client=request.client_profile, status='complete',
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
        MonthlyReport, id=report_id, client=request.client_profile)
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
    profile = request.client_profile
    if request.method == 'POST':
        form = SettingsForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, 'Settings saved.')
            return redirect('clients:settings')
    else:
        form = SettingsForm(instance=profile)
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
            contract.signed = True
            contract.signed_at = timezone.now()
            contract.signed_ip = request.META.get('REMOTE_ADDR')
            contract.signed_name = signed_name
            contract.pdf_path = render_contract_pdf(contract)
            contract.save()

            # Create the Project for this build — awaiting the deposit payment.
            Project.objects.create(
                client=contract.client,
                package=contract.package,
                stage='intake',
                payment_status='awaiting_deposit',
            )

            send_contract_signed_email(contract)
            # Issue the 50% deposit invoice via Stripe (best effort —
            # logs and skips if Stripe is not configured).
            from billing.stripe_helpers import issue_deposit_invoice
            issue_deposit_invoice(contract)

            return redirect('clients:contract_signed')

    return render(request, 'clients/contract_sign.html', {
        'contract': contract,
        'error': error,
    })


def contract_signed(request):
    """Post-signing thank-you page."""
    return render(request, 'clients/contract_signed.html', {})


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
            subject=(f'[Intelligence] {suggestion.client.firm_name} '
                     f'{verb}: {suggestion.title[:60]}'),
            message=(
                f'{suggestion.client.firm_name} {verb.lower()} the '
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


def intelligence_approve(request, token):
    """Public magic-link landing — records approval, renders thanks."""
    from .models import IntelligenceSuggestion

    try:
        s = IntelligenceSuggestion.objects.get(response_token=token)
    except (IntelligenceSuggestion.DoesNotExist, ValueError):
        return render(request, 'clients/intel_response.html',
                      {'state': 'not_found'})

    already_responded = s.client_responded_at is not None
    if not already_responded:
        _intel_record_response(s, 'approve')

    return render(request, 'clients/intel_response.html', {
        'state': 'approved',
        'suggestion': s,
        'already_responded': already_responded,
    })


def intelligence_decline(request, token):
    """Public magic-link landing — records decline, renders thanks."""
    from .models import IntelligenceSuggestion

    try:
        s = IntelligenceSuggestion.objects.get(response_token=token)
    except (IntelligenceSuggestion.DoesNotExist, ValueError):
        return render(request, 'clients/intel_response.html',
                      {'state': 'not_found'})

    already_responded = s.client_responded_at is not None
    if not already_responded:
        _intel_record_response(s, 'decline')

    return render(request, 'clients/intel_response.html', {
        'state': 'declined',
        'suggestion': s,
        'already_responded': already_responded,
    })


# ── Phase 7 Part 3 — Client portal suggestions list ────────────────────────

@client_required
def portal_suggestions(request):
    """Portal page that mirrors what the client received via email."""
    from .models import IntelligenceSuggestion

    profile = request.client_profile
    suggestions = (
        IntelligenceSuggestion.objects
        .filter(client=profile, status__in=_PORTAL_INTEL_STATUSES)
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

    amount = (getattr(price, 'unit_amount', 0) or 0) / 100 if price else 0
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
        'interval': interval,
        'product_name': product_name,
        'cancel_at_period_end': getattr(
            stripe_sub, 'cancel_at_period_end', False),
        'current_period_end': getattr(
            stripe_sub, 'current_period_end', None),
        'trial_end': getattr(stripe_sub, 'trial_end', None),
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

    from billing.stripe_helpers import (
        get_customer_default_payment_method,
        list_customer_payment_methods,
    )

    profile = request.client_profile
    _stripe.api_key = _s.STRIPE_SECRET_KEY

    subscriptions = []
    if profile.stripe_customer_id:
        # Fetch each known subscription by ID so we get current Stripe
        # state (vs trusting the local boolean flags).
        for sub_id in [
            profile.stripe_hosting_subscription_id,
            profile.stripe_subscription_id,
        ]:
            if not sub_id:
                continue
            try:
                sub = _stripe.Subscription.retrieve(sub_id)
                if getattr(sub, 'status', '') in (
                        'active', 'trialing', 'past_due', 'unpaid'):
                    subscriptions.append(_subscription_card(sub))
            except Exception:
                logger.exception(
                    'Subscription fetch failed for %s', sub_id)

    payment_methods = []
    default_pm_id = ''
    if profile.stripe_customer_id:
        try:
            payment_methods = list_customer_payment_methods(
                profile.stripe_customer_id)
            default_pm_id = get_customer_default_payment_method(
                profile.stripe_customer_id)
        except Exception:
            logger.exception(
                'Payment method fetch failed for client %s', profile.pk)

    # Maintenance upsell — show a pitch card on the subscriptions page
    # whenever the client has no active maintenance subscription. The
    # card itself does the "stronger pitch once project is live"
    # styling switch in template; the view passes the raw state.
    upsell_state = _maintenance_upsell_state(profile)

    # Also surface whether the maintenance sub is set to cancel at
    # period end so we can render a Resume button.
    maintenance_cancel_pending = any(
        sub for sub in subscriptions
        if sub and sub.get('cancel_at_period_end')
        and sub.get('id') == profile.stripe_subscription_id
    )

    # Top-3 maintenance tiers for the upsell card's mini-comparison.
    upsell_tiers = []
    if upsell_state['show_upsell']:
        upsell_tiers = list(_maintenance_tiers())

    ctx = _portal_context(
        request, 'subscriptions',
        subscriptions=subscriptions,
        payment_methods=payment_methods,
        default_pm_id=default_pm_id,
        stripe_publishable_key=getattr(
            _s, 'STRIPE_PUBLISHABLE_KEY', ''),
        upsell_state=upsell_state,
        upsell_tiers=upsell_tiers,
        maintenance_cancel_pending=maintenance_cancel_pending,
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

    profile = request.client_profile
    if not profile.stripe_customer_id:
        return JsonResponse(
            {'error': 'No Stripe customer on file. '
                      'Pay an invoice first to seed the customer.'},
            status=400)
    try:
        intent = create_setup_intent_for_customer(
            profile.stripe_customer_id)
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

    profile = request.client_profile
    # Sanity check — the PM must belong to this client's customer.
    methods = list_customer_payment_methods(profile.stripe_customer_id)
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

    profile = request.client_profile
    methods = list_customer_payment_methods(profile.stripe_customer_id)
    if not any(m['id'] == pm_id for m in methods):
        messages.error(request, 'That card is not on your account.')
        return redirect('clients:portal_subscriptions')
    try:
        set_customer_default_payment_method(
            profile.stripe_customer_id, pm_id)
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


def _maintenance_upsell_state(profile):
    """
    Return a small dict describing the upsell state for a client. Used
    by both the subscriptions page (to render the upsell card) and the
    /portal/maintenance/ page (to gate the "subscribe" CTA).

    Keys:
      show_upsell    — bool, render the pitch card on /portal/subscriptions/
      is_subscribed  — bool, client already has an active maintenance sub
      project_live   — bool, project has reached the 'live' stage
      days_since_live — int or None
      current_tier_slug — '' or the active tier slug
    """
    project = _active_project(profile)
    project_live = bool(project and project.stage == 'live')
    days_since_live = None
    if project and getattr(project, 'launch_date', None):
        delta = timezone.now().date() - project.launch_date
        days_since_live = max(delta.days, 0)

    current_tier_slug = ''
    if profile.maintenance_active and profile.package:
        # Convert local package -> ServiceTier slug
        current_tier_slug = profile.package.replace('_', '-')

    return {
        'show_upsell': not profile.maintenance_active,
        'is_subscribed': profile.maintenance_active,
        'project_live': project_live,
        'days_since_live': days_since_live,
        'current_tier_slug': current_tier_slug,
    }


@client_required
def portal_maintenance(request):
    """
    Tier comparison + signup landing page. Shows all maintenance tiers
    with their feature bullets and a Subscribe/Switch button per tier.
    If the client already has maintenance, the matching tier shows as
    Current and the others offer Upgrade/Downgrade.
    """
    profile = request.client_profile
    tiers = list(_maintenance_tiers())
    state = _maintenance_upsell_state(profile)

    ctx = _portal_context(
        request, 'subscriptions',
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

    profile = request.client_profile

    try:
        tier = get_maintenance_tier(slug)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect('clients:portal_maintenance')

    # Resolve current state up front so both GET render + POST validation
    # use the same source of truth.
    state = _maintenance_upsell_state(profile)
    is_change = state['is_subscribed']
    is_same_tier = is_change and state['current_tier_slug'] == slug

    # Pull the default card so the confirmation page can show "Charged to
    # Visa •••• 4242" without a second round trip on POST.
    default_card = None
    if profile.stripe_customer_id:
        try:
            pm_id = get_customer_default_payment_method(
                profile.stripe_customer_id)
            if pm_id:
                methods = list_customer_payment_methods(
                    profile.stripe_customer_id)
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
                change_maintenance_subscription_tier(profile, slug)
                messages.success(
                    request,
                    f'Switched to the {tier.name} maintenance plan. '
                    f'Stripe will prorate the change on your next invoice.')
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
    )
    return render(request, 'clients/portal_maintenance_confirm.html', ctx)


@client_required
def portal_maintenance_success(request):
    """Thank-you page shown after a successful maintenance signup."""
    profile = request.client_profile
    slug = request.GET.get('tier', '') or ''
    tier = None
    if slug in _MAINTENANCE_TIER_SLUGS:
        from billing.pricing_models import ServiceTier
        tier = ServiceTier.objects.filter(slug=slug).first()
    ctx = _portal_context(
        request, 'subscriptions',
        tier=tier,
        profile=profile,
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

    profile = request.client_profile
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

    profile = request.client_profile
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


# ── Phase 7 Part 2 — Client portal referral page ───────────────────────────

@client_required
def portal_referral(request):
    """Client-facing referral link + stats page."""
    from .models import ReferralLink, generate_referral_code

    profile = request.client_profile
    link, _ = ReferralLink.objects.get_or_create(
        client=profile,
        defaults={'code': generate_referral_code(profile.firm_name)},
    )

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
