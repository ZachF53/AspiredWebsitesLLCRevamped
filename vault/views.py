"""
Vault views — PIN-gated, AES-256-GCM encrypted credential manager.

Every view is staff-only (admin_required). Credential views additionally
require an unlocked vault — get_vault_key() must return a key, which it
only does within 1 hour of a verified PIN entry.
"""

import csv
import logging
from datetime import datetime, timedelta

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from admin_dashboard.decorators import admin_required
from clients.models import ClientProfile

from .crypto import (
    decrypt_value,
    derive_key,
    encrypt_value,
    generate_salt,
    hash_pin,
    make_hint,
    reencrypt_credential_with_pin_key,
    unwrap_key,
    verify_pin,
    wrap_key,
)
from .forms import CommandForm, CredentialForm
from .models import (
    ClientVault,
    ServerCommandLibrary,
    SSHSessionLog,
    VaultAccessLog,
    VaultConfig,
    VaultCredential,
)

logger = logging.getLogger(__name__)

SESSION_HOURS = 1
MAX_ATTEMPTS = 5
LOCKOUT_MINUTES = 30
PIN_HOLD_MINUTES = 5  # how long a PIN-only verification stays "remembered"
                      # so the user only has to fix the TOTP field

# Quick-fill templates for the Add Credential page ({firm} is substituted).
CREDENTIAL_TEMPLATES = [
    {'key': 'do', 'name': 'DigitalOcean Server', 'category': 'server',
     'label': 'DigitalOcean — [domain]'},
    {'key': 'domain', 'name': 'Domain Registrar', 'category': 'domain',
     'label': 'Domain Registrar — [domain]'},
    {'key': 'google', 'name': 'Google Account', 'category': 'google',
     'label': 'Google Account — {firm}'},
    {'key': 'facebook', 'name': 'Facebook / Meta', 'category': 'social',
     'label': 'Facebook Business — {firm}'},
    {'key': 'instagram', 'name': 'Instagram', 'category': 'social',
     'label': 'Instagram — {firm}'},
    {'key': 'linkedin', 'name': 'LinkedIn', 'category': 'social',
     'label': 'LinkedIn — {firm}'},
    {'key': 'sendgrid', 'name': 'SendGrid', 'category': 'email',
     'label': 'SendGrid API'},
    {'key': 'stripe', 'name': 'Stripe', 'category': 'stripe',
     'label': 'Stripe Account'},
    {'key': 'custom', 'name': 'Custom', 'category': 'custom', 'label': ''},
]


# ── Session / key helpers ───────────────────────────────────────────────────

def get_vault_key(request):
    """Return the AES key if the vault is unlocked and < 1h old, else None."""
    unlocked_at = request.session.get('vault_unlocked_at')
    wrapped = request.session.get('vault_key_wrapped')
    if not unlocked_at or not wrapped:
        return None
    try:
        unlocked_time = datetime.fromisoformat(unlocked_at)
    except (TypeError, ValueError):
        return None
    if timezone.now() > unlocked_time + timedelta(hours=SESSION_HOURS):
        request.session.pop('vault_unlocked_at', None)
        request.session.pop('vault_key_wrapped', None)
        # TOTP verification is bound to the same PIN session — drop it too,
        # so the next unlock requires PIN + TOTP again.
        request.session.pop('vault_totp_verified', None)
        return None
    return unwrap_key(wrapped)


def _unlock_session(request, key):
    """Store the wrapped key + unlock timestamp in the session."""
    request.session['vault_unlocked_at'] = timezone.now().isoformat()
    request.session['vault_key_wrapped'] = wrap_key(key)


def _seconds_remaining(request):
    unlocked_at = request.session.get('vault_unlocked_at')
    if not unlocked_at:
        return 0
    try:
        unlocked_time = datetime.fromisoformat(unlocked_at)
    except (TypeError, ValueError):
        return 0
    remaining = unlocked_time + timedelta(hours=SESSION_HOURS) - timezone.now()
    return max(int(remaining.total_seconds()), 0)


def _log(action, request, client_name='', credential_label='', note=''):
    VaultAccessLog.objects.create(
        action=action,
        client_name=client_name,
        credential_label=credential_label,
        note=note,
        ip_address=request.META.get('REMOTE_ADDR'),
    )


def _sync_client_plain(cred, key):
    """Mirror (or clear) the client-visible plaintext copy of a credential."""
    if cred.visible_to_client:
        cred.client_username_plain = decrypt_value(cred.username_encrypted, key)
        cred.client_password_plain = decrypt_value(cred.password_encrypted, key)
        cred.client_url_plain = decrypt_value(cred.url_encrypted, key)
        cred.client_notes_plain = decrypt_value(cred.notes_encrypted, key)
    else:
        cred.client_username_plain = ''
        cred.client_password_plain = ''
        cred.client_url_plain = ''
        cred.client_notes_plain = ''


def _apply_ssh_fields(cred, cd, key):
    """Encrypt the SSH fields from a CredentialForm onto the credential."""
    cred.is_ssh_credential = cd.get('is_ssh_credential', False)
    if not cred.is_ssh_credential:
        return
    cred.ssh_host_encrypted = encrypt_value(cd.get('ssh_host', ''), key)
    cred.ssh_port = cd.get('ssh_port') or 22
    cred.ssh_username_encrypted = encrypt_value(cd.get('ssh_username', ''), key)
    cred.ssh_auth_type = cd.get('ssh_auth_type') or 'password'
    cred.ssh_password_encrypted = encrypt_value(cd.get('ssh_password', ''), key)
    cred.ssh_private_key_encrypted = encrypt_value(
        cd.get('ssh_private_key', ''), key)
    cred.ssh_key_passphrase_encrypted = encrypt_value(
        cd.get('ssh_key_passphrase', ''), key)


def _host_hint(cred, vault_key):
    """A masked server hint — only the last IP octet, or the bare domain."""
    host = decrypt_value(cred.ssh_host_encrypted, vault_key)
    if not host or host == '[decryption failed]':
        return '(server)'
    parts = host.split('.')
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return f'Server ***.***.*.{parts[-1]}'
    return host


def _command_groups(cred):
    """ServerCommandLibrary entries for a credential, grouped by category."""
    groups = []
    for cat_key, cat_label in ServerCommandLibrary.CATEGORY_CHOICES:
        items = list(cred.commands.filter(category=cat_key))
        if items:
            groups.append({'key': cat_key, 'label': cat_label, 'items': items})
    return groups


