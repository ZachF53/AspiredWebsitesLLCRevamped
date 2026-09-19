"""v2 Websites — list + tabbed detail. The core page of the v2 build."""

from datetime import timedelta

from django.contrib import messages
from django.core.management import call_command
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from admin_dashboard.decorators import admin_required
from clients.account_models import Website
from clients.services import GuardError

INTAKE_REMINDER_COOLDOWN = timedelta(hours=48)
SETUP_EMAIL_COOLDOWN = timedelta(hours=24)


def _setter_name(request):
    return (request.user.get_full_name()
            or request.user.username or 'admin')


def _has_live_subscription(website):
    """True if this Website carries a live Stripe subscription id.

    Hard constraint from the build brief: no write path in this build may
    touch a Website row where stripe_maintenance_subscription_id or
    stripe_hosting_subscription_id is non-null (Burgland Tech, Denis Law
    Group — the two real paying clients). Every view in this module that
    writes a field on the Website row itself checks this first.
    """
    return bool(website.stripe_hosting_subscription_id
                or website.stripe_maintenance_subscription_id)


def _block_if_live_subscription(request, website):
    if _has_live_subscription(website):
        messages.error(
            request,
            'This website has a live Stripe subscription — v2 write '
            'actions are disabled for it in this build. Use v1 if a '
            'change is genuinely needed.')
        return True
    return False


#: (form field name, model field name) for the three lifecycle selects a
#: no-build client needs set at creation. Kept as one list so the view,
#: the choice-validation loop, and the template iterate the same set —
#: adding a fourth select later means adding it here once, not in three
#: places that can drift.
_LIFECYCLE_SELECT_FIELDS = ('stage', 'payment_status', 'onboarding_status')


@admin_required
def website_create(request):
    """Tag a new Website to an existing Account, in one of two shapes.

    "New build" (default) — unchanged from before: account, name, build
    platform only. stage/payment_status/onboarding_status/url all fall to
    the model's own defaults exactly as they did before this form grew
    the extra fields, because a blank submission for any of them is never
    passed to .create() at all — the model default fires instead of a
    duplicated literal that could drift from it.

    "Existing client — site already live, no build" — the no-build shape
    (clients/account_models.py: stage='live', payment_status='fully_paid',
    onboarding_status='intake_complete') is pre-filled into the SAME
    select elements by the page's own JS preset button, not a server-side
    branch — every field stays a normal, overridable form control either
    way. The server only ever sees "stage/payment_status/onboarding_status
    were submitted as X"; it has no notion of which preset produced them.

    Still a single Website.objects.create() call — no new signal surface,
    no provisioning, no email. (Denis Law Group's onboarding depends on
    that staying true: see the Website-creation-fires-nothing audit.)
    """
    from clients.account_models import Account

    if request.method == 'POST':
        account_id = request.POST.get('account_id')
        name = (request.POST.get('name') or '').strip()
        build_platform = request.POST.get('build_platform') or 'custom'
        url = (request.POST.get('url') or '').strip()

        account = (Account.objects.filter(id=account_id).first()
                   if account_id else None)
        valid_platforms = {v for v, _ in Website.BUILD_PLATFORM_CHOICES}

        # Each lifecycle select is optional — blank means "use the model
        # default", exactly like url and like build_platform did before
        # this field existed. Only a non-blank value that isn't one of
        # the field's own choices is an error.
        lifecycle_values = {}
        lifecycle_errors = []
        for fname in _LIFECYCLE_SELECT_FIELDS:
            raw = (request.POST.get(fname) or '').strip()
            if not raw:
                continue
            choices = dict(Website._meta.get_field(fname).choices or [])
            if raw not in choices:
                lifecycle_errors.append(f'Invalid {fname.replace("_", " ")}: {raw!r}.')
            else:
                lifecycle_values[fname] = raw

        if not account:
            messages.error(request, 'Pick an account.')
        elif not name:
            messages.error(request, 'Website name is required.')
        elif build_platform not in valid_platforms:
            messages.error(request, 'Pick a valid build platform.')
        elif lifecycle_errors:
            for e in lifecycle_errors:
                messages.error(request, e)
        else:
            create_kwargs = dict(
                account=account, name=name, build_platform=build_platform)
            create_kwargs.update(lifecycle_values)
            if url:
                create_kwargs['url'] = url
            website = Website.objects.create(**create_kwargs)
            messages.success(
                request, f'{website.name} created under {account.name}.')
            return redirect('admin_dashboard:website_detail',
                             website_id=website.id)

        return render(request, 'admin_dashboard/v2/website_create.html', {
            'accounts': Account.objects.order_by('name'),
            'name': name, 'build_platform': build_platform, 'url': url,
            'selected_account_id': account_id,
            'lifecycle_choices': _lifecycle_choices(),
            'posted_lifecycle': {
                f: (request.POST.get(f) or '')
                for f in _LIFECYCLE_SELECT_FIELDS},
        })

    return render(request, 'admin_dashboard/v2/website_create.html', {
        'accounts': Account.objects.order_by('name'),
        'lifecycle_choices': _lifecycle_choices(),
        'posted_lifecycle': {},
    })


