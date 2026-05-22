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
    unwrap_key,
    verify_pin,
    wrap_key,
)
from .forms import CredentialForm
from .models import ClientVault, VaultAccessLog, VaultConfig, VaultCredential

logger = logging.getLogger(__name__)

SESSION_HOURS = 1
MAX_ATTEMPTS = 5
LOCKOUT_MINUTES = 30

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

    # PIN entry.
    if request.method == 'POST':
        return _handle_pin_entry(request, config)

    # Already unlocked?
    if get_vault_key(request) is not None:
        return _render_vault_home(request)

    return render(request, 'vault/enter_pin.html', {
        'active': 'vault',
        'expired': bool(request.session.get('vault_was_unlocked')),
        'next': request.GET.get('next', ''),
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
    return redirect('vault:home')


def _handle_pin_entry(request, config):
    now = timezone.now()
    pin = ''.join(request.POST.get(f'd{i}', '') for i in range(1, 5)).strip()
    if not pin:
        pin = (request.POST.get('pin') or '').strip()

    salt = bytes(config.encryption_salt)
    if verify_pin(pin, config.pin_hash, salt):
        config.failed_attempts = 0
        config.lockout_until = None
        config.save()
        key = derive_key(pin, salt)
        _unlock_session(request, key)
        request.session['vault_was_unlocked'] = True
        _log('pin_verified', request, note='Vault unlocked.')
        next_url = request.POST.get('next') or request.GET.get('next') or ''
        if next_url.startswith('/admin-dashboard/vault/'):
            return redirect(next_url)
        return redirect('vault:home')

    # Wrong PIN.
    config.failed_attempts += 1
    if config.failed_attempts >= MAX_ATTEMPTS:
        config.lockout_until = now + timedelta(minutes=LOCKOUT_MINUTES)
        config.failed_attempts = 0
        config.save()
        _log('pin_locked', request,
             note=f'Locked {LOCKOUT_MINUTES} min after {MAX_ATTEMPTS} failures.')
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
        'error': f'Incorrect PIN — {remaining} attempt'
                 f'{"" if remaining == 1 else "s"} remaining before a '
                 f'{LOCKOUT_MINUTES}-minute lockout.',
        'next': request.POST.get('next', ''),
    })


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
            })
        if items:
            groups.append({'key': cat_key, 'label': cat_label, 'items': items})

    return render(request, 'vault/client_vault.html', {
        'active': 'vault',
        'client': client,
        'vault': vault,
        'groups': groups,
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
            cred.save()
            _log('credential_updated', request,
                 client_name=client.firm_name, credential_label=cred.label)
            return redirect('vault:client_vault', client_id=client.id)
    else:
        form = CredentialForm(initial={
            'label': cred.label,
            'category': cred.category,
            'sort_order': cred.sort_order,
            'visible_to_client': cred.visible_to_client,
        })

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