# ── PIN gate / home ─────────────────────────────────────────────────────────

@admin_required
def vault_home(request):
    config = VaultConfig.get()
    now = timezone.now()

    # First-time setup.
    if not config.pin_set:
        if request.method == 'POST':
            return _handle_pin_setup(request, config)
        return render(request, 'vault/setup_pin.html', {'active': 'vault'})

    # Locked out?
    if config.lockout_until and config.lockout_until > now:
        return render(request, 'vault/locked.html', {
            'active': 'vault',
            'lockout_until': config.lockout_until.isoformat(),
        })

    # PIN entry (and TOTP, once configured).
    if request.method == 'POST':
        return _handle_pin_entry(request, config)

    # Already unlocked AND TOTP-verified this session?
    if get_vault_key(request) is not None and (
            not config.totp_configured
            or request.session.get('vault_totp_verified')):
        return _render_vault_home(request)

    return render(request, 'vault/enter_pin.html', {
        'active': 'vault',
        'expired': bool(request.session.get('vault_was_unlocked')),
        'next': request.GET.get('next', ''),
        'totp_required': config.totp_configured,
        # If a credential is still encrypted under the old per-server scheme
        # (or just no TOTP at all yet), surface the one-time migration notice.
        'needs_totp_setup_after_pin': not config.totp_configured,
    })


def _handle_pin_setup(request, config):
    pin = (request.POST.get('pin') or '').strip()
    confirm = (request.POST.get('pin_confirm') or '').strip()
    error = None
    if not (pin.isdigit() and len(pin) == 4):
        error = 'PIN must be exactly 4 digits.'
    elif pin != confirm:
        error = 'The two PINs do not match.'
    if error:
        return render(request, 'vault/setup_pin.html',
                      {'active': 'vault', 'error': error})

    salt = generate_salt()
    config.encryption_salt = salt
    config.pin_hash = hash_pin(pin, salt)
    config.pin_set = True
    config.failed_attempts = 0
    config.lockout_until = None
    config.save()

    key = derive_key(pin, salt)
    _unlock_session(request, key)
    request.session['vault_was_unlocked'] = True
    _log('pin_set', request, note='Vault PIN created.')
    # Force vault-level TOTP enrolment before anything else is reachable.
    return redirect('vault:totp_setup')


def _handle_pin_entry(request, config):
    """
    Combined PIN + TOTP submission.

    Once TOTP is configured, both must be supplied together. PIN failures
    still drive the global lockout counter; TOTP failures don't (because
    the PIN was already correct — a wrong TOTP just re-prompts).
    """
    from .totp_helpers import get_decrypted_totp_secret, verify_totp_code

    now = timezone.now()
    pin = ''.join(request.POST.get(f'd{i}', '') for i in range(1, 5)).strip()
    if not pin:
        pin = (request.POST.get('pin') or '').strip()
    totp_code = ''.join(
        request.POST.get(f't{i}', '') for i in range(1, 7)).strip()
    if not totp_code:
        totp_code = (request.POST.get('totp_code') or '').strip()

    salt = bytes(config.encryption_salt)
    next_url = request.POST.get('next') or request.GET.get('next') or ''

    # ── PIN check ───────────────────────────────────────────────────────────
    if not verify_pin(pin, config.pin_hash, salt):
        config.failed_attempts += 1
        if config.failed_attempts >= MAX_ATTEMPTS:
            config.lockout_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            config.failed_attempts = 0
            config.save()
            _log('pin_locked', request,
                 note=f'Locked {LOCKOUT_MINUTES} min after '
                      f'{MAX_ATTEMPTS} failures.')
            return render(request, 'vault/locked.html', {
                'active': 'vault',
                'lockout_until': config.lockout_until.isoformat(),
            })
        config.save()
        _log('pin_failed', request,
             note=f'Failed attempt {config.failed_attempts} of {MAX_ATTEMPTS}.')
        remaining = MAX_ATTEMPTS - config.failed_attempts
        return render(request, 'vault/enter_pin.html', {
            'active': 'vault',
            'pin_error': (f'Incorrect PIN — {remaining} attempt'
                          f'{"" if remaining == 1 else "s"} remaining '
                          f'before a {LOCKOUT_MINUTES}-minute lockout.'),
            'next': next_url,
            'totp_required': config.totp_configured,
        })

    # PIN good.
    config.failed_attempts = 0
    config.lockout_until = None
    config.save()
    key = derive_key(pin, salt)

    # ── TOTP check (only if configured) ─────────────────────────────────────
    if config.totp_configured:
        secret = get_decrypted_totp_secret(config, key)
        if not secret or not verify_totp_code(secret, totp_code):
            _log('pin_failed', request,
                 note='PIN OK but TOTP code wrong on unlock.')
            return render(request, 'vault/enter_pin.html', {
                'active': 'vault',
                'totp_error': 'Incorrect authenticator code.',
                'next': next_url,
                'totp_required': True,
                # PIN was right — leave its boxes empty so a re-submit
                # requires both again. (We don't trust a hidden-PIN trick:
                # the PIN never leaves the form except as 4 digits.)
            })
        request.session['vault_totp_verified'] = True

    _unlock_session(request, key)
    request.session['vault_was_unlocked'] = True
    _log('pin_verified', request, note='Vault unlocked.')

    if next_url.startswith('/admin-dashboard/vault/'):
        return redirect(next_url)
    return redirect('vault:home')


def _render_vault_home(request):
    query = (request.GET.get('q') or '').strip()
    clients = ClientProfile.objects.order_by('firm_name')
    if query:
        clients = clients.filter(firm_name__icontains=query)

    vaults = []
    for client in clients:
        vault = getattr(client, 'vault', None)
        creds = list(vault.credentials.all()) if vault else []
        vaults.append({
            'client': client,
            'count': len(creds),
            'categories': sorted({c.get_category_display() for c in creds}),
        })
    return render(request, 'vault/home.html', {
        'active': 'vault',
        'vaults': vaults,
        'query': query,
        'seconds_remaining': _seconds_remaining(request),
    })