def _lifecycle_choices():
    return {
        fname: list(Website._meta.get_field(fname).choices or [])
        for fname in _LIFECYCLE_SELECT_FIELDS
    }


@admin_required
def websites_list(request):
    q = (request.GET.get('q') or '').strip()
    stage = (request.GET.get('stage') or '').strip()

    qs = Website.objects.select_related('account').order_by(
        'account__name', 'name')
    if q:
        from django.db.models import Q
        qs = qs.filter(Q(name__icontains=q) | Q(account__name__icontains=q))
    if stage:
        qs = qs.filter(stage=stage)

    rows = []
    for site in qs:
        next_action = 'None'
        if site.onboarding_status == 'pending_intake':
            next_action = 'Awaiting intake'
        elif site.stage == 'review':
            next_action = 'Awaiting revision feedback'
        elif site.payment_status != 'fully_paid' and site.stage == 'pre_launch':
            next_action = 'Awaiting final payment'
        rows.append({'site': site, 'next_action': next_action})

    from clients.account_models import WEBSITE_STAGE_CHOICES

    return render(request, 'admin_dashboard/v2/websites_list.html', {
        'rows': rows, 'q': q, 'stage': stage,
        'stage_choices': WEBSITE_STAGE_CHOICES,
    })


def _onboarding_steps(website):
    account = website.account
    contracts = list(website.contracts.all())
    contract_signed = any(c.signed for c in contracts)
    contract_sent = bool(contracts)
    signed_contract = next((c for c in contracts if c.signed), None)

    token = getattr(account, 'onboarding_token_new', None) if account else None
    intake = website.intake

    return [
        {
            'key': 'contract',
            'label': 'Contract sent / signed',
            'done': contract_signed,
            'date': signed_contract.signed_at if signed_contract else None,
            'status_text': (
                'Signed' if contract_signed else
                'Sent, awaiting signature' if contract_sent else
                'Not sent yet'),
        },
        {
            'key': 'payment',
            'label': 'Payment',
            'done': website.payment_status == 'fully_paid',
            'date': website.final_paid_at or website.deposit_paid_at,
            'status_text': website.get_payment_status_display()
            if hasattr(website, 'get_payment_status_display')
            else website.payment_status,
        },
        {
            'key': 'account_setup',
            'label': 'Account setup',
            'done': bool(account and account.onboarding_status == 'complete'),
            'date': None,
            'status_text': (
                'Complete' if account and account.onboarding_status == 'complete'
                else 'Pending setup' if account else 'No account'),
            'token': token,
        },
        {
            'key': 'intake',
            'label': 'Intake',
            'done': bool(intake and intake.completed),
            'date': intake.completed_at if intake else None,
            'status_text': (
                'Submitted' if intake and intake.completed else
                'Pending intake' if website.onboarding_status == 'pending_intake'
                else 'Not applicable yet'),
            'token': token,
        },
    ]


