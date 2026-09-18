"""v2 Accounts — list, detail, create-record-only, setup email, password reset."""

from collections import defaultdict
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordResetForm
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from admin_dashboard.decorators import admin_required
from clients.account_models import Account

User = get_user_model()

SETUP_EMAIL_COOLDOWN = timedelta(hours=24)

_SORT_FIELDS = {
    'name': 'name',
    '-name': '-name',
    'created': 'created_at',
    '-created': '-created_at',
}


@admin_required
def accounts_list(request):
    from clients.revenue import get_current_mrr

    q = (request.GET.get('q') or '').strip()
    sort = request.GET.get('sort') or 'name'

    qs = Account.objects.select_related('user').prefetch_related('websites')
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(contact_name__icontains=q)
            | Q(user__email__icontains=q))
    qs = qs.order_by(_SORT_FIELDS.get(sort, 'name'))

    mrr_by_account = defaultdict(float)
    try:
        for row in get_current_mrr()['breakdown']:
            mrr_by_account[row['account']] += row['mrr']
    except Exception:
        pass

    rows = []
    for account in qs:
        sites = list(account.websites.all())
        rows.append({
            'account': account,
            'website_count': len(sites),
            'mrr': mrr_by_account.get(account.name, 0),
        })

    return render(request, 'admin_dashboard/v2/accounts_list.html', {
        'rows': rows, 'q': q, 'sort': sort,
    })


@admin_required
def account_detail(request, account_id):
    """
    Mirrors admin_dashboard.views.account_detail (v1) field-for-field:
    same section list (imported from v1, never duplicated, so the two
    pages can't drift), same single-big-form POST handling, same
    delete-impact computation. Reused rather than reimplemented because
    this is the exact "everything under this account" surface v1 already
    built and tested; v2 only owns the page shell and the URLs.

    Deliberately NOT brought over from v1: Comp products, Contracts,
    Scheduling & add-ons, GBP — not asked for. Add if wanted.
    """
    from django.db.models import Count, Q

    from admin_dashboard.views import _ACCOUNT_EDIT_SECTIONS, _account_field_spec
    from clients.account_models import Website
    from clients.models import SupportTicket

    account = get_object_or_404(
        Account.objects.select_related('user'), id=account_id)
    user = account.user

    if request.method == 'POST':
        # Login-enabled lives on User, not Account — handled first so
        # the one Save button writes both, exactly like v1.
        if 'user_is_active' in request.POST and user is not None:
            new_active = request.POST.get('user_is_active') == 'on'
            if user.is_active != new_active:
                user.is_active = new_active
                user.save(update_fields=['is_active'])

        errors = []
        allowed = {
            fname for _, group in _ACCOUNT_EDIT_SECTIONS for fname, _, _ in group
        }
        checkbox_fields = {
            fname for _, group in _ACCOUNT_EDIT_SECTIONS
            for fname, _, ftype in group if ftype == 'checkbox'
        }
        for field in allowed:
            # An unchecked checkbox sends no key at all — still has to
            # be processed so unchecking actually clears it.
            if field not in request.POST and field not in checkbox_fields:
                continue
            spec = _account_field_spec(field)
            if spec['type'] == 'checkbox':
                setattr(account, field, request.POST.get(field) == 'on')
            elif spec['type'] == 'select':
                value = (request.POST.get(field) or '').strip()
                choices = dict(account._meta.get_field(field).choices or [])
                if value and value not in choices:
                    errors.append(f'{field}: invalid value {value!r}')
                else:
                    setattr(account, field, value)
            else:
                value = (request.POST.get(field) or '').strip()
                setattr(account, field, value)

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            try:
                account.save()
                messages.success(request, 'Account saved.')
                return redirect('admin_dashboard:v2_account_detail',
                                 account_id=account.id)
            except Exception as exc:
                messages.error(request, f'Save failed: {exc}')

    sections = []
    for section_label, fields in _ACCOUNT_EDIT_SECTIONS:
        rendered = []
        for fname, flabel, ftype in fields:
            current = getattr(account, fname, '')
            choices = []
            if ftype == 'select':
                choices = list(account._meta.get_field(fname).choices or [])
            rendered.append({
                'name': fname, 'label': flabel, 'type': ftype,
                'value': current, 'choices': choices,
            })
        sections.append({'label': section_label, 'fields': rendered})

    websites = list(account.websites.all().order_by('name'))
    domains = list(account.domains.all().order_by('domain_name'))

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
        'support_tickets': SupportTicket.objects.filter(
            Q(account_new=account) | Q(website_new__account=account)
        ).distinct().count(),
        'documents': counts.get('documents', 0) or 0,
        'revisions': counts.get('revisions', 0) or 0,
        'scans': counts.get('scans', 0) or 0,
        'active_droplets': 0,
        'active_subscriptions': 0,
    }
    for w in websites:
        if w.do_droplet_id:
            delete_impact['active_droplets'] += 1
        if (w.stripe_hosting_subscription_id
                or w.stripe_maintenance_subscription_id):
            delete_impact['active_subscriptions'] += 1

    onboarding_invoice = account.onboarding_invoices_new.order_by(
        '-created_at').first()
    try:
        mini_invoices = list(
            account.mini_invoices_new.all().order_by('-created_at')[:10])
    except Exception:
        mini_invoices = []

    token = getattr(account, 'onboarding_token_new', None)
    setup_cooldown_remaining = None
    if token and token.last_setup_reminder_at:
        elapsed = timezone.now() - token.last_setup_reminder_at
        if elapsed < SETUP_EMAIL_COOLDOWN:
            setup_cooldown_remaining = SETUP_EMAIL_COOLDOWN - elapsed

    ctx = {
        'account': account,
        'user': user,
        'sections': sections,
        'websites': websites,
        'domains': domains,
        'delete_impact': delete_impact,
        'onboarding_invoice': onboarding_invoice,
        'mini_invoices': mini_invoices,
        'token': token,
        'setup_complete': account.onboarding_status == 'complete',
        'setup_cooldown_remaining': setup_cooldown_remaining,
    }
    return render(request, 'admin_dashboard/v2/account_detail.html', ctx)