@admin_required
@require_POST
def new_vault(request):
    """
    Create a new vault entry by name — for an internal property (your own
    site, Moonieful, etc.) that didn't arrive through client onboarding.
    Creates a placeholder, login-disabled User + ClientProfile; the
    ClientProfile post_save signal then creates the ClientVault.
    """
    if get_vault_key(request) is None:
        return redirect('vault:home')
    name = (request.POST.get('name') or '').strip()
    if not name:
        return redirect('vault:home')

    from django.contrib.auth import get_user_model
    from django.utils.text import slugify
    User = get_user_model()

    base = 'vault-' + (slugify(name) or 'entry')
    username = base
    suffix = 1
    while User.objects.filter(username=username).exists():
        username = f'{base}-{suffix}'
        suffix += 1

    user = User(username=username, is_staff=False, is_active=False)
    user.set_unusable_password()  # placeholder — this user never logs in
    user.save()
    profile = ClientProfile.objects.create(
        user=user, firm_name=name, business_type='',
    )
    return redirect('vault:client_vault', client_id=profile.id)


# ── Client vault ────────────────────────────────────────────────────────────

@admin_required
def client_vault(request, client_id):
    key = get_vault_key(request)
    if key is None:
        return redirect(f"{reverse('vault:home')}?next={request.path}")

    client = get_object_or_404(ClientProfile, id=client_id)
    vault, _ = ClientVault.objects.get_or_create(client=client)

    # Re-encrypt any server-key-encrypted credentials (auto-provisioned
    # before any admin had unlocked the vault) under the PIN key now that
    # we have it. Idempotent — does nothing once the flag is cleared.
    provisioning_reencrypted_count = 0
    for cred in vault.credentials.filter(encrypted_with_server_key=True):
        try:
            if reencrypt_credential_with_pin_key(cred, key):
                provisioning_reencrypted_count += 1
        except Exception:
            logger.exception(
                'vault: failed to re-encrypt %s under PIN key', cred.pk)

    groups = []
    for cat_key, cat_label in VaultCredential.CATEGORY_CHOICES:
        items = []
        for cred in vault.credentials.filter(
                category=cat_key).select_related('website_new'):
            items.append({
                'id': cred.id,
                'label': cred.label,
                'username_hint': cred.username_hint or '—',
                'url': decrypt_value(cred.url_encrypted, key),
                'notes': decrypt_value(cred.notes_encrypted, key),
                'has_password': bool(cred.password_encrypted),
                'visible_to_client': cred.visible_to_client,
                'is_ssh_credential': cred.is_ssh_credential,
                # Phase C5 — per-credential website tag. Renders next
                # to the label so admins can see which build each cred
                # belongs to without leaving the vault page.
                'website_name': (
                    cred.website_new.name if cred.website_new_id
                    else 'Account-wide'),
                'website_id': (
                    cred.website_new_id if cred.website_new_id else None),
            })
        if items:
            groups.append({'key': cat_key, 'label': cat_label, 'items': items})

    # Phase C5 — alternate "grouped by website" view of the same
    # credentials. Renders alongside the category grouping in the
    # template via a tab toggle. One pass over the queryset; no
    # extra DB hits.
    site_groups = {}
    for cat_key, cat_label in VaultCredential.CATEGORY_CHOICES:
        for cred in vault.credentials.filter(
                category=cat_key).select_related('website_new'):
            site_id = cred.website_new_id or '__account_wide__'
            site_name = (
                cred.website_new.name if cred.website_new_id
                else 'Account-wide')
            bucket = site_groups.setdefault(site_id, {
                'site_id': cred.website_new_id,
                'site_name': site_name,
                'items': [],
            })
            bucket['items'].append({
                'id': cred.id,
                'label': cred.label,
                'category_label': cat_label,
                'username_hint': cred.username_hint or '—',
                'url': decrypt_value(cred.url_encrypted, key),
                'notes': decrypt_value(cred.notes_encrypted, key),
                'has_password': bool(cred.password_encrypted),
                'visible_to_client': cred.visible_to_client,
                'is_ssh_credential': cred.is_ssh_credential,
            })
    # Stable ordering: account-wide first, then site name A-Z.
    sorted_site_groups = []
    if '__account_wide__' in site_groups:
        sorted_site_groups.append(site_groups.pop('__account_wide__'))
    sorted_site_groups.extend(
        sorted(site_groups.values(), key=lambda g: g['site_name']))

    # Pull the account's websites for the cred-add form's site-tag
    # dropdown (Phase C5 — add-credential UI gets a "Belongs to which
    # website?" picker). Resolve via the Account derived from this
    # client. Falls back to [] for environments where the backfill
    # hasn't run.
    websites_for_tagging = []
    try:
        from clients.account_models import Account
        acc = Account.objects.filter(legacy_client_profile=client).first()
        if acc is not None:
            websites_for_tagging = list(
                acc.websites.all().order_by('name'))
    except Exception:
        pass

    return render(request, 'vault/client_vault.html', {
        'active': 'vault',
        'client': client,
        'vault': vault,
        'groups': groups,
        'site_groups': sorted_site_groups,
        'websites_for_tagging': websites_for_tagging,
        'provisioning_reencrypted_count': provisioning_reencrypted_count,
        'seconds_remaining': _seconds_remaining(request),
    })


@admin_required
@require_POST
def reveal_credential(request, client_id, cred_id):
    key = get_vault_key(request)
    if key is None:
        return JsonResponse({'error': 'Vault locked'}, status=403)

    cred = get_object_or_404(
        VaultCredential, id=cred_id, vault__client_id=client_id,
    )
    _log('credential_viewed', request,
         client_name=cred.vault.client.firm_name,
         credential_label=cred.label,
         note='Decrypted values revealed.')
    return JsonResponse({
        'username': decrypt_value(cred.username_encrypted, key),
        'password': decrypt_value(cred.password_encrypted, key),
        'url': decrypt_value(cred.url_encrypted, key),
        'notes': decrypt_value(cred.notes_encrypted, key),
    })


@admin_required
def add_credential(request, client_id):
    key = get_vault_key(request)
    if key is None:
        return redirect(f"{reverse('vault:home')}?next={request.path}")

    client = get_object_or_404(ClientProfile, id=client_id)
    vault, _ = ClientVault.objects.get_or_create(client=client)

    if request.method == 'POST':
        form = CredentialForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            cred = VaultCredential(
                vault=vault,
                label=cd['label'],
                category=cd['category'],
                sort_order=cd['sort_order'],
                visible_to_client=cd['visible_to_client'],
                username_encrypted=encrypt_value(cd['username'], key),
                password_encrypted=encrypt_value(cd['password'], key),
                url_encrypted=encrypt_value(cd['url'], key),
                notes_encrypted=encrypt_value(cd['notes'], key),
                username_hint=make_hint(cd['username']),
            )
            _sync_client_plain(cred, key)
            _apply_ssh_fields(cred, cd, key)
            cred.save()
            _log('credential_created', request,
                 client_name=client.firm_name, credential_label=cred.label)
            return redirect('vault:client_vault', client_id=client.id)
    else:
        form = CredentialForm()

    templates = [
        {**t, 'label': t['label'].replace('{firm}', client.firm_name)}
        for t in CREDENTIAL_TEMPLATES
    ]
    return render(request, 'vault/credential_form.html', {
        'active': 'vault',
        'client': client,
        'form': form,
        'mode': 'add',
        'templates': templates,
        'seconds_remaining': _seconds_remaining(request),
    })


