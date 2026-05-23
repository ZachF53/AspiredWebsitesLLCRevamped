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
        for cred in vault.credentials.filter(category=cat_key):
            items.append({
                'id': cred.id,
                'label': cred.label,
                'username_hint': cred.username_hint or '—',
                'url': decrypt_value(cred.url_encrypted, key),
                'notes': decrypt_value(cred.notes_encrypted, key),
                'has_password': bool(cred.password_encrypted),
                'visible_to_client': cred.visible_to_client,
                'is_ssh_credential': cred.is_ssh_credential,
            })
        if items:
            groups.append({'key': cat_key, 'label': cat_label, 'items': items})

    return render(request, 'vault/client_vault.html', {
        'active': 'vault',
        'client': client,
        'vault': vault,
        'groups': groups,
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
