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

from .decorators import client_required
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
    'Brand', 'Photos', 'Website Copy', 'References', 'Domain & Access',
    'Review & Submit',
]


def _intake_steps(intake):
    """Per-step completion flags for the 6-step intake form."""
    if intake is None:
        done = [False] * 6
    else:
        done = [
            bool(intake.brand_colors or intake.brand_fonts or intake.logo),
            bool(intake.photos_provided or intake.photos_note),
            bool(intake.about_copy or intake.practice_areas or intake.attorney_bios),
            bool(intake.reference_sites or intake.competitors),
            bool(intake.domain_name or intake.domain_registrar),
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

def _intake_unlocked(project):
    return bool(project) and project.payment_status in ('deposit_paid', 'fully_paid')


@client_required
def intake(request):
    profile = request.client_profile
    project = _active_project(profile)

    if not _intake_unlocked(project):
        ctx = _portal_context(request, 'intake', intake_locked=True)
        return render(request, 'clients/intake.html', ctx)

    intake_obj, _ = IntakeResponse.objects.get_or_create(project=project)

    if request.method == 'POST':
        # Final submission — fields are already auto-saved; just finalize.
        if not intake_obj.completed:
            intake_obj.completed = True
            intake_obj.completed_at = timezone.now()
            intake_obj.save(update_fields=['completed', 'completed_at', 'updated_at'])
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
def intake_save(request):
    """HTMX auto-save endpoint — persists the intake form on every change."""
    profile = request.client_profile
    project = _active_project(profile)
    if request.method != 'POST' or not _intake_unlocked(project):
        return redirect('clients:intake')

    intake_obj, _ = IntakeResponse.objects.get_or_create(project=project)
    form = IntakeForm(request.POST, request.FILES, instance=intake_obj)
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


def _notify_admin_intake_complete(profile):
    from django.conf import settings
    from django.core.mail import send_mail
    send_mail(
        subject=f'{profile.firm_name} completed intake',
        message=f'{profile.firm_name} has submitted their intake form.',
        from_email=settings.EMAIL_FROM_NO_REPLY,
        recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
        fail_silently=True,
    )


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
        rrweb_css = (static_root / 'rrweb-player.css').read_text(
            encoding='utf-8')
    except OSError:
        rrweb_css = ''
    try:
        rrweb_js = (static_root / 'rrweb-player.min.js').read_text(
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
        'rrweb_css': rrweb_css,
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

    from reporting.models import SessionRecording

    rec = get_object_or_404(
        SessionRecording, id=rec_id, client=request.client_profile)

    ctx = _portal_context(
        request, 'recordings',
        recording=rec,
        events_json=_json.dumps(rec.get_all_events(), default=str),
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