@admin_required
def edit_credential(request, client_id, cred_id):
    key = get_vault_key(request)
    if key is None:
        return redirect(f"{reverse('vault:home')}?next={request.path}")

    client = get_object_or_404(ClientProfile, id=client_id)
    cred = get_object_or_404(
        VaultCredential, id=cred_id, vault__client_id=client_id,
    )

    if request.method == 'POST':
        form = CredentialForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            cred.label = cd['label']
            cred.category = cd['category']
            cred.sort_order = cd['sort_order']
            cred.visible_to_client = cd['visible_to_client']
            # Only re-encrypt a sensitive field when its "change" flag is set.
            if cd['change_username']:
                cred.username_encrypted = encrypt_value(cd['username'], key)
                cred.username_hint = make_hint(cd['username'])
            if cd['change_password']:
                cred.password_encrypted = encrypt_value(cd['password'], key)
            if cd['change_url']:
                cred.url_encrypted = encrypt_value(cd['url'], key)
            if cd['change_notes']:
                cred.notes_encrypted = encrypt_value(cd['notes'], key)
            _sync_client_plain(cred, key)
            _apply_ssh_fields(cred, cd, key)
            cred.save()
            _log('credential_updated', request,
                 client_name=client.firm_name, credential_label=cred.label)
            return redirect('vault:client_vault', client_id=client.id)
    else:
        initial = {
            'label': cred.label,
            'category': cred.category,
            'sort_order': cred.sort_order,
            'visible_to_client': cred.visible_to_client,
            'is_ssh_credential': cred.is_ssh_credential,
        }
        if cred.is_ssh_credential:
            initial.update({
                'ssh_host': decrypt_value(cred.ssh_host_encrypted, key),
                'ssh_port': cred.ssh_port,
                'ssh_username': decrypt_value(cred.ssh_username_encrypted, key),
                'ssh_auth_type': cred.ssh_auth_type or 'password',
                'ssh_password': decrypt_value(cred.ssh_password_encrypted, key),
                'ssh_private_key': decrypt_value(
                    cred.ssh_private_key_encrypted, key),
                'ssh_key_passphrase': decrypt_value(
                    cred.ssh_key_passphrase_encrypted, key),
            })
        form = CredentialForm(initial=initial)

    return render(request, 'vault/credential_form.html', {
        'active': 'vault',
        'client': client,
        'form': form,
        'mode': 'edit',
        'credential': cred,
        'templates': [],
        'seconds_remaining': _seconds_remaining(request),
    })


@admin_required
@require_POST
def delete_credential(request, client_id, cred_id):
    if get_vault_key(request) is None:
        return redirect(f"{reverse('vault:home')}?next="
                        f"{reverse('vault:client_vault', args=[client_id])}")
    cred = get_object_or_404(
        VaultCredential, id=cred_id, vault__client_id=client_id,
    )
    label, firm = cred.label, cred.vault.client.firm_name
    cred.delete()
    _log('credential_deleted', request,
         client_name=firm, credential_label=label)
    return redirect('vault:client_vault', client_id=client_id)


@admin_required
@require_POST
def toggle_visibility(request, client_id, cred_id):
    """HTMX — flip visible_to_client and sync/clear the client plaintext copy."""
    key = get_vault_key(request)
    if key is None:
        return JsonResponse({'error': 'Vault locked'}, status=403)
    cred = get_object_or_404(
        VaultCredential, id=cred_id, vault__client_id=client_id,
    )
    cred.visible_to_client = not cred.visible_to_client
    _sync_client_plain(cred, key)
    cred.save()
    _log('credential_updated', request,
         client_name=cred.vault.client.firm_name,
         credential_label=cred.label,
         note=f'visible_to_client set to {cred.visible_to_client}.')
    return render(request, 'vault/_visibility_toggle.html', {
        'client_id': client_id,
        'cred_id': cred.id,
        'visible': cred.visible_to_client,
    })


# ── Access log ──────────────────────────────────────────────────────────────

@admin_required
def vault_access_log(request):
    logs = VaultAccessLog.objects.all()
    action = request.GET.get('action', '')
    client_name = (request.GET.get('client') or '').strip()
    date_from = request.GET.get('from', '')
    date_to = request.GET.get('to', '')
    if action:
        logs = logs.filter(action=action)
    if client_name:
        logs = logs.filter(client_name__icontains=client_name)
    if date_from:
        logs = logs.filter(created_at__date__gte=date_from)
    if date_to:
        logs = logs.filter(created_at__date__lte=date_to)

    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = (
            'attachment; filename="vault-access-log.csv"'
        )
        writer = csv.writer(response)
        writer.writerow(['Timestamp', 'Action', 'Client', 'Credential',
                         'IP Address', 'Note'])
        for entry in logs:
            writer.writerow([
                entry.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                entry.get_action_display(), entry.client_name,
                entry.credential_label, entry.ip_address or '', entry.note,
            ])
        return response

    return render(request, 'vault/access_log.html', {
        'active': 'vault',
        'logs': logs[:500],
        'action_choices': VaultAccessLog.ACTION_CHOICES,
        'filter_action': action,
        'filter_client': client_name,
        'filter_from': date_from,
        'filter_to': date_to,
    })


# ── Vault-level TOTP setup + SSH terminal ──────────────────────────────────

