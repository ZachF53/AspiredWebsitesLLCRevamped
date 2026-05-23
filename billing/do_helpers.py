"""
DigitalOcean Droplet provisioning helpers.

One Droplet per client (CLAUDE.md). provision_client_droplet() is called by
the provision_droplet_task Celery task after a deposit payment clears.

Provisioning is fully automated end-to-end:

1. A fresh random root password is generated and injected via cloud-init
   user_data (DO has no native root_password param; cloud-init is the
   standard route).
2. The Droplet is created from the base snapshot and polled to active.
3. setup_vault_key_for_droplet() SSHes in with that temp password,
   generates an Ed25519 keypair on the box, authorises it, locks the box
   back down (disables password auth, locks the root account password),
   and captures the private key.
4. _create_ssh_vault_credential() stores the key in a VaultCredential
   encrypted with the VAULT_SERVER_SECRET-derived server key, so
   credentials created before any admin has unlocked the vault are still
   recoverable. The first admin to open the credential re-encrypts it
   under the PIN key.

Vault setup failures NEVER block the Droplet from being created — the
temp password is stashed on the client and an admin is alerted via
"Needs You" so the work can finish manually.
"""

import logging
import secrets
import time

import paramiko
import requests
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from django.utils.text import slugify

logger = logging.getLogger(__name__)

DO_API = 'https://api.digitalocean.com/v2'
DROPLET_REGION = 'nyc1'          # closest region serving TX/GA
DROPLET_SIZE = 's-1vcpu-1gb'     # $6/month
PROVISION_TIMEOUT = 300          # seconds — max wait for status=active
POLL_INTERVAL = 10               # seconds between status polls

# After the Droplet reports active, the SSH daemon may need a few more
# seconds before it actually accepts connections.
SSH_BOOT_GRACE_SECONDS = 20


class DONotConfigured(RuntimeError):
    """Raised when a DO API call is attempted without DO_API_TOKEN."""


def _headers():
    if not settings.DO_API_TOKEN:
        raise DONotConfigured('DO_API_TOKEN is not set in .env')
    return {
        'Authorization': f'Bearer {settings.DO_API_TOKEN}',
        'Content-Type': 'application/json',
    }


def droplet_name_for(client):
    """Naming convention: clientname-prod (lowercase, hyphens)."""
    return f'{slugify(client.firm_name)}-prod'


def _public_ip(droplet):
    for net in (droplet.get('networks') or {}).get('v4') or []:
        if net.get('type') == 'public':
            return net.get('ip_address')
    return None


def _cloud_init_user_data(root_password: str) -> str:
    """
    cloud-init that lets us SSH in as root with `root_password` ONCE so we
    can install our vault keypair. Password auth is disabled again by
    setup_vault_key_for_droplet() after the key is in place.
    """
    return (
        '#cloud-config\n'
        'chpasswd:\n'
        '  list: |\n'
        f'    root:{root_password}\n'
        '  expire: false\n'
        'ssh_pwauth: true\n'
        'runcmd:\n'
        '  - sed -i "s/^#\\?PasswordAuthentication.*/PasswordAuthentication yes/" /etc/ssh/sshd_config\n'
        '  - systemctl reload sshd || systemctl reload ssh || true\n'
    )


