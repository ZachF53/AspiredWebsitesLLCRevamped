"""
WebSocket SSH terminal consumer.

A real interactive SSH session bridged to an xterm.js terminal in the browser.
Every connection is gated by: authenticated staff user + unlocked vault
(SECRET_KEY-wrapped key in the session) + a TOTP-verified SSH session.
"""

import io
import json
import logging
import select
import threading
import time
from datetime import datetime, timedelta

import paramiko
from channels.generic.websocket import WebsocketConsumer
from django.utils import timezone

logger = logging.getLogger(__name__)

VAULT_SESSION_HOURS = 1
SSH_SESSION_MINUTES = 15


def _load_private_key(key_text, passphrase):
    """
    Parse a PEM private key, trying the common key types in turn.
    DSSKey is intentionally omitted — Paramiko 3.x+ dropped it.
    """
    for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_cls.from_private_key(
                io.StringIO(key_text), password=passphrase or None)
        except (paramiko.SSHException, ValueError):
            continue
    raise paramiko.SSHException('Unsupported or invalid private key.')


class SSHTerminalConsumer(WebsocketConsumer):
    """Bridges a browser terminal to a paramiko SSH shell."""

    def connect(self):
        self.cred_id = self.scope['url_route']['kwargs']['cred_id']
        self.user = self.scope.get('user')
        self.ssh_client = None
        self.channel = None
        self.session_log = None
        self.credential = None

        # ── Auth gate ──
        if not self.user or not self.user.is_authenticated:
            self.close(code=4001)
            return
        if not self.user.is_staff:
            self.close(code=4003)
            return

        vault_key = self._get_vault_key()
        if not vault_key:
            self.close(code=4002)
            return
        if not self._is_totp_valid():
            self.close(code=4004)
            return

        from vault.models import VaultCredential
        self.credential = VaultCredential.objects.filter(
            id=self.cred_id, is_ssh_credential=True).first()
        if self.credential is None:
            self.close(code=4005)
            return

        # ── Decrypt SSH connection details with the vault key ──
        from vault.crypto import decrypt_value
        self.ssh_host = decrypt_value(self.credential.ssh_host_encrypted, vault_key)
        self.ssh_port = self.credential.ssh_port or 22
        self.ssh_user = decrypt_value(
            self.credential.ssh_username_encrypted, vault_key)
        self.ssh_auth_type = self.credential.ssh_auth_type or 'password'
        if self.ssh_auth_type == 'password':
            self.ssh_password = decrypt_value(
                self.credential.ssh_password_encrypted, vault_key)
            self.ssh_key = None
            self.ssh_passphrase = None
        else:
            self.ssh_password = None
            self.ssh_key = decrypt_value(
                self.credential.ssh_private_key_encrypted, vault_key)
            self.ssh_passphrase = (
                decrypt_value(self.credential.ssh_key_passphrase_encrypted,
                              vault_key)
                if self.credential.ssh_key_passphrase_encrypted else None)

        self.accept()
        threading.Thread(target=self._start_ssh, daemon=True).start()

    # ── SSH lifecycle ──────────────────────────────────────────────────────

    def _start_ssh(self):
        try:
            self.ssh_client = paramiko.SSHClient()
            self.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs = {
                'hostname': self.ssh_host,
                'port': self.ssh_port,
                'username': self.ssh_user,
                'timeout': 15,
                'allow_agent': False,
                'look_for_keys': False,
            }
            if self.ssh_auth_type == 'password':
                connect_kwargs['password'] = self.ssh_password
            else:
                connect_kwargs['pkey'] = _load_private_key(
                    self.ssh_key, self.ssh_passphrase)

            self.ssh_client.connect(**connect_kwargs)
            self.channel = self.ssh_client.invoke_shell(
                term='xterm-256color', width=220, height=50)
            self.channel.setblocking(False)

            self._create_session_log()
            self._log_access('ssh_connected', 'SSH terminal session opened.')
            self._read_output()

        except Exception as exc:  # noqa: BLE001 — report any failure to the UI
            logger.exception('SSH connection failed for credential %s',
                             self.cred_id)
            try:
                self.send(text_data=json.dumps({
                    'type': 'error',
                    'message': f'Connection failed: {exc}',
                }))
            except Exception:
                pass
            self.close()

    def _read_output(self):
        """Stream SSH channel output to the browser until the channel closes."""
        while self.channel and not self.channel.closed:
            try:
                readable, _, _ = select.select([self.channel], [], [], 0.1)
                if readable:
                    data = self.channel.recv(4096)
                    if not data:
                        break
                    self.send(text_data=json.dumps({
                        'type': 'output',
                        'data': data.decode('utf-8', errors='replace'),
                    }))
                time.sleep(0.01)
            except Exception:
                break
        self.close()

    def receive(self, text_data):
        """Handle keystrokes, resize events, and library commands."""
        try:
            data = json.loads(text_data)
        except (ValueError, TypeError):
            return
        msg_type = data.get('type')

        if msg_type == 'input' and self.channel:
            self.channel.send(str(data.get('data', '')).encode('utf-8'))

        elif msg_type == 'command' and self.channel:
            command = str(data.get('data', ''))
            self._log_command(command, bool(data.get('dangerous')))
            self.channel.send(command.encode('utf-8'))

        elif msg_type == 'resize' and self.channel:
            try:
                self.channel.resize_pty(
                    width=int(data.get('cols', 220)),
                    height=int(data.get('rows', 50)))
            except Exception:
                pass

    def disconnect(self, close_code):
        if self.session_log:
            try:
                self.session_log.ended_at = timezone.now()
                delta = self.session_log.ended_at - self.session_log.started_at
                self.session_log.duration_seconds = int(delta.total_seconds())
                self.session_log.save(update_fields=[
                    'ended_at', 'duration_seconds', 'updated_at'])
            except Exception:
                logger.exception('Failed to finalise SSH session log')
        if self.credential:
            self._log_access('ssh_disconnected', 'SSH terminal session closed.')
        if self.channel:
            try:
                self.channel.close()
            except Exception:
                pass
        if self.ssh_client:
            try:
                self.ssh_client.close()
            except Exception:
                pass

    # ── Helpers ────────────────────────────────────────────────────────────

    def _create_session_log(self):
        from vault.models import SSHSessionLog
        client_ip = None
        if self.scope.get('client'):
            client_ip = self.scope['client'][0]
        try:
            self.session_log = SSHSessionLog.objects.create(
                credential=self.credential,
                client=self.credential.vault.client,
                totp_verified=True,
                ip_address=client_ip,
            )
        except Exception:
            logger.exception('Failed to create SSH session log')

    def _log_command(self, command, dangerous):
        if not self.session_log:
            return
        try:
            entry = {
                'command': command.strip(),
                'timestamp': timezone.now().isoformat(),
                'was_dangerous': dangerous,
                'approved_by_human': True,
            }
            self.session_log.commands_executed = (
                (self.session_log.commands_executed or []) + [entry])
            self.session_log.save(update_fields=[
                'commands_executed', 'updated_at'])
        except Exception:
            logger.exception('Failed to log SSH command')

    def _log_access(self, action, note):
        from vault.models import VaultAccessLog
        try:
            client_ip = None
            if self.scope.get('client'):
                client_ip = self.scope['client'][0]
            VaultAccessLog.objects.create(
                action=action,
                client_name=self.credential.vault.client.firm_name,
                credential_label=self.credential.label,
                ip_address=client_ip,
                note=note,
            )
        except Exception:
            logger.exception('Failed to write vault access log')

    def _get_vault_key(self):
        """
        Recover the AES vault key from the session — same scheme as
        vault.views.get_vault_key: a SECRET_KEY-wrapped key, never raw.
        """
        from vault.crypto import unwrap_key
        session = self.scope.get('session')
        if not session:
            return None
        unlocked_at = session.get('vault_unlocked_at')
        wrapped = session.get('vault_key_wrapped')
        if not unlocked_at or not wrapped:
            return None
        try:
            unlocked_time = datetime.fromisoformat(unlocked_at)
        except (TypeError, ValueError):
            return None
        if timezone.now() > unlocked_time + timedelta(hours=VAULT_SESSION_HOURS):
            return None
        return unwrap_key(wrapped)

    def _is_totp_valid(self):
        """Check the TOTP-verified SSH session window from the session dict."""
        session = self.scope.get('session')
        if not session:
            return False
        if not session.get(f'ssh_session_{self.cred_id}_verified'):
            return False
        raw = session.get(f'ssh_session_{self.cred_id}_verified_at')
        if not raw:
            return False
        try:
            verified_at = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return False
        return timezone.now() <= verified_at + timedelta(
            minutes=SSH_SESSION_MINUTES)