@admin_required
def totp_setup(request):
    """
    Vault-level TOTP enrolment. Runs once, right after PIN setup (or any
    time TOTP is unconfigured) — the secret lives on the VaultConfig
    singleton, AES-encrypted with the PIN-derived vault key.

    On successful verify we also mint 8 one-time recovery codes and
    render the show-once page; the plaintext only lives in that one HTTP
    response and never touches the database.
    """
    key = get_vault_key(request)
    if key is None:
        return redirect(f"{reverse('vault:home')}?next={request.path}")

    config = VaultConfig.get()
    if config.totp_configured:
        # Already enrolled — just send them to the vault home.
        return redirect('vault:home')

    from .recovery import generate_recovery_codes, store_recovery_codes
    from .totp_helpers import (
        ACCOUNT_NAME, ISSUER_NAME,
        generate_qr_code_base64, generate_totp_secret, get_totp_uri,
        verify_totp_code,
    )
    session_key = 'vault_totp_setup_secret'

    error = None
    if request.method == 'POST':
        secret = request.session.get(session_key)
        code = (request.POST.get('code') or '').strip()
        if secret and verify_totp_code(secret, code):
            plaintext_codes = generate_recovery_codes()
            config.totp_secret_encrypted = encrypt_value(secret, key)
            config.totp_configured = True
            store_recovery_codes(config, plaintext_codes)
            config.save(update_fields=[
                'totp_secret_encrypted', 'totp_configured',
                'recovery_codes', 'updated_at'])
            request.session.pop(session_key, None)
            # Mark this session as TOTP-verified so the admin doesn't have
            # to immediately re-enter the code they just generated.
            request.session['vault_totp_verified'] = True
            _log('ssh_totp_setup', request,
                 note='Vault-level TOTP configured.')
            # Show-once: render the codes page directly. The plaintext is
            # only in this response — there's no re-render.
            return render(request, 'vault/totp_codes.html', {
                'active': 'vault',
                'recovery_codes': plaintext_codes,
                'seconds_remaining': _seconds_remaining(request),
            })
        error = 'Code incorrect — try again.'
        secret = secret or generate_totp_secret()
    else:
        secret = generate_totp_secret()

    request.session[session_key] = secret
    uri = get_totp_uri(secret)
    return render(request, 'vault/totp_setup.html', {
        'active': 'vault',
        'secret': secret,
        'qr_code': generate_qr_code_base64(uri),
        'error': error,
        'issuer_name': ISSUER_NAME,
        'account_name': ACCOUNT_NAME,
        'seconds_remaining': _seconds_remaining(request),
    })


@admin_required
def recover(request):
    """
    Lost-authenticator entry point. The admin supplies their PIN (still
    mathematically required to derive the AES vault key) AND a one-time
    recovery code (standing in for the missing TOTP). On success the
    vault unlocks for the session, the consumed code is marked used,
    TOTP enrolment is cleared, and the admin is sent straight to
    /totp-setup/ to enrol a fresh authenticator.
    """
    from .recovery import consume_recovery_code

    config = VaultConfig.get()
    if not config.pin_set:
        return redirect('vault:home')
    if not config.totp_configured:
        # If there's nothing to recover from, there's nothing to do here.
        return redirect('vault:home')

    error = None
    if request.method == 'POST':
        pin = ''.join(request.POST.get(f'd{i}', '') for i in range(1, 5))
        if not pin:
            pin = (request.POST.get('pin') or '').strip()
        code = (request.POST.get('recovery_code') or '').strip()
        salt = bytes(config.encryption_salt)

        if not verify_pin(pin, config.pin_hash, salt):
            error = 'Incorrect PIN.'
            _log('pin_failed', request,
                 note='Recovery-code attempt with wrong PIN.')
        elif not consume_recovery_code(config, code):
            error = 'That recovery code is not valid (or already used).'
            _log('pin_failed', request,
                 note='Recovery-code attempt with wrong/used code.')
        else:
            # PIN good and a fresh recovery code was consumed. Unlock the
            # vault, then wipe the old TOTP enrolment so the next page
            # forces a new QR scan.
            key = derive_key(pin, salt)
            _unlock_session(request, key)
            request.session['vault_was_unlocked'] = True
            request.session['vault_totp_verified'] = True
            config.totp_secret_encrypted = ''
            config.totp_configured = False
            config.save(update_fields=[
                'totp_secret_encrypted', 'totp_configured', 'updated_at'])
            _log('ssh_totp_setup', request,
                 note='Vault recovered via recovery code — TOTP cleared.')
            return redirect('vault:totp_setup')

    return render(request, 'vault/recover.html', {
        'active': 'vault',
        'error': error,
    })


@admin_required
def totp_reset(request):
    """
    Reset the authenticator from inside an already-unlocked vault — for
    when the admin is switching phones / apps and still has access. One
    recovery code is consumed; the existing TOTP enrolment is cleared so
    /totp-setup/ can re-issue a fresh QR.
    """
    from .recovery import consume_recovery_code, remaining_count

    if get_vault_key(request) is None:
        return redirect(f"{reverse('vault:home')}?next={request.path}")
    config = VaultConfig.get()

    error = None
    if request.method == 'POST':
        code = (request.POST.get('recovery_code') or '').strip()
        if not consume_recovery_code(config, code):
            error = 'That recovery code is not valid (or already used).'
            _log('pin_failed', request,
                 note='TOTP-reset attempt with wrong/used recovery code.')
        else:
            config.totp_secret_encrypted = ''
            config.totp_configured = False
            config.save(update_fields=[
                'totp_secret_encrypted', 'totp_configured', 'updated_at'])
            # Drop the "already verified this session" flag so the new
            # TOTP enrolment is genuinely required before SSH unlocks.
            request.session.pop('vault_totp_verified', None)
            _log('ssh_totp_setup', request,
                 note='TOTP reset via recovery code — new enrolment required.')
            return redirect('vault:totp_setup')

    return render(request, 'vault/totp_reset.html', {
        'active': 'vault',
        'error': error,
        'remaining_codes': remaining_count(config),
        'seconds_remaining': _seconds_remaining(request),
    })


@admin_required
def vault_settings(request):
    """
    Vault settings — currently just recovery-code status + a button to
    regenerate them. Regeneration requires a current TOTP code so a
    drive-by session can't invalidate the admin's backup access.
    """
    from .recovery import (
        generate_recovery_codes, remaining_count, store_recovery_codes,
    )
    from .totp_helpers import get_decrypted_totp_secret, verify_totp_code

    key = get_vault_key(request)
    if key is None:
        return redirect(f"{reverse('vault:home')}?next={request.path}")
    config = VaultConfig.get()

    error = None
    new_codes = None  # only set on a successful regen — shown once
    if request.method == 'POST' and request.POST.get('action') == 'regenerate':
        if not config.totp_configured:
            error = ('Set up TOTP before generating recovery codes.')
        else:
            code = (request.POST.get('totp_code') or '').strip()
            secret = get_decrypted_totp_secret(config, key)
            if not secret or not verify_totp_code(secret, code):
                error = 'Incorrect authenticator code.'
                _log('pin_failed', request,
                     note='Recovery-code regen blocked by wrong TOTP.')
            else:
                new_codes = generate_recovery_codes()
                store_recovery_codes(config, new_codes)
                config.save(update_fields=['recovery_codes', 'updated_at'])
                _log('ssh_totp_setup', request,
                     note='Recovery codes regenerated — old codes invalidated.')

    return render(request, 'vault/settings.html', {
        'active': 'vault',
        'error': error,
        'totp_configured': config.totp_configured,
        'remaining_codes': remaining_count(config),
        'total_codes': len(config.recovery_codes or []),
        'new_codes': new_codes,
        'seconds_remaining': _seconds_remaining(request),
    })


