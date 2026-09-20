"""
Shared "open an SSH session to a client's Droplet from a background job"
primitive. One implementation, two callers: billing.do_helpers (payment-
failure maintenance mode / reinstatement) and reporting.droplet_audit
(the monthly SSH health check).

Background jobs have no admin PIN session, so they can only ever use a
server-key-encrypted credential. That used to mean "only until the first
time an admin opens this credential in the vault UI" — encrypted_with_
server_key flips to False forever at that point (vault.crypto.
reencrypt_credential_with_pin_key). automation_ssh_private_key_encrypted
is a second, permanent, always-server-key-encrypted copy of the same key
that exists specifically so background access survives that. It's
populated at credential creation (billing.do_helpers._create_ssh_vault_
credential) and refreshed on every manual edit
(vault.views._apply_ssh_fields) — reencrypt_credential_with_pin_key never
touches it.
"""

import logging
from io import StringIO

import paramiko
from django.utils import timezone

logger = logging.getLogger(__name__)


def _load_private_key(key_text, passphrase=None):
    """Parse a PEM/OpenSSH private key, trying the common key types in
    turn — same order as vault/consumers.py's SSH terminal."""
    for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_cls.from_private_key(
                StringIO(key_text), password=passphrase or None)
        except (paramiko.SSHException, ValueError):
            continue
    return None


def _automation_credential(website):
    """The account's automation-enabled SSH credential, or None.

    Credentials are account-scoped (one key per customer); the Droplet
    is site-scoped, so the host comes from the site and the key from
    its account. `vault__account_new_id` — NOT a bare `account_new_id`,
    which isn't a field on VaultCredential (it lives on the related
    ClientVault row) and raises FieldError if used directly.
    """
    if website.account_id is None:
        return None
    from vault.models import VaultCredential
    return (VaultCredential.objects
            .filter(vault__account_new_id=website.account_id,
                    is_ssh_credential=True,
                    automation_access_enabled=True)
            .exclude(automation_ssh_private_key_encrypted='')
            .first())


def open_automation_ssh(website, *, timeout=10):
    """Open a paramiko SSH session as root@<droplet_ip> for `website`,
    using the account's automation-safe credential. Returns the open
    client, or None on any failure (no droplet IP, no credential,
    automation revoked, decrypt failure, connect failure). Caller must
    ssh.close() when done."""
    if not website.do_droplet_ip:
        return None

    cred = _automation_credential(website)
    if cred is None:
        return None

    from vault.crypto import decrypt_value, derive_server_key
    try:
        pkey_str = decrypt_value(
            cred.automation_ssh_private_key_encrypted, derive_server_key())
        if not pkey_str or pkey_str.startswith('['):
            return None
        pkey = _load_private_key(pkey_str)
        if pkey is None:
            return None

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(website.do_droplet_ip, username='root', pkey=pkey,
                    timeout=timeout, banner_timeout=timeout,
                    auth_timeout=timeout)
    except Exception:
        logger.exception(
            'open_automation_ssh: SSH open failed for website %s',
            website.pk)
        return None

    cred.automation_key_last_used_at = timezone.now()
    cred.save(update_fields=['automation_key_last_used_at'])
    return ssh


def run_remote(ssh, cmd, *, timeout=30, check=True):
    """Run a remote command and return (exit_code, stdout, stderr).

    check=True (the default, matching the old billing.do_helpers._run)
    raises RuntimeError on a non-zero exit — use for provisioning steps
    that must succeed. check=False returns whatever came back and never
    raises — use for best-effort steps (a droplet audit checking for an
    optional tool, a maintenance-mode toggle that has a power-off
    fallback either way).
    """
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode('utf-8', errors='replace')
    err = stderr.read().decode('utf-8', errors='replace')
    code = stdout.channel.recv_exit_status()
    if check and code != 0:
        raise RuntimeError(
            f'remote command failed (exit {code}): {cmd}\n{err or out}')
    return code, out, err


def run_remote_best_effort(ssh, cmd, *, timeout=20):
    """Best-effort remote command. Returns exit code only, swallows
    paramiko exceptions (caller treats failure as 'didn't take'). Same
    shape as the old billing.do_helpers._run_remote."""
    try:
        _, stdout, _ = ssh.exec_command(cmd, timeout=timeout)
        return stdout.channel.recv_exit_status()
    except Exception:
        return -1