@admin_required
def account_send_setup_email(request, account_id):
    if request.method != 'POST':
        return redirect('admin_dashboard:v2_account_detail',
                         account_id=account_id)

    account = get_object_or_404(Account, id=account_id)
    token = getattr(account, 'onboarding_token_new', None)
    if token is None:
        messages.error(
            request,
            'No account-setup token exists for this account yet — one is '
            'created when the onboarding invoice is generated.')
        return redirect('admin_dashboard:v2_account_detail',
                         account_id=account.id)

    if token.last_setup_reminder_at:
        elapsed = timezone.now() - token.last_setup_reminder_at
        if elapsed < SETUP_EMAIL_COOLDOWN:
            messages.error(
                request,
                'Setup email was already sent within the last 24 hours — '
                'try again later.')
            return redirect('admin_dashboard:v2_account_detail',
                             account_id=account.id)

    from clients.emails import send_onboarding_setup_email

    try:
        send_onboarding_setup_email(account, token)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            'v2 send_setup_email failed for account %s', account.id)
        messages.error(request, 'Could not send the setup email — see logs.')
        return redirect('admin_dashboard:v2_account_detail',
                         account_id=account.id)

    token.setup_reminders_sent += 1
    token.last_setup_reminder_at = timezone.now()
    token.save(update_fields=['setup_reminders_sent',
                               'last_setup_reminder_at', 'updated_at'])
    messages.success(request, 'Account setup email sent.')
    return redirect('admin_dashboard:v2_account_detail', account_id=account.id)


@admin_required
def account_reset_password(request, account_id):
    """Mirrors admin_dashboard.views.account_send_password_reset exactly
    (same Django PasswordResetForm + same templates) so a v2 admin isn't
    bounced into the v1 UI to trigger it. Not a new email — same
    mechanism, same templates, just callable from a v2 view so the
    redirect lands back in v2."""
    if request.method != 'POST':
        return redirect('admin_dashboard:v2_account_detail',
                         account_id=account_id)

    account = get_object_or_404(Account.objects.select_related('user'),
                                 id=account_id)
    user = account.user
    if not user or not user.is_active or not user.email:
        messages.error(
            request, 'This account has no active login to send a reset to.')
        return redirect('admin_dashboard:v2_account_detail',
                         account_id=account.id)

    form = PasswordResetForm({'email': user.email})
    if form.is_valid():
        form.save(
            request=request, use_https=request.is_secure(),
            email_template_name='public/password_reset_email.txt',
            subject_template_name='public/password_reset_subject.txt',
            from_email=None)
        messages.success(request, f'Password reset email sent to {user.email}.')
    else:
        messages.error(request, 'Could not send a password reset email.')
    return redirect('admin_dashboard:v2_account_detail', account_id=account.id)


@admin_required
def account_create(request):
    """Records only. Sends nothing — this is also how dormant legacy
    clients get a record without triggering any onboarding email.

    Login is enabled (User.is_active=True) immediately on creation, so
    the client can sign in and complete their own setup right away.
    Account.onboarding_status is explicitly set to 'pending_setup' (the
    field default is 'complete', which would otherwise show this
    freshly-created account as already set up) — this is what routes a
    first login to the account-setup page (WHOIS contact + vault PIN)
    instead of straight into the portal."""
    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()
        email = (request.POST.get('email') or '').strip()
        contact_name = (request.POST.get('contact_name') or '').strip()
        phone = (request.POST.get('phone') or '').strip()

        if not name or not email:
            messages.error(request, 'Name and email are required.')
            return render(request, 'admin_dashboard/v2/account_create.html', {
                'name': name, 'email': email,
                'contact_name': contact_name, 'phone': phone,
            })

        if User.objects.filter(email__iexact=email).exists():
            messages.error(request, f'A user with email {email} already exists.')
            return render(request, 'admin_dashboard/v2/account_create.html', {
                'name': name, 'email': email,
                'contact_name': contact_name, 'phone': phone,
            })

        username = email.split('@')[0][:150]
        suffix = 1
        base_username = username
        while User.objects.filter(username=username).exists():
            suffix += 1
            username = f'{base_username}{suffix}'[:150]

        user = User.objects.create_user(
            username=username, email=email, password=None, is_active=True)
        account = Account.objects.filter(user=user).first() or (
            Account.objects.create(user=user, name=name))
        account.name = name
        account.contact_name = contact_name
        account.phone = phone
        account.onboarding_status = 'pending_setup'
        account.save(update_fields=['name', 'contact_name', 'phone',
                                     'onboarding_status', 'updated_at'])

        messages.success(
            request,
            f'Account record created for {name}. Login is enabled; '
            f'account setup is pending. No email was sent.')
        return redirect('admin_dashboard:v2_account_detail',
                         account_id=account.id)

    return render(request, 'admin_dashboard/v2/account_create.html', {})