def _vault_session_authenticated(request, config):
    """Vault is unlocked AND (if required) TOTP-verified this session."""
    if get_vault_key(request) is None:
        return False
    if config.totp_configured and not request.session.get(
            'vault_totp_verified'):
        return False
    return True


@admin_required
def terminal(request, cred_id):
    """
    Browser SSH terminal. Vault PIN session + vault-level TOTP must both
    be valid; there is no per-server TOTP step any more. Locked vault
    redirects back through `enter_pin` with ?next= so unlock comes
    straight here.
    """
    config = VaultConfig.get()
    if not _vault_session_authenticated(request, config):
        return redirect(
            f"{reverse('vault:home')}?next={request.path}")

    key = get_vault_key(request)
    cred = get_object_or_404(
        VaultCredential, id=cred_id, is_ssh_credential=True)

    return render(request, 'vault/terminal.html', {
        'credential': cred,
        'host_hint': _host_hint(cred, key),
        'command_groups': _command_groups(cred),
        # The vault session timer is the only clock that matters now;
        # the old per-server 15-minute TOTP window is gone.
        'totp_remaining_seconds': _seconds_remaining(request),
    })


@admin_required
def command_library(request, cred_id):
    """Manage the saved command library for an SSH credential."""
    key = get_vault_key(request)
    if key is None:
        return redirect(f"{reverse('vault:home')}?next={request.path}")
    cred = get_object_or_404(
        VaultCredential, id=cred_id, is_ssh_credential=True)

    if request.method == 'POST':
        if request.POST.get('action') == 'delete':
            ServerCommandLibrary.objects.filter(
                id=request.POST.get('command_id'), credential=cred).delete()
            return redirect('vault:command_library', cred_id=cred.id)
        form = CommandForm(request.POST)
        if form.is_valid():
            command = form.save(commit=False)
            command.credential = cred
            command.save()
            return redirect('vault:command_library', cred_id=cred.id)
    else:
        form = CommandForm()

    return render(request, 'vault/command_library.html', {
        'active': 'vault',
        'client': cred.vault.client,
        'credential': cred,
        'form': form,
        'command_groups': _command_groups(cred),
        'seconds_remaining': _seconds_remaining(request),
    })


@admin_required
def command_row(request, cred_id, cmd_id):
    """HTMX — render one command's display row (used for edit-cancel)."""
    cred = get_object_or_404(
        VaultCredential, id=cred_id, is_ssh_credential=True)
    cmd = get_object_or_404(ServerCommandLibrary, id=cmd_id, credential=cred)
    return render(request, 'vault/_command_row.html',
                  {'credential': cred, 'cmd': cmd})


@admin_required
def command_edit(request, cred_id, cmd_id):
    """HTMX inline edit — GET returns the form, POST saves and returns the row."""
    cred = get_object_or_404(
        VaultCredential, id=cred_id, is_ssh_credential=True)
    cmd = get_object_or_404(ServerCommandLibrary, id=cmd_id, credential=cred)

    if request.method == 'POST':
        form = CommandForm(request.POST, instance=cmd)
        if form.is_valid():
            form.save()
            return render(request, 'vault/_command_row.html',
                          {'credential': cred, 'cmd': cmd})
    else:
        form = CommandForm(instance=cmd)

    return render(request, 'vault/_command_form.html',
                  {'credential': cred, 'cmd': cmd, 'form': form})


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6d — AI Ops Agent
# ─────────────────────────────────────────────────────────────────────────────
#
# Architecture in one screen:
#
#   ops_agent      GET — renders the split-panel page; auto-creates an
#                  OpsSession on first hit and stores its id on the
#                  Django session keyed by credential.
#
#   ops_chat       POST {message, session_id} — one Claude turn.
#                  Calls the Anthropic SDK directly (not via
#                  reporting.ai.claude_complete) so we can read
#                  response.usage and stamp the real token count on
#                  the OpsSession. Model is Sonnet — better
#                  log-reading + multi-step diagnosis is worth the
#                  cost over Haiku for this workload. Extracts any
#                  commands from the reply, classifies each via
#                  ops_safety, and returns {agent_message,
#                  safe_commands, dangerous_commands, tokens_used}.
#                  Safe commands are auto-executed client-side;
#                  dangerous ones surface a safety-gate card.
#
#   ops_execute    POST {command, session_id, approved_by_human} —
#                  one paramiko round trip. Refuses dangerous commands
#                  without explicit approval (defence-in-depth: the
#                  classification runs on BOTH sides).
#
#   ops_deny       POST {command, session_id} — records the denial,
#                  inserts a system-flagged user message into the
#                  conversation, and lets the JS poll /chat/ for the
#                  agent's alternative.
#
#   ops_end_session POST {session_id} — stamps ended_at / duration
#                   and clears the per-credential session pointer.
#
#   ops_sessions_list / ops_session_replay — read-only audit log.

