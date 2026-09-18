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
    account = get_object_or_404(
        Account.objects.select_related('user'), id=account_id)

    if request.method == 'POST' and request.POST.get('action') == 'save_contact':
        account.name = (request.POST.get('name') or account.name).strip()
        account.contact_name = (request.POST.get('contact_name') or '').strip()
        account.phone = (request.POST.get('phone') or '').strip()
        account.email_alt = (request.POST.get('email_alt') or '').strip()
        account.address = (request.POST.get('address') or '').strip()
        account.city = (request.POST.get('city') or '').strip()
        account.state = (request.POST.get('state') or '').strip()
        account.zip_code = (request.POST.get('zip_code') or '').strip()
        account.save(update_fields=[
            'name', 'contact_name', 'phone', 'email_alt', 'address',
            'city', 'state', 'zip_code', 'updated_at',
        ])
        messages.success(request, 'Contact info updated.')
        return redirect('admin_dashboard:v2_account_detail',
                         account_id=account.id)

    token = getattr(account, 'onboarding_token_new', None)
    setup_cooldown_remaining = None
    if token and token.last_setup_reminder_at:
        elapsed = timezone.now() - token.last_setup_reminder_at
        if elapsed < SETUP_EMAIL_COOLDOWN:
            setup_cooldown_remaining = SETUP_EMAIL_COOLDOWN - elapsed

    ctx = {
        'account': account,
        'websites': account.websites.all(),
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
    """Records only. Creates an inactive User + Account. Sends nothing —
    this is also how dormant legacy clients get a record without
    triggering any onboarding email."""
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
            username=username, email=email, password=None, is_active=False)
        account = Account.objects.filter(user=user).first() or (
            Account.objects.create(user=user, name=name))
        account.name = name
        account.contact_name = contact_name
        account.phone = phone
        account.save(update_fields=['name', 'contact_name', 'phone',
                                     'updated_at'])

        messages.success(
            request,
            f'Account record created for {name}. No email was sent.')
        return redirect('admin_dashboard:v2_account_detail',
                         account_id=account.id)

    return render(request, 'admin_dashboard/v2/account_create.html', {})
