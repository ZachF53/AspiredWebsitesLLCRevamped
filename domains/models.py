"""
Domain registration models.

A `DomainRegistration` is one purchased domain on the client's behalf.
`DNSRecord` rows are the editable record set we push to Namecheap
whenever the client (or auto-A-record sync) changes them.

Encryption: the EPP/auth code is encrypted at rest with the same
vault-server-key pattern used elsewhere in the project so a DB leak
doesn't hand over transfer authority.
"""

import uuid

from django.conf import settings
from django.db import models

from core.models import TimestampedModel


# ── Constants ────────────────────────────────────────────────────────────────

TLD_CHOICES = [
    ('com', '.com'),
    ('net', '.net'),
    ('org', '.org'),
    ('law', '.law'),
    ('legal', '.legal'),
    ('attorney', '.attorney'),
]

# TLDs in this set use the "domain-law" ServiceTier ($175/yr); everything
# else uses the "domain-standard" ServiceTier ($75/yr).
#
# All three attorney-niche TLDs (.law, .legal, .attorney) sit on the
# premium tier — they're bought for the same reason (signal "I'm an
# attorney") so they get consistent pricing. Wholesale costs from
# Namecheap also justify it: ~$100/yr .law, ~$45 .legal, ~$50
# .attorney, leaving healthy margin at $175 retail across the board.
PREMIUM_TLDS = frozenset({'law', 'legal', 'attorney'})


def tier_slug_for_tld(tld):
    """Return the ServiceTier slug that prices this TLD."""
    return 'domain-law' if tld in PREMIUM_TLDS else 'domain-standard'


DOMAIN_STATUS_CHOICES = [
    ('pending', 'Pending registration'),
    ('active', 'Active'),
    ('grace', 'In grace period (post-cancel)'),
    ('expired', 'Expired'),
    ('transferred_out', 'Transferred out'),
    ('cancelled', 'Cancelled'),
    ('failed', 'Registration failed'),
]


DNS_RECORD_TYPE_CHOICES = [
    ('A', 'A — IPv4 address'),
    ('AAAA', 'AAAA — IPv6 address'),
    ('CNAME', 'CNAME — alias'),
    ('MX', 'MX — mail exchange'),
    ('TXT', 'TXT — text (SPF/DKIM/verification)'),
    ('URL', 'URL — HTTP 301 redirect'),
    ('URL301', 'URL301 — permanent redirect'),
    ('FRAME', 'FRAME — masked redirect'),
]


# ── Models ───────────────────────────────────────────────────────────────────