@admin_required
def ops_agent(request, cred_id):
    """
    Render the AI Ops Agent page. Requires vault unlocked + TOTP
    verified; auto-creates an OpsSession on first hit (one per
    credential per Django session). The server context snapshot is
    taken right here so the conversation reflects what the box
    actually looks like at session start.
    """
    from .models import OpsSession
    from .ops_context import get_server_context

    config = VaultConfig.get()
    if not _vault_session_authenticated(request, config):
        return redirect(
            f"{reverse('vault:home')}?next={request.path}")

    cred = get_object_or_404(
        VaultCredential, id=cred_id, is_ssh_credential=True)
    vault_key = get_vault_key(request)

    sess_key = f'ops_session_{cred_id}'
    ops_session = None
    sid = request.session.get(sess_key)
    if sid:
        ops_session = OpsSession.objects.filter(
            id=sid, ended_at__isnull=True).first()

    if ops_session is None:
        context_snapshot = get_server_context(cred, vault_key)
        ops_session = OpsSession.objects.create(
            credential=cred,
            client=getattr(cred.vault, 'client', None),
            context_snapshot=context_snapshot,
        )
        request.session[sess_key] = str(ops_session.id)
        _log('ssh_connected', request,
             client_name=(cred.vault.client.firm_name
                          if cred.vault.client else ''),
             credential_label=cred.label,
             note=f'AI Ops session started: {ops_session.id}')

    return render(request, 'vault/ops_agent.html', {
        'credential': cred,
        'ops_session': ops_session,
        'client': getattr(cred.vault, 'client', None),
        'context_snapshot': ops_session.context_snapshot,
        'host_hint': _host_hint(cred, vault_key),
        'command_groups': _command_groups(cred),
        'recent_sessions': (
            OpsSession.objects.filter(credential=cred)
            .exclude(id=ops_session.id)
            .order_by('-started_at')[:3]),
    })


# ── HTTP helpers shared by the four POST endpoints ─────────────────────────

def _ops_json_loads(request):
    """Parse a JSON POST body; return {} on empty / bad input."""
    import json
    try:
        return json.loads(request.body or b'{}')
    except (json.JSONDecodeError, TypeError):
        return {}


def _ops_session_for(request, cred):
    """
    Look up the active OpsSession id from the Django session, with
    the request body session_id as a fallback. The body lookup matters
    when the page was reloaded but the JS still holds the old id.
    """
    from .models import OpsSession

    data = _ops_json_loads(request)
    sid = data.get('session_id') or request.session.get(
        f'ops_session_{cred.id}')
    if not sid:
        return None, data
    return OpsSession.objects.filter(id=sid).first(), data


@admin_required
@require_POST
def ops_chat(request, cred_id):
    """
    One Claude turn for the AI Ops Agent.
    Body: message, session_id.
    Returns: agent_message, safe_commands, dangerous_commands,
             tokens_used, session_id.
    """
    from django.http import JsonResponse
    from django.utils import timezone as dj_timezone

    from django.conf import settings
    from .ops_context import build_system_prompt
    from .ops_safety import (
        check_command_safety, extract_commands_from_response,
    )

    # Sonnet for the ops agent — log-reading + multi-step diagnosis
    # benefits enough from the bigger model to justify the cost over
    # Haiku. Using `claude-sonnet-4-6` (the current Sonnet per
    # CLAUDE.md / env) rather than the older claude-sonnet-4-20250514
    # snapshot in case future Anthropic deprecations bite the dated id.
    OPS_AGENT_MODEL = 'claude-sonnet-4-6'

    config = VaultConfig.get()
    if not _vault_session_authenticated(request, config):
        return JsonResponse(
            {'error': 'Vault session expired — please re-unlock.'},
            status=401)

    cred = get_object_or_404(
        VaultCredential, id=cred_id, is_ssh_credential=True)
    ops_session, data = _ops_session_for(request, cred)
    if ops_session is None:
        return JsonResponse(
            {'error': 'No active session.'}, status=400)

    user_message = (data.get('message') or '').strip()
    if not user_message:
        return JsonResponse(
            {'error': 'Empty message.'}, status=400)

    now_iso = dj_timezone.now().isoformat()
    conversation = list(ops_session.conversation or [])
    conversation.append({
        'role': 'user',
        'content': user_message,
        'timestamp': now_iso,
    })

    scan_summary = None
    if ops_session.client_id:
        try:
            latest_scan = (
                ops_session.client.vulnerability_scans
                .filter(status='complete')
                .order_by('-completed_at').first()
            )
            if latest_scan:
                scan_summary = (
                    f"Last scan: {latest_scan.completed_at.date()} — "
                    f"Critical: {latest_scan.critical_count}, "
                    f"High: {latest_scan.high_count}, "
                    f"Medium: {latest_scan.medium_count}"
                )
        except Exception:
            pass
    system_prompt = build_system_prompt(
        credential=cred, client=ops_session.client,
        context=ops_session.context_snapshot or {},
        scan_summary=scan_summary,
    )

    api_messages = [
        {'role': m['role'], 'content': m['content']}
        for m in conversation[-50:]
    ]
    # Direct SDK call (not via reporting.ai.claude_complete) so we can
    # read response.usage and increment OpsSession.total_tokens_used
    # with the real number rather than a sentinel zero.
    if not settings.ANTHROPIC_API_KEY:
        return JsonResponse({
            'error': 'ANTHROPIC_API_KEY is not configured on the server.',
        }, status=500)
    try:
        from anthropic import Anthropic
        sdk_client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = sdk_client.messages.create(
            model=OPS_AGENT_MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=api_messages,
        )
        agent_message = response.content[0].text.strip()
        usage = getattr(response, 'usage', None)
        tokens_used = (
            (getattr(usage, 'input_tokens', 0) or 0) +
            (getattr(usage, 'output_tokens', 0) or 0)
        ) if usage else 0
    except Exception as exc:  # noqa: BLE001 — surface every API failure uniformly
        logger.exception('Ops chat — Claude API call failed')
        return JsonResponse({
            'error': f'Claude API error: {exc}',
        }, status=500)

    conversation.append({
        'role': 'assistant',
        'content': agent_message,
        'timestamp': dj_timezone.now().isoformat(),
    })

    commands = extract_commands_from_response(agent_message)
    safe_commands, dangerous_commands = [], []
    for cmd in commands:
        safety = check_command_safety(cmd)
        if safety['is_dangerous']:
            dangerous_commands.append({
                'command': cmd,
                'reason': safety['reason'],
            })
        else:
            safe_commands.append(cmd)

    ops_session.conversation = conversation
    ops_session.total_tokens_used = (
        (ops_session.total_tokens_used or 0) + tokens_used)
    ops_session.save(update_fields=[
        'conversation', 'total_tokens_used', 'updated_at'])

    return JsonResponse({
        'agent_message': agent_message,
        'safe_commands': safe_commands,
        'dangerous_commands': dangerous_commands,
        'tokens_used': tokens_used,
        'session_id': str(ops_session.id),
    })


