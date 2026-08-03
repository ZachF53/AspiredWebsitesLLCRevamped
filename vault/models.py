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


# Phase 1 onboarding refactor — credential-type taxonomy. Each list
# is the set of valid `credential_type` values within a category. The
# admin form renders a cascading dropdown: category → type. 'other'
# in any list means "not in the list — describe it in custom_label".
# Order in each list is the display order in the dropdown.
TYPES_BY_CATEGORY = {
    'social': [
        ('facebook',  'Facebook'),
        ('instagram', 'Instagram'),
        ('linkedin',  'LinkedIn'),
        ('twitter',   'X (Twitter)'),
        ('tiktok',    'TikTok'),
        ('youtube',   'YouTube'),
        ('pinterest', 'Pinterest'),
        ('threads',   'Threads'),
        ('other',     'Other social platform'),
    ],
    'cms': [
        ('wordpress_admin',   'WordPress admin'),
        ('shopify_admin',     'Shopify admin'),
        ('squarespace_admin', 'Squarespace admin'),
        ('wix_admin',         'Wix admin'),
        ('webflow_admin',     'Webflow admin'),
        ('custom_site_admin', 'Custom site admin'),
        ('other',             'Other CMS / site builder'),
    ],
    'server': [
        ('ssh',           'SSH access'),
        ('ftp_sftp',      'FTP / SFTP'),
        ('cpanel',        'cPanel'),
        ('hosting_panel', 'Hosting control panel'),
        ('other',         'Other server / hosting'),
    ],
    'infra': [
        ('domain_registrar', 'Domain registrar'),
        ('cloudflare',       'Cloudflare'),
        ('email_workspace',  'Email workspace (Google / 365)'),
        ('other',            'Other domain / infrastructure'),
    ],
    'google': [
        ('google_analytics',       'Google Analytics'),
        ('google_search_console',  'Google Search Console'),
        ('google_business_profile','Google Business Profile'),
        ('google_ads',             'Google Ads'),
        ('other',                  'Other Google service'),
    ],
    'other': [
        ('other', 'Other (describe below)'),
    ],
}


def all_credential_type_choices():
    """Flat (value, label) list across every category — used as a
    fallback choice list when the cascading dropdown JS hasn't run
    yet, and for admin / select-widget validation."""
    seen = set()
    out = []
    for cat, items in TYPES_BY_CATEGORY.items():
        for value, label in items:
            if value in seen:
                continue
            seen.add(value)
            out.append((value, label))
    return out