class DomainRegistration(TimestampedModel):
    """
    One registered (or pending) domain owned by a client account.

    The Stripe subscription drives renewals (we never use Namecheap's
    own auto-renew flag — `auto_renew_at_registrar` stays False so the
    `invoice.upcoming` webhook is the single source of truth).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    client = models.ForeignKey(
        'clients.ClientProfile',
        on_delete=models.PROTECT,
        related_name='domain_registrations',
    )

    # The full domain name, lower-cased, e.g. "johnsonlawfirm.com".
    domain_name = models.CharField(max_length=253, unique=True)
    # TLD without the leading dot — drives pricing tier resolution.
    tld = models.CharField(max_length=20, choices=TLD_CHOICES)

    status = models.CharField(
        max_length=20, choices=DOMAIN_STATUS_CHOICES, default='pending')

    # Registration timestamps + expiry from Namecheap.
    registered_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    # Privacy + transfer controls. WHOIS privacy is Namecheap's free
    # "Withheld for Privacy" proxy — on by default. Registrar lock is
    # the standard transfer-protection flag; we lift it on cancel.
    whois_privacy_enabled = models.BooleanField(default=True)
    registrar_lock = models.BooleanField(default=True)
    # We control renewals via Stripe — NEVER set Namecheap's own
    # auto-renew flag. Kept here only to mirror what Namecheap reports.
    auto_renew_at_registrar = models.BooleanField(default=False)

    # Nameservers — what Namecheap reports. We default to Namecheap's
    # own nameservers on registration; we never push custom nameservers
    # from this app (DNS management lives at Namecheap so all clients
    # share the same nameservers).
    nameservers = models.JSONField(default=list, blank=True)

    # EPP transfer-out code, encrypted with the vault-server-key.
    # Populated on cancel + emailed to the client. Encrypted with
    # encrypt_value(value, derive_server_key()) — stored as hex string
    # (AES-256-GCM nonce + ciphertext). DB dump alone can't yield the
    # plaintext transfer code.
    epp_code_encrypted = models.TextField(blank=True)
    epp_code_issued_at = models.DateTimeField(null=True, blank=True)

    # Stripe — domain has its OWN subscription, separate from hosting
    # + maintenance. Renewal gating happens in invoice.upcoming.
    stripe_subscription_id = models.CharField(max_length=255, blank=True)
    # The ServiceTier slug that priced this registration. Kept for
    # historical audit even if the tier definition is later edited.
    pricing_tier_slug = models.CharField(max_length=50, blank=True)

    # On registration we auto-create an A record pointing at the
    # client's Droplet IP. This timestamp records when that completed
    # successfully. Re-runs of the sync update it.
    auto_a_record_set_at = models.DateTimeField(null=True, blank=True)

    # Last successful API call to Namecheap for THIS domain (any
    # operation). Diagnostic only.
    last_api_call_at = models.DateTimeField(null=True, blank=True)
    last_api_error = models.TextField(blank=True)

    # Free-form admin notes — visible only to staff.
    internal_notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', 'status']),
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f'{self.domain_name} ({self.get_status_display()})'

    # ── Helpers ──────────────────────────────────────────────────────────

    @property
    def is_active(self):
        return self.status in ('active', 'grace')

    @property
    def display_status(self):
        """Short human label used in client UI."""
        return dict(DOMAIN_STATUS_CHOICES).get(self.status, self.status)

    def decrypt_epp_code(self):
        """Decrypt and return the EPP code, or '' if not yet issued."""
        if not self.epp_code_encrypted:
            return ''
        from vault.crypto import decrypt_value, derive_server_key
        return decrypt_value(self.epp_code_encrypted, derive_server_key())

    def set_epp_code(self, plaintext):
        """Encrypt + store the EPP code. Caller is responsible for save()."""
        from django.utils import timezone

        from vault.crypto import derive_server_key, encrypt_value
        self.epp_code_encrypted = encrypt_value(
            plaintext or '', derive_server_key())
        self.epp_code_issued_at = timezone.now() if plaintext else None


class DNSRecord(TimestampedModel):
    """
    One DNS record for a registered domain.

    We hold the full record set locally so the portal UI can edit it
    without a Namecheap roundtrip per change; on save the set is
    PUSHED in full to Namecheap (their API replaces all records on
    setHosts — there is no per-record update endpoint).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    domain = models.ForeignKey(
        DomainRegistration, on_delete=models.CASCADE,
        related_name='dns_records',
    )

    record_type = models.CharField(
        max_length=8, choices=DNS_RECORD_TYPE_CHOICES)
    # Sub-name — "@" for the apex, "www", "staging", "mail", etc.
    host = models.CharField(max_length=120, default='@')
    # IPv4, hostname, text, or URL depending on record_type.
    value = models.TextField()
    # MX priority — required for MX, ignored for other types. 10 is
    # the conventional default.
    mx_priority = models.IntegerField(default=10)
    ttl = models.IntegerField(default=1800)

    # True for records WE created automatically (auto-A on Droplet
    # provision). Helps the admin tooling distinguish staff-managed
    # records from client edits when reconciling drift.
    auto_managed = models.BooleanField(default=False)

    class Meta:
        ordering = ['host', 'record_type']
        indexes = [
            models.Index(fields=['domain', 'record_type']),
        ]

    def __str__(self):
        return f'{self.host}.{self.domain.domain_name} {self.record_type} {self.value[:40]}'