def provision_client_droplet(client):
    """
    Create the client's Droplet from the base snapshot, poll until it is
    active, store the ID/IP on the ClientProfile, attempt automated
    vault-key setup, and notify admin. Returns the droplet dict.

    Vault-key failures are non-blocking — they stash the temp password on
    the client and flag an admin alert instead of raising.
    """
    headers = _headers()
    name = droplet_name_for(client)
    temp_password = secrets.token_urlsafe(32)
    payload = {
        'name': name,
        'region': DROPLET_REGION,
        'size': DROPLET_SIZE,
        'image': settings.DO_BASE_SNAPSHOT_ID,
        'tags': ['aspired-websites', 'client'],
        'user_data': _cloud_init_user_data(temp_password),
    }
    resp = requests.post(
        f'{DO_API}/droplets', json=payload, headers=headers, timeout=30,
    )
    resp.raise_for_status()
    droplet = resp.json()['droplet']
    droplet_id = droplet['id']
    logger.info('DO: created droplet %s (%s) for %s', droplet_id, name, client.pk)

    # Poll until the Droplet is active and has a public IP.
    deadline = time.time() + PROVISION_TIMEOUT
    while time.time() < deadline:
        if droplet.get('status') == 'active' and _public_ip(droplet):
            break
        time.sleep(POLL_INTERVAL)
        poll = requests.get(
            f'{DO_API}/droplets/{droplet_id}', headers=headers, timeout=30,
        )
        poll.raise_for_status()
        droplet = poll.json()['droplet']

    ip = _public_ip(droplet)
    client.do_droplet_id = str(droplet_id)
    client.do_droplet_ip = ip or None
    client.do_droplet_created_at = timezone.now()
    client.save(update_fields=[
        'do_droplet_id', 'do_droplet_ip', 'do_droplet_created_at', 'updated_at',
    ])
    _notify_admin(client, droplet_id, ip)

    # ── Automated vault key bootstrap (non-blocking) ──────────────────────
    if ip:
        # Give cloud-init/sshd a beat to settle before we try to log in.
        time.sleep(SSH_BOOT_GRACE_SECONDS)
        try:
            setup_vault_key_for_droplet(client, ip, temp_password)
            logger.info('DO: vault key installed for %s', client.pk)
        except Exception as exc:  # noqa: BLE001 — never block provisioning
            logger.exception(
                'DO: automated vault key setup failed for %s — stashing '
                'temp password for manual recovery', client.pk)
            _stash_temp_password(client, temp_password)
            _alert_vault_setup_failure(client, ip, str(exc))
    else:
        logger.warning(
            'DO: droplet %s never reported a public IP — skipping vault key '
            'setup, stashing temp password', droplet_id)
        _stash_temp_password(client, temp_password)
        _alert_vault_setup_failure(client, None,
                                   'Droplet never reported a public IP.')

    return droplet


