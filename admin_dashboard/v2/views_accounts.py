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
        or w.stripe_build_installment_subscription_id
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
        if w.stripe_build_installment_subscription_id:
            subs.append(
                'build installment subscription '
                f'{w.stripe_build_installment_subscription_id}')
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


def _account_is_set_up(account):
    """Whether this account has actually completed setup — a property of
    the User, not of Account.onboarding_status.

    onboarding_status is a workflow label multiple paths can write
    (refactor_to_accounts backfill, v1's intake-completion handler, manual
    edits) without the client ever having set a password. "Set up" means
    the client can actually log in: an active User with a usable password.

    last_login is deliberately NOT part of this check — a client who has
    set a password but hasn't logged in yet is still set up; login timing
    says nothing about whether setup happened.
    """
    user = getattr(account, 'user', None)
    return bool(user is not None and user.is_active
                and user.has_usable_password())


def _setup_recipient_email(account):
    """The address the setup email would actually go to, or '' if none.

    Same resolver the send itself relies on: clients.emails._recipient is
    a private wrapper one layer in front of this exact function
    (clients.display.owner_recipient), so calling it here means "no
    address" is judged by the identical rule the send would silently no-op
    against — not a second, possibly-drifting definition of "has an
    email". owner_recipient also checks Account.email_alt as a fallback
    when the User has none, so that counts as a usable recipient too.
    """
    from clients.display import owner_recipient

    email, _name = owner_recipient(account)
    return (email or '').strip()


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
            # The template's `|timeuntil` needs the future datetime the
            # cooldown ends at, not the remaining timedelta — passing the
            # timedelta crashed the page (`AttributeError: 'datetime.
            # timedelta' object has no attribute 'year'`) the first time
            # anyone actually clicked resend inside the 24h window.
            setup_cooldown_remaining = (
                token.last_setup_reminder_at + SETUP_EMAIL_COOLDOWN)

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
        'setup_complete': _account_is_set_up(account),
        'setup_cooldown_remaining': setup_cooldown_remaining,
        'setup_no_recipient': not _setup_recipient_email(account),
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


def _mint_onboarding_token(account):
    """Mint the OnboardingInvoice + OnboardingToken pair for an account
    that has neither — the existing admin flows only mint a token
    alongside invoice generation (admin_dashboard/views.py:1951 and
    :2337), which leaves an existing client with no invoice unable to
    ever be sent a setup link. Mirrors the zero-dollar pattern at
    admin_dashboard/views.py:2327-2338 (same OnboardingInvoice shape,
    same "no invoice required" zero-amount/paid convention) minus the
    User/Account/Website creation that flow does — this is for an
    account that already has both.

    get_or_create on OnboardingToken.account_new (a OneToOneField, so
    unique at the DB level) means at most one token is ever minted per
    account: a concurrent second caller gets created=False and reuses
    the row Django's get_or_create already retried the get() for, so
    the invoice-create below only runs on the one call that actually
    won the insert.
    """
    from decimal import Decimal

    from django.db import transaction

    from clients.models import OnboardingInvoice, OnboardingToken

    with transaction.atomic():
        token, created = OnboardingToken.objects.get_or_create(
            account_new=account)
        if created:
            OnboardingInvoice.objects.create(
                account_new=account,
                website_new=None,
                line_items=[],
                total_amount=Decimal('0'),
                status='paid',
                sent_at=timezone.now(),
                paid_at=timezone.now(),
            )
    return token


@admin_required
def account_send_setup_email(request, account_id):
    if request.method != 'POST':
        return redirect('admin_dashboard:v2_account_detail',
                         account_id=account_id)

    account = get_object_or_404(Account, id=account_id)

    # Server-side, ahead of any mint. A crafted POST bypassing the
    # disabled template button must get the same refusal, and this must
    # run before _mint_onboarding_token — an account with no deliverable
    # address should come out of a refused send with the database
    # untouched: no token, no invoice, no sent date.
    if not _setup_recipient_email(account):
        messages.error(
            request,
            'This account has no email address on file — the setup '
            'email would be sent to nobody. Add one under Billing email '
            '(Account State section) or fix the linked User\'s email, '
            'then try again.')
        return redirect('admin_dashboard:v2_account_detail',
                         account_id=account.id)

    token = getattr(account, 'onboarding_token_new', None)
    if token is None:
        token = _mint_onboarding_token(account)

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