@admin_required
@require_POST
def ops_execute(request, cred_id):
    """
    Run one shell command over SSH and log it to the OpsSession.

    Body: command, session_id, approved_by_human?.
    Returns: output, exit_code, command, was_dangerous, approved.

    Defence-in-depth: re-classifies safety here, even if the JS posts
    approved_by_human=True for something that shouldn't be flagged.
    """
    from django.http import JsonResponse
    from django.utils import timezone as dj_timezone

    from .ops_context import open_ssh_for_credential
    from .ops_safety import check_command_safety

    config = VaultConfig.get()
    if not _vault_session_authenticated(request, config):
        return JsonResponse(
            {'error': 'Vault session expired — please re-unlock.'},
            status=401)

    cred = get_object_or_404(
        VaultCredential, id=cred_id, is_ssh_credential=True)
    ops_session, data = _ops_session_for(request, cred)
    if ops_session is None:
        return JsonResponse(
            {'error': 'No active session.'}, status=400)

    command = (data.get('command') or '').strip()
    if not command:
        return JsonResponse(
            {'error': 'No command provided.'}, status=400)
    approved_by_human = bool(data.get('approved_by_human'))

    safety = check_command_safety(command)
    if safety['is_dangerous'] and not approved_by_human:
        return JsonResponse({
            'error': 'dangerous_command',
            'reason': safety['reason'],
            'command': command,
        }, status=400)

    vault_key = get_vault_key(request)
    full_output, exit_code = '', -1
    try:
        ssh = open_ssh_for_credential(cred, vault_key)
        try:
            _, stdout, stderr = ssh.exec_command(command, timeout=60)
            out = stdout.read().decode('utf-8', errors='replace')
            err = stderr.read().decode('utf-8', errors='replace')
            exit_code = stdout.channel.recv_exit_status()
            full_output = out
            if err:
                full_output += f'\n[stderr]: {err}'
        finally:
            try:
                ssh.close()
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001
        full_output = f'SSH Error: {exc}'
        exit_code = -1

    truncated = full_output[:5000]
    if len(full_output) > 5000:
        truncated += '\n[…output truncated…]'

    command_record = {
        'command': command,
        'output': truncated,
        'exit_code': exit_code,
        'timestamp': dj_timezone.now().isoformat(),
        'was_dangerous': safety['is_dangerous'],
        'approved_by_human': approved_by_human,
        'denied_by_human': False,
    }
    cmds = list(ops_session.commands_executed or [])
    cmds.append(command_record)
    ops_session.commands_executed = cmds

    if safety['is_dangerous'] and approved_by_human:
        approved = list(ops_session.dangerous_commands_approved or [])
        approved.append({
            'command': command,
            'reason': safety['reason'],
            'timestamp': dj_timezone.now().isoformat(),
        })
        ops_session.dangerous_commands_approved = approved

    ops_session.save(update_fields=[
        'commands_executed', 'dangerous_commands_approved',
        'updated_at'])

    return JsonResponse({
        'output': full_output,
        'exit_code': exit_code,
        'command': command,
        'was_dangerous': safety['is_dangerous'],
        'approved': approved_by_human,
    })


@admin_required
@require_POST
def ops_deny(request, cred_id):
    """
    Record a human denial of a dangerous command. Adds a system-
    flagged user message to the conversation so the next /chat/ call
    forces the agent to either justify or work around it.
    """
    from django.http import JsonResponse
    from django.utils import timezone as dj_timezone

    cred = get_object_or_404(
        VaultCredential, id=cred_id, is_ssh_credential=True)
    ops_session, data = _ops_session_for(request, cred)
    if ops_session is None:
        return JsonResponse(
            {'error': 'No active session.'}, status=400)

    command = (data.get('command') or '').strip()
    if not command:
        return JsonResponse(
            {'error': 'No command provided.'}, status=400)

    now_iso = dj_timezone.now().isoformat()
    denied = list(ops_session.dangerous_commands_denied or [])
    denied.append({'command': command, 'timestamp': now_iso})
    ops_session.dangerous_commands_denied = denied

    conv = list(ops_session.conversation or [])
    conv.append({
        'role': 'user',
        'content': (
            f'I denied running this command: `{command}`. Please find '
            f'an alternative approach that does not require this '
            f'command, or explain why it is necessary.'),
        'timestamp': now_iso,
        'is_system': True,
    })
    ops_session.conversation = conv
    ops_session.save(update_fields=[
        'dangerous_commands_denied', 'conversation', 'updated_at'])
    return JsonResponse({'denied': True})


@admin_required
@require_POST
def ops_end_session(request, cred_id):
    """Close out the active OpsSession + clear the session pointer."""
    from django.http import JsonResponse
    from django.utils import timezone as dj_timezone

    cred = get_object_or_404(
        VaultCredential, id=cred_id, is_ssh_credential=True)
    ops_session, _data = _ops_session_for(request, cred)
    if ops_session is None:
        return JsonResponse({'ended': True})

    if ops_session.ended_at is None:
        ops_session.ended_at = dj_timezone.now()
        ops_session.duration_seconds = int(
            (ops_session.ended_at - ops_session.started_at)
            .total_seconds())
        ops_session.save(update_fields=[
            'ended_at', 'duration_seconds', 'updated_at'])

    request.session.pop(f'ops_session_{cred.id}', None)
    return JsonResponse({'ended': True})


@admin_required
def ops_sessions_list(request):
    """Read-only list of every AI Ops session, newest first."""
    from .models import OpsSession
    sessions = (OpsSession.objects
                .select_related('credential', 'client')
                .order_by('-started_at')[:200])
    return render(request, 'vault/ops_sessions_list.html', {
        'active': 'ops', 'sessions': sessions,
    })


@admin_required
def ops_session_replay(request, session_id):
    """
    Read-only replay of one session — every message, every command,
    every approve/deny in time order.
    """
    from .models import OpsSession
    sess = get_object_or_404(
        OpsSession.objects.select_related('credential', 'client'),
        id=session_id,
    )
    timeline = []
    for m in (sess.conversation or []):
        timeline.append({
            'kind': 'message',
            'role': m.get('role', 'user'),
            'content': m.get('content', ''),
            'timestamp': m.get('timestamp', ''),
            'is_system': bool(m.get('is_system')),
        })
    for c in (sess.commands_executed or []):
        timeline.append({
            'kind': 'command',
            'command': c.get('command', ''),
            'output': c.get('output', ''),
            'exit_code': c.get('exit_code'),
            'timestamp': c.get('timestamp', ''),
            'was_dangerous': bool(c.get('was_dangerous')),
            'approved_by_human': bool(c.get('approved_by_human')),
        })
    timeline.sort(key=lambda x: x.get('timestamp', ''))

    return render(request, 'vault/ops_session_replay.html', {
        'active': 'ops', 'sess': sess, 'timeline': timeline,
    })