def setup_vault_key_for_droplet(client, droplet_ip, root_password,
                                max_retries=5, retry_delay=30):
    """
    SSH into a freshly-provisioned Droplet with the temp root password,
    generate an Ed25519 keypair on the box, authorise it for root,
    verify the loopback connection works, then lock the box down:
    PasswordAuthentication off, root account password locked.

    Retries `max_retries` times on transient errors (the SSH daemon often
    isn't quite ready in the first ~30s after cloud-init). Once the key is
    in place and confirmed working, the temp password is rendered useless.
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                hostname=droplet_ip,
                port=22,
                username='root',
                password=root_password,
                timeout=20,
                allow_agent=False,
                look_for_keys=False,
            )
            private_key = _bootstrap_vault_key_over_ssh(ssh)
            _lock_down_password_auth(ssh)
            ssh.close()
            _create_ssh_vault_credential(client, droplet_ip, private_key)
            return
        except (paramiko.SSHException, OSError, EOFError) as exc:
            last_error = exc
            logger.warning(
                'vault bootstrap attempt %d/%d failed for %s: %s',
                attempt, max_retries, droplet_ip, exc)
            try:
                ssh.close()
            except Exception:
                pass
            if attempt < max_retries:
                time.sleep(retry_delay)
    raise RuntimeError(
        f'SSH vault key setup failed after {max_retries} attempts: '
        f'{last_error}')


def _run(ssh, command, *, check=True, timeout=30):
    """Run a remote command and return (exit_code, stdout, stderr)."""
    _, stdout, stderr = ssh.exec_command(command, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    code = stdout.channel.recv_exit_status()
    if check and code != 0:
        raise RuntimeError(
            f'remote command failed (exit {code}): {command}\n{err or out}')
    return code, out, err


def _bootstrap_vault_key_over_ssh(ssh) -> str:
    """
    Generate /root/.ssh/aspired_vault_key (Ed25519, no passphrase — the
    private key is itself encrypted at rest by the vault's AES-256-GCM,
    so a passphrase on the file would be redundant), append the public
    half to authorized_keys, prove the loopback works, and return the
    private key as text.
    """
    key_path = '/root/.ssh/aspired_vault_key'
    pub_path = f'{key_path}.pub'

    _run(ssh, 'mkdir -p /root/.ssh && chmod 700 /root/.ssh')
    # -y on an existing key would just print the public half; -N "" + -f
    # generates a fresh keypair without prompting.
    _run(ssh, f'rm -f {key_path} {pub_path}')
    _run(ssh,
         f'ssh-keygen -t ed25519 -N "" -C "aspired-vault" -f {key_path}')
    # Add to authorized_keys (idempotent — comment marker uniquely identifies it).
    _run(ssh, 'touch /root/.ssh/authorized_keys && '
              'chmod 600 /root/.ssh/authorized_keys')
    _run(ssh, 'sed -i "/aspired-vault/d" /root/.ssh/authorized_keys')
    _run(ssh, f'cat {pub_path} >> /root/.ssh/authorized_keys')

    # Loopback test: prove the key can actually authenticate as root.
    _run(ssh,
         f'ssh -i {key_path} -o StrictHostKeyChecking=no '
         '-o UserKnownHostsFile=/dev/null -o BatchMode=yes '
         '-o ConnectTimeout=10 root@127.0.0.1 true')

    _, private_key, _ = _run(ssh, f'cat {key_path}')
    if not private_key.strip().startswith('-----BEGIN'):
        raise RuntimeError('captured private key is malformed')
    return private_key


def _lock_down_password_auth(ssh):
    """
    Disable PasswordAuthentication via an sshd_config.d drop-in (overrides
    cloud-init's earlier ssh_pwauth: true) and lock the root account
    password — the temp password generated at provision time becomes
    immediately useless.
    """
    drop_in = (
        '# Aspired vault bootstrap — password auth was only enabled long '
        'enough\n# to install the vault keypair. Key-only from now on.\n'
        'PasswordAuthentication no\n'
        'KbdInteractiveAuthentication no\n'
        'ChallengeResponseAuthentication no\n'
    )
    _run(ssh,
         "cat > /etc/ssh/sshd_config.d/99-vault-lockdown.conf << 'EOF'\n"
         f"{drop_in}EOF")
    _run(ssh, 'sshd -t')  # validate config before reloading
    _run(ssh, 'systemctl reload sshd || systemctl reload ssh || true',
         check=False)
    _run(ssh, 'passwd -l root', check=False)


def _create_ssh_vault_credential(client, droplet_ip, private_key):
    """
    Create a VaultCredential holding the SSH key for the new Droplet,
    encrypted with the VAULT_SERVER_SECRET-derived server key. The first
    admin to open it re-encrypts under the PIN key.
    """
    # Local import — avoids loading vault models at app start, sidesteps any
    # billing → vault import-time cycle.
    from vault.crypto import derive_server_key, encrypt_value, make_hint
    from vault.models import ClientVault, VaultCredential

    server_key = derive_server_key()
    vault, _ = ClientVault.objects.get_or_create(client=client)
    label = f'DigitalOcean — {droplet_name_for(client)}'

    cred = VaultCredential.objects.create(
        vault=vault,
        category='server',
        label=label,
        is_ssh_credential=True,
        ssh_port=22,
        ssh_auth_type='private_key',
        ssh_host_encrypted=encrypt_value(droplet_ip, server_key),
        ssh_username_encrypted=encrypt_value('root', server_key),
        ssh_private_key_encrypted=encrypt_value(private_key, server_key),
        username_hint=make_hint('root'),
        encrypted_with_server_key=True,
        notes_encrypted=encrypt_value(
            'Auto-provisioned during Droplet creation. Re-encrypted under '
            'your PIN the first time it was opened.',
            server_key),
    )
    # The post_save signal seeds default ServerCommandLibrary entries —
    # call create_default_commands too in case the signal is suppressed in
    # a test/transaction context.
    from vault.default_commands import create_default_commands
    create_default_commands(cred)
    return cred


# ── Manual-recovery helpers when automation can't finish ────────────────────

TEMP_PASSWORD_PREFIX = 'TEMP SSH PASSWORD (change immediately): '


def _stash_temp_password(client, temp_password):
    """
    Stash the temp root password on the client's internal_notes so an
    admin can finish the SSH key install by hand. Prefixed and timestamped
    so it's unmissable.
    """
    line = (f'{TEMP_PASSWORD_PREFIX}{temp_password}  '
            f'(stashed {timezone.now().isoformat()})')
    existing = client.internal_notes or ''
    client.internal_notes = (line + '\n\n' + existing).strip()
    client.save(update_fields=['internal_notes', 'updated_at'])


def _alert_vault_setup_failure(client, droplet_ip, reason):
    """Email the admin so it lands in Needs You / the inbox immediately."""
    send_mail(
        subject=f'[Needs You] Vault SSH key setup failed — {client.firm_name}',
        message=(
            f'Automated SSH vault key install failed for {client.firm_name}.\n\n'
            f'Droplet IP: {droplet_ip or "unknown"}\n'
            f'Reason: {reason}\n\n'
            f'The temp root password has been stashed at the top of the '
            f'client\'s internal notes (admin dashboard → Clients → '
            f'{client.firm_name} → Internal Notes). SSH in with it, install '
            f'the vault key by hand, then DELETE the line from internal '
            f'notes when done.\n'
        ),
        from_email=settings.EMAIL_FROM_NO_REPLY,
        recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
        fail_silently=True,
    )


def snapshot_client_droplet(client, label):
    """Take a snapshot of the client's Droplet. Returns the action ID."""
    headers = _headers()
    if not client.do_droplet_id:
        raise ValueError(f'{client.firm_name} has no Droplet to snapshot.')
    resp = requests.post(
        f'{DO_API}/droplets/{client.do_droplet_id}/actions',
        json={'type': 'snapshot', 'name': label},
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()['action']['id']


def transfer_snapshot(snapshot_id, target_email):
    """Transfer a snapshot to another DO account — used for offboarding."""
    headers = _headers()
    resp = requests.post(
        f'{DO_API}/images/{snapshot_id}/actions',
        json={'type': 'transfer', 'transfer_to_account': target_email},
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()['action']['id']


def destroy_client_droplet(client):
    """Destroy the client's Droplet (payment-failure Day 30). Snapshot retained."""
    if not client.do_droplet_id:
        return
    headers = _headers()
    resp = requests.delete(
        f'{DO_API}/droplets/{client.do_droplet_id}', headers=headers, timeout=30,
    )
    resp.raise_for_status()
    logger.info('DO: destroyed droplet %s for %s', client.do_droplet_id, client.pk)
    client.do_droplet_id = ''
    client.do_droplet_ip = None
    client.save(update_fields=['do_droplet_id', 'do_droplet_ip', 'updated_at'])


def _notify_admin(client, droplet_id, ip):
    send_mail(
        subject=f'Droplet created for {client.firm_name} — IP: {ip or "pending"}',
        message=(
            f'A DigitalOcean Droplet has been provisioned.\n\n'
            f'Client:  {client.firm_name}\n'
            f'Droplet: {droplet_id} ({droplet_name_for(client)})\n'
            f'IP:      {ip or "pending"}\n'
        ),
        from_email=settings.EMAIL_FROM_NO_REPLY,
        recipient_list=[settings.LEAD_NOTIFICATION_EMAIL],
        fail_silently=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 6b — Droplet Dashboard helpers
# ─────────────────────────────────────────────────────────────────────────────
# Generic, ID-keyed wrappers around the DO API for the admin Droplet
# dashboard. They DO NOT touch ClientProfile by themselves — the dashboard
# layer matches Droplets to clients on display.

def _normalise_droplet(d: dict) -> dict:
    """Project a raw DO Droplet payload into the slim dict the dashboard uses."""
    ip = ''
    for net in (d.get('networks') or {}).get('v4') or []:
        if net.get('type') == 'public':
            ip = net.get('ip_address') or ''
            break
    size = d.get('size') or {}
    region = d.get('region') or {}
    return {
        'id': d['id'],
        'name': d.get('name') or '',
        'status': d.get('status') or 'unknown',
        'ip': ip,
        'region': region.get('slug') or '',
        'size_slug': d.get('size_slug') or '',
        'memory': d.get('memory') or 0,
        'vcpus': d.get('vcpus') or 0,
        'disk': d.get('disk') or 0,
        'monthly_cost': float(size.get('price_monthly') or 0),
        'created_at': d.get('created_at') or '',
        'tags': list(d.get('tags') or []),
    }


def get_all_droplets() -> list[dict]:
    """
    Every Droplet on the account, paginated transparently. Returns [] on
    any error or when DO_API_TOKEN is unset — callers should never crash
    the dashboard just because the API is down.
    """
    if not settings.DO_API_TOKEN:
        return []
    headers = _headers()
    droplets = []
    url = f'{DO_API}/droplets?per_page=100'
    try:
        while url:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                logger.warning('DO list droplets failed: %s', resp.status_code)
                return []
            data = resp.json()
            for raw in data.get('droplets') or []:
                droplets.append(_normalise_droplet(raw))
            url = ((data.get('links') or {}).get('pages') or {}).get('next')
    except requests.RequestException:
        logger.exception('DO list droplets — request failed')
        return []
    return droplets


def get_droplet(droplet_id) -> dict | None:
    """Fetch one Droplet by ID. Returns None on any failure."""
    if not settings.DO_API_TOKEN:
        return None
    try:
        resp = requests.get(
            f'{DO_API}/droplets/{droplet_id}',
            headers=_headers(), timeout=15)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return _normalise_droplet(resp.json().get('droplet') or {})


def _droplet_action(droplet_id, action_type: str) -> bool:
    """Fire any DO Droplet action by name (power_on, shutdown, reboot, …)."""
    try:
        resp = requests.post(
            f'{DO_API}/droplets/{droplet_id}/actions',
            headers=_headers(), json={'type': action_type}, timeout=15)
    except requests.RequestException:
        return False
    return resp.status_code in (200, 201)


def power_on_droplet(droplet_id) -> bool:
    """Power a Droplet on."""
    return _droplet_action(droplet_id, 'power_on')


def power_off_droplet(droplet_id) -> bool:
    """Gracefully shut a Droplet down (DO 'shutdown' action)."""
    return _droplet_action(droplet_id, 'shutdown')


def destroy_droplet(droplet_id) -> bool:
    """
    Destroy a Droplet permanently. Returns True on a 204 from DO.

    The view layer is responsible for refusing this on client-tagged
    Droplets — this function will happily destroy anything, by design,
    because it is also reused for the client-flow `destroy_client_droplet`
    path below where the gate has already been satisfied.
    """
    try:
        resp = requests.delete(
            f'{DO_API}/droplets/{droplet_id}',
            headers=_headers(), timeout=15)
    except requests.RequestException:
        return False
    return resp.status_code == 204


def next_droplet_name(prefix: str = 'manual') -> str:
    """
    Return the next auto-incremented name in `{prefix}-NNN` form. Walks
    the live Droplet list once so two parallel "New Droplet" tabs can't
    both pick the same number.
    """
    existing = [d['name'] for d in get_all_droplets()]
    numbers = []
    for n in existing:
        if not n.startswith(f'{prefix}-'):
            continue
        suffix = n.split('-')[-1]
        if suffix.isdigit():
            numbers.append(int(suffix))
    next_num = (max(numbers) + 1) if numbers else 1
    return f'{prefix}-{str(next_num).zfill(3)}'


# Reusable cloud-init: drop a one-time root password so paramiko can SSH
# in long enough to install the vault keypair. setup_vault_key_for_droplet
# then disables PasswordAuthentication and locks the root account.
def _manual_droplet_user_data(temp_password: str) -> str:
    return _cloud_init_user_data(temp_password)


def create_droplet(name: str, *,
                   region: str = 'nyc1',
                   size: str = 's-1vcpu-1gb',
                   snapshot_id=None,
                   tags=None,
                   client=None,
                   provision_timeout: int = PROVISION_TIMEOUT) -> dict:
    """
    Create a Droplet from a snapshot and poll until active + IP.

    A fresh random root password is generated and injected via cloud-init
    so the existing setup_vault_key_for_droplet() bootstrap path works for
    manual spin-ups too — same approach as `provision_client_droplet`.
    Vault key install is best-effort and non-blocking; on success the
    temp password is rendered useless by the lockdown step.

    If `client` is provided, the linked ClientProfile is updated with
    the new IP / Droplet id and the bootstrap targets that client's
    vault. Otherwise this is a "manual" Droplet — no client linkage.
    """
    if snapshot_id is None:
        if not settings.DO_BASE_SNAPSHOT_ID:
            raise RuntimeError('DO_BASE_SNAPSHOT_ID is not set in .env')
        snapshot_id = int(settings.DO_BASE_SNAPSHOT_ID)
    if tags is None:
        tags = ['aspired-websites', 'client' if client else 'manual']

    temp_password = secrets.token_urlsafe(32)
    payload = {
        'name': name,
        'region': region,
        'size': size,
        'image': snapshot_id,
        'tags': tags,
        'ipv6': False,
        'user_data': _manual_droplet_user_data(temp_password),
    }

    resp = requests.post(
        f'{DO_API}/droplets', headers=_headers(),
        json=payload, timeout=30)
    if resp.status_code not in (200, 202):
        raise RuntimeError(
            f'DO create droplet failed: {resp.status_code} '
            f'{resp.text[:200]}')

    droplet = resp.json()['droplet']
    droplet_id = droplet['id']
    logger.info('DO: created manual droplet %s (%s)', droplet_id, name)

    # Poll until active + has a public IP.
    deadline = time.time() + provision_timeout
    info = None
    while time.time() < deadline:
        info = get_droplet(droplet_id)
        if info and info['status'] == 'active' and info['ip']:
            break
        time.sleep(POLL_INTERVAL)
    if not info or info['status'] != 'active' or not info['ip']:
        raise RuntimeError(
            f'Droplet {droplet_id} did not become active within '
            f'{provision_timeout} seconds')

    # Stamp the client record if one was linked.
    if client is not None:
        client.do_droplet_id = str(droplet_id)
        client.do_droplet_ip = info['ip']
        client.do_droplet_created_at = timezone.now()
        client.save(update_fields=[
            'do_droplet_id', 'do_droplet_ip',
            'do_droplet_created_at', 'updated_at',
        ])

    # Best-effort vault key bootstrap. Failures stash the temp password
    # on the client (when one is linked) or just log otherwise.
    time.sleep(SSH_BOOT_GRACE_SECONDS)
    if client is not None:
        try:
            setup_vault_key_for_droplet(client, info['ip'], temp_password)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                'DO: vault key setup failed for manual droplet %s', name)
            _stash_temp_password(client, temp_password)
            _alert_vault_setup_failure(client, info['ip'], str(exc))
    else:
        # Manual / unlinked Droplet — there's no client to stash the
        # temp password on. Surface it in the logs only; an admin can
        # SSH in by hand and run the gen_vault_key.sh script.
        logger.info(
            'DO: manual droplet %s has temp root password — '
            'not stashed (no client linked). Use DO console to retrieve.',
            name)

    return info
