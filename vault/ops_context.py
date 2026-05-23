"""
AI Ops Agent — server-context fetch + Claude system-prompt builder.

`get_server_context(credential, vault_key)` runs a handful of read-only
diagnostics over SSH once per session start; the result is stored on
OpsSession.context_snapshot so the agent has accurate "what was the
state when we began" data for the entire conversation.

`build_system_prompt(...)` assembles the system message handed to
Claude on every turn. The credential's IP, username, and key material
are NEVER inlined into the prompt — Claude works with the server
abstractly and the SSH execution layer holds the secrets.
"""

import io
import logging

import paramiko

from vault.crypto import decrypt_value

logger = logging.getLogger(__name__)


# ── SSH helper (shared with ops_execute) ────────────────────────────────────

def open_ssh_for_credential(credential, vault_key, timeout=15):
    """
    Open a paramiko SSHClient against this credential, using either the
    encrypted password or the encrypted private key. The caller closes.
    Returns a connected SSHClient. Raises paramiko.SSHException (or its
    children) on any failure — never returns None silently.
    """
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    host = decrypt_value(credential.ssh_host_encrypted, vault_key)
    username = decrypt_value(credential.ssh_username_encrypted, vault_key)

    connect_kwargs = {
        'hostname': host,
        'port': credential.ssh_port or 22,
        'username': username,
        'timeout': timeout,
        'allow_agent': False,
        'look_for_keys': False,
    }

    if (credential.ssh_auth_type or 'password') == 'password':
        connect_kwargs['password'] = decrypt_value(
            credential.ssh_password_encrypted, vault_key)
    else:
        key_data = decrypt_value(
            credential.ssh_private_key_encrypted, vault_key)
        passphrase = (
            decrypt_value(credential.ssh_key_passphrase_encrypted, vault_key)
            if credential.ssh_key_passphrase_encrypted else None)
        connect_kwargs['pkey'] = _load_private_key(key_data, passphrase)

    ssh.connect(**connect_kwargs)
    return ssh


def _load_private_key(key_text, passphrase):
    """Try Ed25519 → RSA → ECDSA in order. Matches consumers.py."""
    for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_cls.from_private_key(
                io.StringIO(key_text), password=passphrase or None)
        except (paramiko.SSHException, ValueError):
            continue
    raise paramiko.SSHException('Unsupported or invalid private key.')


# ── Context fetch ──────────────────────────────────────────────────────────

# Read-only diagnostics — strictly informational, no side effects, no
# data exposure. Each one is short and capped with a fallback so the
# whole snapshot still renders if a single command isn't available
# (e.g. supervisorctl on a fresh Droplet without supervisor installed).
_CONTEXT_COMMANDS = {
    'supervisorctl_status': (
        'supervisorctl status 2>/dev/null '
        '|| echo "supervisorctl not available"'),
    'disk_usage':    'df -h / 2>/dev/null',
    'memory_usage':  'free -h 2>/dev/null',
    'uptime':        'uptime 2>/dev/null',
    'nginx_status':  ('systemctl is-active nginx 2>/dev/null '
                      '|| echo "unknown"'),
    'recent_errors': ('tail -20 /var/www/aspired/logs/gunicorn-error.log '
                      '2>/dev/null || echo "Log not found"'),
}


def get_server_context(credential, vault_key) -> dict:
    """
    Snapshot the server state at session start. Returns a dict with
    keys matching `_CONTEXT_COMMANDS` plus an optional `connection_error`
    when SSH itself failed. Every value is a string; never raises.
    """
    context = {key: 'Unable to fetch' for key in _CONTEXT_COMMANDS}

    try:
        ssh = open_ssh_for_credential(credential, vault_key)
    except Exception as exc:  # noqa: BLE001 — defensive
        context['connection_error'] = str(exc)
        return context

    try:
        for key, cmd in _CONTEXT_COMMANDS.items():
            try:
                _, stdout, _ = ssh.exec_command(cmd, timeout=10)
                context[key] = stdout.read().decode(
                    'utf-8', errors='replace').strip()
            except Exception:  # noqa: BLE001 — single-cmd fallback
                context[key] = 'Unable to fetch'
    finally:
        try:
            ssh.close()
        except Exception:
            pass

    return context