@admin_required
def website_detail(request, website_id):
    website = get_object_or_404(
        Website.objects.select_related('account', 'account__user'),
        id=website_id)
    active_tab = request.GET.get('tab', 'overview')

    intake = website.intake
    intake_photos = list(intake.photos.all()) if intake else []

    setup_cooldown = None
    intake_cooldown = None
    token = getattr(website.account, 'onboarding_token_new', None) \
        if website.account else None
    if token:
        now = timezone.now()
        if token.last_setup_reminder_at:
            elapsed = now - token.last_setup_reminder_at
            if elapsed < SETUP_EMAIL_COOLDOWN:
                setup_cooldown = SETUP_EMAIL_COOLDOWN - elapsed
        if token.last_intake_reminder_at:
            elapsed = now - token.last_intake_reminder_at
            if elapsed < INTAKE_REMINDER_COOLDOWN:
                intake_cooldown = INTAKE_REMINDER_COOLDOWN - elapsed

    from clients.models import OnboardingInvoice

    ctx = {
        'website': website,
        'account': website.account,
        'active_tab': active_tab,
        'has_live_subscription': _has_live_subscription(website),
        'stage_choices': website._meta.get_field('stage').choices,
        # Onboarding
        'onboarding_steps': _onboarding_steps(website),
        'setup_cooldown': setup_cooldown,
        'intake_cooldown': intake_cooldown,
        # Intake
        'intake': intake,
        'intake_photos': intake_photos,
        # Infrastructure
        'has_droplet': website.needs_droplet and bool(website.do_droplet_id),
        'is_wordpress': website.build_platform == 'wordpress',
        # Security
        'scans': website.vulnerability_scans_new.order_by('-created_at')[:20],
        # Domains
        'domains': website.domains.all(),
        # Billing
        'onboarding_invoices': OnboardingInvoice.objects.filter(
            website_new=website).order_by('-created_at'),
        'mini_invoices': website.mini_invoices_new.order_by('-created_at'),
        # Monitoring
        'changelog_entries': website.changelog_entries.order_by('-date_of_change')[:25],
        'uptime_records': website.uptime_records_new.order_by('-checked_at')[:20],
        'stage_logs': website.stage_logs.order_by('-created_at')[:25],
    }
    return render(request, 'admin_dashboard/v2/website_detail.html', ctx)


@admin_required
def website_stage(request, website_id):
    if request.method != 'POST':
        return redirect('admin_dashboard:v2_website_detail',
                         website_id=website_id)

    website = get_object_or_404(Website, id=website_id)
    if _block_if_live_subscription(request, website):
        return redirect('admin_dashboard:v2_website_detail',
                         website_id=website.id)

    action = request.POST.get('action')

    if action == 'payment_override':
        note = (request.POST.get('override_reason') or '').strip()
        if not note:
            messages.error(
                request,
                'A reason is required to record an off-Stripe payment.')
            return redirect('admin_dashboard:v2_website_detail',
                             website_id=website.id)

        setter = _setter_name(request)
        website.payment_status = 'fully_paid'
        if not website.final_paid_at:
            website.final_paid_at = timezone.now()
        website.save(update_fields=[
            'payment_status', 'final_paid_at', 'updated_at'])

        # Reuses the existing verify_website_payment command rather than
        # re-implementing the attestation write — same fields, same
        # auditable pattern the CLI already uses for exactly this case.
        call_command('verify_website_payment', website.slug,
                      by=setter, note=note, apply=True)

        try:
            from clients.services import mark_live
            mark_live(website, set_by=setter, payment_verified=True)
            messages.success(
                request,
                'Payment recorded and site moved to live.')
        except GuardError as e:
            messages.warning(
                request,
                f'Payment recorded, but could not move to live yet: {e}')
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                'v2 payment override mark_live failed for %s', website.id)
            messages.warning(
                request,
                'Payment recorded. Move to live manually from Overview.')
        return redirect('admin_dashboard:v2_website_detail',
                         website_id=website.id)

    new_stage = (request.POST.get('new_stage') or '').strip()
    note = (request.POST.get('note') or '').strip()
    if not new_stage:
        messages.error(request, 'No stage selected.')
        return redirect('admin_dashboard:v2_website_detail',
                         website_id=website.id)

    from clients.services import change_client_stage

    try:
        log, notified = change_client_stage(
            website, new_stage, set_by=_setter_name(request), note=note)
        if log is None:
            messages.info(request, 'Already in that stage.')
        else:
            messages.success(
                request,
                f'Moved to {website.get_stage_display()}.'
                + ('' if notified else ' (client notification did not send)'))
    except GuardError as e:
        messages.error(request, str(e))
    except ValueError as e:
        messages.error(request, str(e))

    return redirect('admin_dashboard:v2_website_detail', website_id=website.id)


