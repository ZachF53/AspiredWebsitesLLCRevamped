"""
Vault models — encrypted client-credential store.

All sensitive credential values are AES-256-GCM encrypted at rest. The PIN
is never stored; VaultConfig holds only a verification hash and the salt.
"""

import uuid

from django.db import models

from clients.models import ClientProfile
from core.models import TimestampedModel

# VaultConfig is a singleton. TimestampedModel uses a UUID primary key, so the
# singleton is pinned to a fixed UUID rather than integer pk=1.
SINGLETON_ID = uuid.UUID(int=1)


class VaultConfig(TimestampedModel):
    """Singleton — holds the PIN verification hash, salt, and lockout state."""

    pin_hash = models.CharField(max_length=256, blank=True)
    # 32-byte random salt (set at PIN setup). default=b'' so the singleton row
    # can be created before a PIN exists.
    encryption_salt = models.BinaryField(max_length=32, default=b'')
    pin_set = models.BooleanField(default=False)
    failed_attempts = models.IntegerField(default=0)
    lockout_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Vault Configuration'
        verbose_name_plural = 'Vault Configuration'

    def save(self, *args, **kwargs):
        self.pk = SINGLETON_ID  # always the one singleton row
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=SINGLETON_ID)
        return obj

    def __str__(self):
        return 'Vault Configuration'


class ClientVault(TimestampedModel):
    """One vault per client — a container for that client's credentials."""

    client = models.OneToOneField(
        ClientProfile, on_delete=models.CASCADE, related_name='vault',
    )
    notes = models.TextField(
        blank=True,
        help_text='General plaintext notes about this client\'s setup '
                  '(not sensitive).',
    )

    class Meta:
        ordering = ['client__firm_name']

    def __str__(self):
        return f'Vault — {self.client.firm_name}'


class VaultCredential(TimestampedModel):
    """A single stored credential. Sensitive fields are AES-256-GCM encrypted."""

    CATEGORY_CHOICES = [
        ('server', 'Server / Hosting'),
        ('domain', 'Domain Registrar'),
        ('google', 'Google Account'),
        ('social', 'Social Media'),
        ('email', 'Email / DNS'),
        ('stripe', 'Stripe / Payments'),
        ('custom', 'Custom'),
    ]

    vault = models.ForeignKey(
        ClientVault, on_delete=models.CASCADE, related_name='credentials',
    )
    category = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, default='custom',
    )
    label = models.CharField(max_length=200)

    # Sensitive — AES-256-GCM encrypted hex (nonce + ciphertext).
    username_encrypted = models.TextField(blank=True)
    password_encrypted = models.TextField(blank=True)
    url_encrypted = models.TextField(blank=True)
    notes_encrypted = models.TextField(blank=True)

    # Non-sensitive metadata (plaintext) — a masked hint, never the full value.
    username_hint = models.CharField(max_length=50, blank=True)

    sort_order = models.IntegerField(default=0)

    # Client visibility. When True, the decrypted values are copied into the
    # client_*_plain fields below so the client portal can show them without
    # the admin PIN. When toggled off, those fields are cleared.
    visible_to_client = models.BooleanField(default=False)
    client_username_plain = models.TextField(blank=True)
    client_password_plain = models.TextField(blank=True)
    client_url_plain = models.URLField(blank=True)
    client_notes_plain = models.TextField(blank=True)

    # ── SSH credential — all sensitive fields AES-256-GCM encrypted at rest ──
    SSH_AUTH_CHOICES = [
        ('password', 'Password'),
        ('private_key', 'Private Key'),
    ]

    is_ssh_credential = models.BooleanField(default=False)
    ssh_host_encrypted = models.TextField(blank=True)
    ssh_port = models.IntegerField(default=22)
    ssh_username_encrypted = models.TextField(blank=True)
    ssh_auth_type = models.CharField(
        max_length=12, choices=SSH_AUTH_CHOICES, default='password', blank=True,
    )
    ssh_password_encrypted = models.TextField(blank=True)
    ssh_private_key_encrypted = models.TextField(blank=True)
    ssh_key_passphrase_encrypted = models.TextField(blank=True)

    # TOTP for elevated SSH access — secret encrypted with the vault key.
    totp_secret_encrypted = models.TextField(blank=True)
    totp_configured = models.BooleanField(default=False)

    class Meta:
        ordering = ['category', 'sort_order', 'label']
        verbose_name = 'Vault Credential'
        verbose_name_plural = 'Vault Credentials'

    def __str__(self):
        return f'{self.vault.client.firm_name} — {self.label}'


class ServerCommandLibrary(TimestampedModel):
    """A saved, runnable command for an SSH credential's terminal."""

    CATEGORY_CHOICES = [
        ('maintenance', 'Maintenance'),
        ('logs', 'Logs'),
        ('monitoring', 'Monitoring'),
        ('deploy', 'Deploy'),
        ('custom', 'Custom'),
    ]

    credential = models.ForeignKey(
        VaultCredential, on_delete=models.CASCADE, related_name='commands',
    )
    label = models.CharField(max_length=200)
    command = models.CharField(max_length=500)
    category = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, default='custom',
    )
    requires_confirmation = models.BooleanField(default=False)
    is_dangerous = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['category', 'sort_order']
        verbose_name = 'Server Command'
        verbose_name_plural = 'Server Commands'

    def __str__(self):
        return f'{self.credential.label} — {self.label}'


class SSHSessionLog(TimestampedModel):
    """An audit record of one browser-terminal SSH session."""

    credential = models.ForeignKey(
        VaultCredential, on_delete=models.CASCADE, related_name='ssh_sessions',
    )
    client = models.ForeignKey(
        'clients.ClientProfile', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ssh_sessions',
    )
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.IntegerField(null=True, blank=True)
    totp_verified = models.BooleanField(default=False)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    # List of {command, timestamp, was_dangerous, approved_by_human}.
    commands_executed = models.JSONField(default=list)

    class Meta:
        ordering = ['-started_at']
        verbose_name = 'SSH Session Log'
        verbose_name_plural = 'SSH Session Logs'

    def __str__(self):
        return f'{self.credential.label} — {self.started_at}'


class VaultAccessLog(TimestampedModel):
    """An append-only audit trail of every vault action."""

    ACTION_CHOICES = [
        ('pin_set', 'PIN Set'),
        ('pin_verified', 'PIN Verified — Vault Unlocked'),
        ('pin_failed', 'PIN Failed'),
        ('pin_locked', 'Vault Locked — Too Many Attempts'),
        ('credential_viewed', 'Credential Viewed'),
        ('credential_created', 'Credential Created'),
        ('credential_updated', 'Credential Updated'),
        ('credential_deleted', 'Credential Deleted'),
        ('ssh_totp_setup', 'SSH TOTP Configured'),
        ('ssh_totp_verified', 'SSH TOTP Verified — Session Started'),
        ('ssh_connected', 'SSH Terminal Connected'),
        ('ssh_disconnected', 'SSH Terminal Disconnected'),
    ]

    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    # Client name stored as plaintext (not an FK) so the audit trail survives
    # client deletion.
    client_name = models.CharField(max_length=200, blank=True)
    credential_label = models.CharField(max_length=200, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Vault Access Log'
        verbose_name_plural = 'Vault Access Logs'

    def __str__(self):
        return f'{self.action} — {self.created_at}'
