"""v2 Accounts — list, detail, create-record-only, setup email, password reset."""

from collections import defaultdict
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

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


def _live_subscription_websites(account):
    """Websites on this account carrying a live Stripe subscription id.

    v1's account_delete has no check for this — it only refuses
    self-delete and staff/superuser delete, and merely warns (does not
    block) when the modal detects an active subscription. Deleting
    cascades the Account and its Websites, destroying the local record
    of a subscription that keeps billing in Stripe. v2 refuses outright
    rather than warn.
    """
    return [
        w for w in account.websites.all()
        if w.stripe_hosting_subscription_id
        or w.stripe_maintenance_subscription_id
    ]


def _live_subscription_block_message(blocking_websites, action='delete'):
    parts = []
    for w in blocking_websites:
        subs = []
        if w.stripe_hosting_subscription_id:
            subs.append(f'hosting subscription {w.stripe_hosting_subscription_id}')
        if w.stripe_maintenance_subscription_id:
            subs.append(
                f'maintenance subscription {w.stripe_maintenance_subscription_id}')
        parts.append(f'{w.name} ({" and ".join(subs)})')
    return (
        f'Cannot {action} this account — cancel the following in Stripe '
        'first: ' + '; '.join(parts) + '.'
    )


def _block_if_live_subscription(request, account):
    """Guard for the account field editor — same check, same helper as
    the delete guard (_live_subscription_websites), just a different
    action word in the message. Blocks the whole save (including the
    user_is_active toggle) rather than a subset of fields: the editor
    is one Save button writing one POST, so a per-field guard would
    need its own whitelist to stay in sync with _ACCOUNT_EDIT_SECTIONS
    and would leave an unclear rule about which fields are "safe".
    """
    blocking_websites = _live_subscription_websites(account)
    if blocking_websites:
        messages.error(
            request,
            _live_subscription_block_message(blocking_websites, action='edit'))
        return True
    return False


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
    from admin_dashboard.v2 import services

    account = get_object_or_404(
        Account.objects.select_related('user'), id=account_id)
    user = account.user

    if request.method == 'POST':
        if _block_if_live_subscription(request, account):
            return redirect('admin_dashboard:v2_account_detail',
                             account_id=account.id)

        errors = services.apply_account_edit_fields(account, request.POST)

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

    sections = services.build_account_edit_sections(account)

    websites = list(account.websites.all().order_by('name'))
    domains = list(account.domains.all().order_by('domain_name'))

    delete_impact = services.compute_account_delete_impact(account)

    payments = services.account_payments_summary(account)
    onboarding_invoice = payments['onboarding_invoice']
    mini_invoices = payments['mini_invoices']

    token = getattr(account, 'onboarding_token_new', None)
    setup_cooldown_remaining = None
    if token and token.last_setup_reminder_at:
        elapsed = timezone.now() - token.last_setup_reminder_at
        if elapsed < SETUP_EMAIL_COOLDOWN:
            setup_cooldown_remaining = SETUP_EMAIL_COOLDOWN - elapsed

    blocking_websites = _live_subscription_websites(account)

    ctx = {
        'account': account,
        'user': user,
        'sections': sections,
        'websites': websites,
        'domains': domains,
        'delete_impact': delete_impact,
        'delete_blocked_websites': blocking_websites,
        'edit_blocked_websites': blocking_websites,
        'onboarding_invoice': onboarding_invoice,
        'mini_invoices': mini_invoices,
        'token': token,
        'setup_complete': account.onboarding_status == 'complete',
        'setup_cooldown_remaining': setup_cooldown_remaining,
    }
    return render(request, 'admin_dashboard/v2/account_detail.html', ctx)


@admin_required
@require_POST
def account_delete(request, account_id):
    """Guard in front of v1's admin_dashboard.views.account_delete.

    v1's view has no live-subscription check — it only refuses
    self-delete and staff/superuser delete, and the confirmation modal
    merely *warns* about an active subscription without blocking.
    Deleting cascades the Account and its Websites, which would destroy
    the local record of a subscription still billing in Stripe.

    This view refuses outright (server-side — the template also disables
    the button, but that alone is not a guard) when any website on the
    account has a live stripe_hosting_subscription_id or
    stripe_maintenance_subscription_id. When clear, it calls v1's
    account_delete directly — not reimplemented, not duplicated — so
    v1's confirm-by-name check, safety rails, and cascade transaction
    run completely unchanged.
    """
    account = get_object_or_404(Account, id=account_id)
    blocking_websites = _live_subscription_websites(account)
    if blocking_websites:
        messages.error(request, _live_subscription_block_message(blocking_websites))
        return redirect('admin_dashboard:v2_account_detail',
                         account_id=account.id)

    from admin_dashboard.views import account_delete as v1_account_delete
    return v1_account_delete(request, account_id)


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

    from admin_dashboard.v2 import services

    account = get_object_or_404(Account.objects.select_related('user'),
                                 id=account_id)
    ok, msg = services.send_account_password_reset(account, request)
    if ok:
        messages.success(request, msg)
    else:
        messages.error(request, msg)
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