@admin_required
def website_send_intake_reminder(request, website_id):
    if request.method != 'POST':
        return redirect('admin_dashboard:v2_website_detail',
                         website_id=website_id)

    website = get_object_or_404(
        Website.objects.select_related('account'), id=website_id)
    account = website.account
    token = getattr(account, 'onboarding_token_new', None) if account else None

    if token is None:
        messages.error(request, 'No onboarding token exists for this account.')
        return redirect('admin_dashboard:v2_website_detail',
                         website_id=website.id)

    if token.last_intake_reminder_at:
        elapsed = timezone.now() - token.last_intake_reminder_at
        if elapsed < INTAKE_REMINDER_COOLDOWN:
            messages.error(
                request,
                'An intake reminder was already sent within the last 48 '
                'hours — try again later.')
            return redirect('admin_dashboard:v2_website_detail',
                             website_id=website.id)

    from clients.tasks import _send_intake_reminder

    try:
        _send_intake_reminder(website, token)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            'v2 intake reminder failed for %s', website.id)
        messages.error(request, 'Could not send the intake reminder.')
        return redirect('admin_dashboard:v2_website_detail',
                         website_id=website.id)

    token.intake_reminders_sent += 1
    token.last_intake_reminder_at = timezone.now()
    token.save(update_fields=[
        'intake_reminders_sent', 'last_intake_reminder_at', 'updated_at'])
    messages.success(request, 'Intake reminder sent.')
    return redirect('admin_dashboard:v2_website_detail', website_id=website.id)


@admin_required
def website_run_scan(request, website_id):
    """Mirrors admin_dashboard.views_scans.run_scan's exact creation
    pattern (create a VulnerabilityScan row, enqueue the Celery task) —
    no new scanning logic."""
    if request.method != 'POST':
        return redirect('admin_dashboard:v2_website_detail',
                         website_id=website_id)

    website = get_object_or_404(Website, id=website_id)
    scan_type = request.POST.get('scan_type') or 'quick'

    from reporting.models import VulnerabilityScan
    from reporting.tasks import run_vulnerability_scan_task

    scan = VulnerabilityScan.objects.create(
        website_new=website,
        target_url=website.live_url or website.staging_url or '',
        scan_type=scan_type,
        status='pending',
    )
    try:
        run_vulnerability_scan_task.delay(str(scan.id))
        messages.success(request, 'Scan queued.')
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            'v2 run_scan enqueue failed for %s', website.id)
        messages.error(request, 'Scan created but could not be queued.')

    return redirect(
        f"{reverse_v2_website_tab(website.id, 'security')}")


def reverse_v2_website_tab(website_id, tab):
    from django.urls import reverse
    return f"{reverse('admin_dashboard:v2_website_detail', args=[website_id])}?tab={tab}"


@admin_required
def website_toggle_auto_send_scan(request, website_id):
    if request.method != 'POST':
        return redirect('admin_dashboard:v2_website_detail',
                         website_id=website_id)
    website = get_object_or_404(Website, id=website_id)
    if _block_if_live_subscription(request, website):
        return redirect(f"{reverse_v2_website_tab(website.id, 'security')}")
    website.auto_send_scan_reports = not website.auto_send_scan_reports
    website.save(update_fields=['auto_send_scan_reports', 'updated_at'])
    messages.success(
        request,
        f"Auto-send scan reports {'enabled' if website.auto_send_scan_reports else 'disabled'}.")
    return redirect(f"{reverse_v2_website_tab(website.id, 'security')}")