class VaultConfig(TimestampedModel):
    """Singleton — holds the PIN verification hash, salt, lockout state,
    and the vault-level TOTP secret (one authenticator entry per admin,
    not one per server)."""

    pin_hash = models.CharField(max_length=256, blank=True)
    # 32-byte random salt (set at PIN setup). default=b'' so the singleton row
    # can be created before a PIN exists.
    encryption_salt = models.BinaryField(max_length=32, default=b'')
    pin_set = models.BooleanField(default=False)
    failed_attempts = models.IntegerField(default=0)
    lockout_until = models.DateTimeField(null=True, blank=True)

    # Vault-level TOTP. Secret is AES-256-GCM encrypted with the PIN-derived
    # vault key, so it is unreadable without an unlocked vault. Verified once
    # per PIN session; every SSH terminal opened in that session is then
    # authorised without another code.
    totp_secret_encrypted = models.TextField(blank=True)
    totp_configured = models.BooleanField(default=False)

    # Recovery codes for when the authenticator app is lost. Stored as a
    # list of {"code_hash": "<sha256 hex>", "used": bool} — never the
    # plaintext. Generated once when TOTP is configured (shown to the
    # admin a single time) and regenerated on demand from the vault
    # settings page. Each code consumes itself on use.
    recovery_codes = models.JSONField(default=list, blank=True)

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
    # Phase A — vault is account-level. One PIN unlocks every cred for
    # every Website under that Account.
    account_new = models.OneToOneField(
        'clients.Account', on_delete=models.CASCADE,
        related_name='vault_new', null=True, blank=True,
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

    # Phase 1 onboarding refactor — new taxonomy. Each category groups
    # related credential types; the cascading dropdown in the form
    # filters TYPES_BY_CATEGORY by the chosen category. Data migration
    # 0008 maps the old categories (server/domain/google/social/email/
    # stripe/custom) onto these new ones losslessly.
    CATEGORY_CHOICES = [
        ('social', 'Social profile'),
        ('cms', 'Website / CMS'),
        ('server', 'Server / hosting'),
        ('infra', 'Domain & infrastructure'),
        ('google', 'Google services'),
        ('other', 'Other'),
    ]

    vault = models.ForeignKey(
        ClientVault, on_delete=models.CASCADE, related_name='credentials',
    )
    # Phase A — per-Account vault view groups credentials by Website
    # tag (so SSH key for site A and site B render under their
    # respective headings). Nullable — credentials not tied to a
    # specific build show under an "Account-wide" bucket.
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.SET_NULL,
        related_name='vault_credentials_new',
        null=True, blank=True,
    )
    category = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, default='other',
    )
    # Per-category sub-type (slug). Drives the To-Do auto-completion in
    # the SetupTodo widget — when a credential is saved with type
    # 'facebook', the matching open SetupTodo flips to completed. The
    # full type list per category lives in TYPES_BY_CATEGORY below.
    # 'other' (the default) means "no specific type" and requires
    # `custom_label` to identify what it actually is.
    credential_type = models.CharField(
        max_length=40, default='other', blank=True,
    )
    # Required if credential_type == 'other'. Free text describing
    # what this credential is for (e.g. "Postmark account").
    custom_label = models.CharField(max_length=100, blank=True)

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

    # True when the CLIENT added this credential from their portal (vs
    # staff sharing one down). Keeps the two groups separate in both the
    # portal and the admin vault. Client-added creds are encrypted with
    # the server key on save, so the admin's re-encrypt-on-unlock pulls
    # them into the admin vault automatically — staff always see them.
    created_by_client = models.BooleanField(default=False)

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

    # When True, sensitive fields are encrypted with a VAULT_SERVER_SECRET-
    # derived server key (used by automated Droplet provisioning, before any
    # admin has unlocked the vault). The vault re-encrypts them with the
    # PIN-derived key the first time an admin opens the credential.
    encrypted_with_server_key = models.BooleanField(default=False)

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
    # Phase A — account-level audit trail (and optional website tag
    # mirrors the credential's website_new).
    account_new = models.ForeignKey(
        'clients.Account', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ssh_sessions_new',
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ssh_sessions_new',
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


class OpsSession(TimestampedModel):
    """
    A complete AI Ops Agent session against one SSH credential.

    Every chat turn, every command suggested + executed (or denied), and
    the server-state snapshot taken at session start all land here. The
    session row is the source of truth for replay — a Session Replay
    page reconstructs the entire conversation verbatim from
    `conversation`, `commands_executed`, and the two dangerous-command
    lists.
    """

    credential = models.ForeignKey(
        VaultCredential, on_delete=models.CASCADE,
        related_name='ops_sessions',
    )
    client = models.ForeignKey(
        'clients.ClientProfile', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ops_sessions',
    )
    # Phase A — account/website tags for the audit trail.
    account_new = models.ForeignKey(
        'clients.Account', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ops_sessions_new',
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='ops_sessions_new',
    )

    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.IntegerField(null=True, blank=True)

    # How the session ended. This is an audit log, so "the operator
    # closed it", "an admin killed it from the list" and "it was reaped
    # for going quiet" are three different facts and must not all look
    # like a clean exit.
    END_NORMAL = 'normal'
    END_KILLED = 'killed'
    END_IDLE = 'idle_timeout'
    END_REASONS = [
        (END_NORMAL, 'Ended by operator'),
        (END_KILLED, 'Killed from the sessions list'),
        (END_IDLE, 'Auto-closed after inactivity'),
    ]
    end_reason = models.CharField(
        max_length=20, choices=END_REASONS, blank=True,
        help_text='Blank while the session is still open.')

    # Last time anything actually happened — a chat turn, a command, an
    # approve/deny. `updated_at` would drift on any unrelated save, so
    # idle detection gets its own column it can trust.
    last_activity_at = models.DateTimeField(null=True, blank=True)

    # Full conversation history: list of
    #   {role: 'user'|'assistant', content: str, timestamp: iso,
    #    is_system?: bool}
    # System-flagged user messages (denials, etc.) render differently
    # in the replay so it's clear they weren't typed by the operator.
    conversation = models.JSONField(default=list, blank=True)

    # Every command actually run on the box: list of
    #   {command, output, exit_code, timestamp,
    #    was_dangerous, approved_by_human, denied_by_human}
    commands_executed = models.JSONField(default=list, blank=True)

    # Safety-gate decisions — duplicates info on commands_executed but
    # keeps an at-a-glance list for the session replay header.
    dangerous_commands_approved = models.JSONField(
        default=list, blank=True)
    dangerous_commands_denied = models.JSONField(
        default=list, blank=True)

    total_tokens_used = models.IntegerField(default=0)

    # Server-state snapshot taken once at session start so a later
    # replay can show what the box looked like when the conversation
    # began.
    context_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-started_at']
        verbose_name = 'AI Ops Session'
        verbose_name_plural = 'AI Ops Sessions'

    def __str__(self):
        return (f'{self.credential.label} — '
                f"{self.started_at.strftime('%Y-%m-%d %H:%M')}")

    # Sessions go quiet without ever being closed — the operator shuts
    # the tab, the browser crashes, a deploy restarts gunicorn — and
    # the old end-session endpoint could only close the session bound
    # to the *current* Django session. Anything else stayed "LIVE"
    # forever; the list had rows still showing live two months on.
    IDLE_TIMEOUT_MINUTES = 60

    @property
    def is_live(self):
        return self.ended_at is None

    def touch(self, save=True):
        """Record activity. Called on every chat turn and command."""
        from django.utils import timezone as _tz
        self.last_activity_at = _tz.now()
        if save:
            self.save(update_fields=['last_activity_at', 'updated_at'])

    def close(self, reason=END_NORMAL, when=None):
        """
        Stamp ended_at/duration/end_reason. Idempotent — a session that
        is already closed keeps its original timestamps, so a double
        click on Kill cannot rewrite history.
        """
        from django.utils import timezone as _tz
        if self.ended_at is not None:
            return False
        self.ended_at = when or _tz.now()
        self.end_reason = reason
        self.duration_seconds = max(
            0, int((self.ended_at - self.started_at).total_seconds()))
        self.save(update_fields=[
            'ended_at', 'end_reason', 'duration_seconds', 'updated_at'])
        return True

    @classmethod
    def close_idle(cls, minutes=None):
        """
        Close every open session whose last activity is older than the
        timeout. Returns the number closed.

        ended_at is backdated to the last activity, NOT to now. A
        session abandoned in May and reaped in August did not run for
        three months, and an audit log that says it did is worse than
        one that says nothing. Sessions that never recorded any
        activity fall back to started_at.
        """
        from datetime import timedelta

        from django.utils import timezone as _tz
        minutes = minutes or cls.IDLE_TIMEOUT_MINUTES
        cutoff = _tz.now() - timedelta(minutes=minutes)
        closed = 0
        for session in cls.objects.filter(ended_at__isnull=True):
            last = session.last_activity_at or session.started_at
            if last <= cutoff:
                if session.close(reason=cls.END_IDLE, when=last):
                    closed += 1
        return closed
