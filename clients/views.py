"""Client portal views."""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

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

logger = logging.getLogger(__name__)

_STAGE_KEYS = [key for key, _ in PROJECT_STAGES]


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
    ctx = {
        'profile': profile,
        'project': project,
        'intake': intake,
        'active_portal_nav': active_nav,
        'intake_incomplete': bool(project) and (intake is None or not intake.completed),
        'pending_revisions': pending_revisions,
        'open_tickets': open_tickets,
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

    ctx = _portal_context(
        request, 'dashboard',
        stage_steps=stage_steps,
        activity=activity,
        next_invoice=next_invoice,
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

    ctx = _portal_context(
        request, 'project',
        timeline=timeline,
        revisions=revisions,
        support_window_left=support_window_left,
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


# ── Page 9: Credentials (vault — client-visible only) ───────────────────────

@client_required
def credentials(request):
    profile = request.client_profile
    from vault.models import ClientVault
    vault, _ = ClientVault.objects.get_or_create(client=profile)
    visible = list(vault.credentials.filter(visible_to_client=True))
    ctx = _portal_context(request, 'credentials', credentials=visible)
    return render(request, 'clients/credentials.html', ctx)


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