# ── System prompt ──────────────────────────────────────────────────────────

def build_system_prompt(credential, client, context,
                        scan_summary=None) -> str:
    """
    Build the system message sent with every Claude turn.

    The credential's host / username / key material are deliberately
    NOT inlined — Claude reasons about "the server" abstractly. The
    SSH layer holds the secrets and just executes whatever Claude
    suggests (after the safety gate).
    """
    from vault.models import ServerCommandLibrary

    commands = (
        ServerCommandLibrary.objects
        .filter(credential=credential)
        .order_by('category', 'sort_order')
    )
    command_list = '\n'.join(
        f"  - {cmd.label}: {cmd.command}"
        f"{' [REQUIRES CONFIRMATION]' if cmd.requires_confirmation else ''}"
        f"{' [DANGEROUS]' if cmd.is_dangerous else ''}"
        for cmd in commands
    ) or '  (none configured)'

    # Client block — optional; some credentials are agency-internal.
    client_block = ''
    if client:
        live_url = ''
        live_project = client.projects.filter(stage='live').first()
        if live_project and live_project.live_url:
            live_url = live_project.live_url
        client_block = (
            f"\nCLIENT INFORMATION:\n"
            f"  Firm:           {client.firm_name}\n"
            f"  Business type:  {client.business_type or 'n/a'}\n"
            f"  Domain:         {live_url or 'unknown'}\n"
            f"  City / State:   "
            f"{client.city or '?'}, {client.state or '?'}\n"
        )

    scan_block = f"\nLATEST SECURITY SCAN:\n{scan_summary}\n" if scan_summary else ''

    # First-N dangerous patterns for the agent's prompt — full list lives
    # in ops_safety; we only need to give Claude the shape so it knows
    # WHICH commands will hit the safety gate.
    from vault.ops_safety import DANGEROUS_PATTERNS
    dangerous_examples = '\n'.join(
        f"  - {reason}: pattern /{pattern}/"
        for pattern, reason in DANGEROUS_PATTERNS[:12]
    )

    return f"""You are an expert DevOps assistant for Aspired Websites LLC. You help manage Ubuntu servers running Django web applications.
{client_block}
SERVER INFORMATION:
  Host:      ***configured*** (executed via secure SSH layer)
  Username:  ***configured***
  OS:        Ubuntu (24.04 or similar)
  Stack:     Nginx + Gunicorn + Django + Celery + Redis

CURRENT SERVER STATE (fetched at session start):

Supervisor status:
{context.get('supervisorctl_status', 'Unknown')}

Disk usage:
{context.get('disk_usage', 'Unknown')}

Memory:
{context.get('memory_usage', 'Unknown')}

Uptime:
{context.get('uptime', 'Unknown')}

Nginx status: {context.get('nginx_status', 'Unknown')}

Recent error log (last 20 lines):
{context.get('recent_errors', 'None')}
{scan_block}
AVAILABLE QUICK COMMANDS (from the credential's command library):
{command_list}

YOUR ROLE:
  - Diagnose and resolve server issues.
  - Suggest shell commands; the platform executes them via SSH and
    feeds the output back to you in the next turn so you can continue.
  - Always explain WHAT you're doing and WHY before suggesting a
    command.
  - Read command output carefully and summarise what it means.

COMMAND PROTOCOL — IMPORTANT:
  - When you want to run a command, wrap it in a ```bash code block
    with one command per line. Plain prose suggestions outside a code
    block are NOT executed.
  - Comments inside a ```bash block (lines starting with `#`) are
    skipped — useful for inline explanation.

SAFETY GATE:
  Certain commands ALWAYS require human approval before execution.
  Common examples:
{dangerous_examples}
  When you suggest a command that hits the safety gate, the operator
  is shown an Approve / Deny dialog. If they Deny, you'll get a
  follow-up message saying so — find an alternative or explain why
  the dangerous command is genuinely necessary.

  Never assume approval. Don't repeat the same denied command without
  good reason.

COMMUNICATION STYLE:
  - Be concise but thorough. Lead with what you found, then what
    you'll do next.
  - After a command runs, summarise what the output tells you and
    what the next step is.
  - Ask clarifying questions if the operator's request is ambiguous.
  - When the issue is resolved, say so clearly and summarise what
    was done."""
