"""
Account + Website models — Phase A of the account/website refactor.

Replaces the single ``ClientProfile`` model with a two-level structure:

  Account  — the login layer. One human (or firm) = one Account =
             one Stripe customer = one WHOIS contact = one vault.
             Owns: billing customer ID, vault PIN, address (WHOIS),
             support tickets, referral link, domain registrations.

  Website  — one per build. An Account may have many.
             Owns: stage, intake, droplet, hosting + maintenance
             subscriptions, scans, monthly reports, intelligence
             reports, etc.

During Phase A both models are added alongside the legacy
``ClientProfile`` / ``Project`` rows; every dependent model gains a
nullable ``account_new`` or ``website_new`` FK. Phase B backfills the
new rows from existing data. Phase C switches readers + writers.
Phase D drops the legacy columns + models.

Per-subscription payment-method override is handled by
``SubscriptionPaymentMethod`` — Stripe supports setting
``default_payment_method`` on each Subscription independently of the
Customer-level default, so a client can route hosting to one card and
maintenance to another. A row here = "this subscription is pinned to
this PM"; absence = "use account default".
"""

import re
import uuid

from django.conf import settings
from django.db import models

from core.models import TimestampedModel


# Reused on Website — same project stage choices, kept here to avoid an
# import cycle with clients.models.
WEBSITE_STAGE_CHOICES = [
    ('intake', 'Intake'),
    ('structure', 'Structure'),
    ('design', 'Design'),
    ('content', 'Content'),
    ('review', 'Review'),
    ('revisions', 'Revisions'),
    ('pre_launch', 'Pre-Launch'),
    ('live', 'Live'),
]


def _slugify_unique(base, model_cls, field='slug'):
    """
    Slugify ``base`` and append ``-2``, ``-3``, ... until the result is
    unique on ``model_cls.<field>``. Caller is responsible for the save.

    Defensive: if ``base`` slugifies to empty (e.g. only punctuation),
    falls back to ``'website'`` so we never return ''.
    """
    cleaned = re.sub(r'[^a-z0-9]+', '-', (base or '').lower()).strip('-')
    cleaned = cleaned[:60] or 'website'
    candidate = cleaned
    n = 2
    while model_cls.objects.filter(**{field: candidate}).exists():
        suffix = f'-{n}'
        candidate = (cleaned[:60 - len(suffix)] + suffix)
        n += 1
        if n > 9999:  # paranoia guard
            candidate = f'{cleaned[:50]}-{uuid.uuid4().hex[:8]}'
            break
    return candidate


# ── Account ─────────────────────────────────────────────────────────────────

class Account(TimestampedModel):
    """
    A client account — the login layer. One ``User`` → one ``Account``.
    The Account holds everything that belongs to the human / firm
    rather than to any single build: WHOIS contact info, billing
    Stripe customer ID, vault PIN, communication preferences, domains.
    """

    STATUS_CHOICES = [
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('archived', 'Archived'),
    ]
    CONTACT_METHOD_CHOICES = [
        ('email', 'Email'),
        ('phone', 'Phone'),
        ('text', 'Text'),
    ]
    # Account-level onboarding is intentionally small — WHOIS contact +
    # vault PIN. Anything build-specific lives on ``Website``.
    ONBOARDING_STATUS_CHOICES = [
        ('pending_setup', 'Pending Account Setup'),
        ('complete', 'Complete'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='account',
    )

    # Identity — the account holder, NOT the firm. Per the new model,
    # the firm/brand name lives on each Website. An Account named
    # "Jane Smith" can have two Websites named "Smith Family Law" and
    # "Smith Mediation Services".
    name = models.CharField(
        max_length=200,
        help_text='Account holder name (person or organisation).',
    )
    contact_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email_alt = models.EmailField(
        blank=True,
        help_text='Optional billing email — defaults to user.email.',
    )

    # ── WHOIS / mailing address (drives domain registration + invoices) ──
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=50, blank=True)
    zip_code = models.CharField(max_length=10, blank=True)
    country = models.CharField(max_length=2, default='US')

    # ── Account-level status ──
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='active')
    is_tester = models.BooleanField(
        default=False,
        help_text='True for Aspired internal test accounts.',
    )
    internal_notes = models.TextField(
        blank=True, help_text='Staff only — never shown to the client.',
    )

    # ── Stripe — one customer per Account, all websites bill under it ──
    stripe_customer_id = models.CharField(
        max_length=255, blank=True, db_index=True)

    # ── Account-level communication prefs ──
    preferred_contact_method = models.CharField(
        max_length=10, choices=CONTACT_METHOD_CHOICES, default='email',
    )
    notify_on_stage_change = models.BooleanField(default=True)
    notify_on_invoice = models.BooleanField(default=True)
    notify_on_scan_complete = models.BooleanField(default=True)

    # ── Onboarding (account-level — done once) ──
    onboarding_status = models.CharField(
        max_length=20,
        choices=ONBOARDING_STATUS_CHOICES,
        default='complete',
        help_text=(
            'Gate for the account setup page (WHOIS contact + vault '
            'PIN). Per-website intake state lives on Website.'),
    )
    onboarding_complete = models.BooleanField(default=False)

    # ── Vault PIN (one PIN unlocks all websites' creds for this account) ──
    client_pin_hash = models.CharField(max_length=256, blank=True)
    client_pin_salt = models.BinaryField(
        max_length=32, null=True, blank=True)
    client_pin_set = models.BooleanField(default=False)
    client_pin_failed_attempts = models.IntegerField(default=0)
    client_pin_lockout_until = models.DateTimeField(null=True, blank=True)

    # ── Moonieful sync (account-level — Miki refers a PERSON) ──
    moonieful_client_id = models.UUIDField(
        null=True, blank=True, unique=True)
    synced_from_moonieful = models.BooleanField(default=False)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    sync_conflict_flagged = models.BooleanField(default=False)

    # ── Legacy ClientProfile reference — kept during Phase B/C so
    # the backfill is reversible. Dropped in Phase D.
    legacy_client_profile = models.OneToOneField(
        'clients.ClientProfile',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='migrated_account',
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Account'
        verbose_name_plural = 'Accounts'

    def __str__(self):
        return self.name or (self.user.email if self.user_id else '(no name)')


# ── Website ─────────────────────────────────────────────────────────────────

class Website(TimestampedModel):
    """
    One website build owned by an Account. The bulk of per-build
    state — stage, droplet, hosting + maintenance subscriptions,
    revisions, scans, reports — hangs off this model.

    ``slug`` is globally unique (across all accounts) so portal URLs
    don't have to encode the account ID. Slug collisions auto-append
    ``-2``, ``-3``, …
    """

    STATUS_CHOICES = [
        ('active', 'Active'),
        ('paused', 'Paused'),
        ('archived', 'Archived'),
    ]
    PACKAGE_CHOICES = [
        ('essential_build', 'Essential Website Build'),
        ('premium_build', 'Premium Website Build'),
        ('maintenance_essentials', 'Maintenance — Essentials'),
        ('maintenance_growth', 'Maintenance — Growth'),
        ('maintenance_dominant', 'Maintenance — Dominant'),
        ('moonieful_referred', 'Moonieful Referred'),
    ]
    PAYMENT_STATUS_CHOICES = [
        ('awaiting_deposit', 'Awaiting Deposit'),
        ('deposit_paid', 'Deposit Paid'),
        ('fully_paid', 'Fully Paid'),
    ]
    # Website-level onboarding tracks the build-specific intake form
    # rather than account-level WHOIS setup.
    ONBOARDING_STATUS_CHOICES = [
        ('pending_intake', 'Pending Intake'),
        ('intake_complete', 'Intake Complete'),
        ('complete', 'Complete'),
    ]

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE, related_name='websites',
    )

    # Identity
    name = models.CharField(
        max_length=200,
        help_text='Business / brand name for this website.',
    )
    slug = models.SlugField(
        max_length=80, unique=True,
        help_text='URL identifier — appears in /portal/<slug>/.',
    )
    business_type = models.CharField(
        max_length=100, blank=True,
        help_text='Blank for Moonieful-synced clients.',
    )

    # URLs (single source of truth — no more website/live_url split)
    url = models.URLField(blank=True, help_text='Live URL.')
    staging_url = models.URLField(blank=True)

    # Lifecycle
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='active')
    stage = models.CharField(
        max_length=20, choices=WEBSITE_STAGE_CHOICES, default='intake')
    package = models.CharField(
        max_length=30, choices=PACKAGE_CHOICES, blank=True)

    # ── Per-website onboarding (intake form gate) ──
    onboarding_status = models.CharField(
        max_length=20,
        choices=ONBOARDING_STATUS_CHOICES,
        default='pending_intake',
    )

    # ── DigitalOcean Droplet (one per Website) ──
    do_droplet_id = models.CharField(max_length=50, blank=True)
    do_droplet_ip = models.GenericIPAddressField(null=True, blank=True)
    do_droplet_created_at = models.DateTimeField(null=True, blank=True)
    do_droplet_name = models.CharField(max_length=120, blank=True)

    # ── Launch + support window ──
    launch_date = models.DateField(null=True, blank=True)
    support_window_ends = models.DateField(
        null=True, blank=True, help_text='Launch date + 14 days.',
    )

    # ── Payment state (per-build) ──
    payment_status = models.CharField(
        max_length=20, choices=PAYMENT_STATUS_CHOICES,
        default='awaiting_deposit',
    )
    deposit_paid_at = models.DateTimeField(null=True, blank=True)
    final_paid_at = models.DateTimeField(null=True, blank=True)

    # ── Revisions (per-build, resettable) ──
    revision_count = models.PositiveIntegerField(default=0)
    revision_limit = models.PositiveIntegerField(default=2)
    revisions_reset_at = models.DateTimeField(null=True, blank=True)

    # ── Moonieful (per-build — Miki hands off one site at a time) ──
    moonieful_referred = models.BooleanField(default=False)
    moonieful_handoff_at = models.DateTimeField(null=True, blank=True)
    moonieful_stage_history = models.JSONField(default=list, blank=True)
    moonieful_package = models.CharField(max_length=100, blank=True)
    handoff_followup_sent = models.JSONField(default=dict, blank=True)
    maintenance_upsell_log = models.JSONField(default=dict, blank=True)

    # ── Stripe — one customer (Account) bills multiple subs ──
    # Each Subscription's metadata carries website_id so webhook
    # handlers can route the event back to the right Website.
    stripe_hosting_subscription_id = models.CharField(
        max_length=255, blank=True)
    stripe_maintenance_subscription_id = models.CharField(
        max_length=255, blank=True)
    stripe_invoice_id = models.CharField(
        max_length=255, blank=True,
        help_text='One-time onboarding invoice ID for this build.',
    )

    # ── Maintenance state ──
    maintenance_active = models.BooleanField(default=False)
    maintenance_started_at = models.DateTimeField(null=True, blank=True)
    maintenance_cancelled_at = models.DateTimeField(null=True, blank=True)

    # ── Features ──
    session_recording_enabled = models.BooleanField(default=False)
    auto_send_scan_reports = models.BooleanField(default=False)

    # ── Admin review queue ──
    needs_admin_review_at = models.DateTimeField(null=True, blank=True)
    admin_reviewed_at = models.DateTimeField(null=True, blank=True)

    # ── Testimonials (per-site) ──
    testimonial_requested_at = models.DateTimeField(null=True, blank=True)
    testimonial_received = models.BooleanField(default=False)
    testimonial_url = models.URLField(blank=True)

    # ── Legacy Project reference — kept during Phase B/C so the
    # backfill is reversible. Dropped in Phase D.
    legacy_project = models.OneToOneField(
        'clients.Project',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='migrated_website',
    )

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Website'
        verbose_name_plural = 'Websites'
        indexes = [
            models.Index(fields=['account', 'stage']),
            models.Index(fields=['stage', '-created_at']),
        ]

    def __str__(self):
        return f'{self.name} ({self.get_stage_display()})'

    # ── Helpers (parity with the old Project / ClientProfile API) ──

    @property
    def revisions_remaining(self):
        return max(self.revision_limit - self.revision_count, 0)

    @property
    def over_revision_limit(self):
        return self.revision_count > self.revision_limit

    def reset_revisions(self, *, save=True):
        """Reset the revision counter back to zero (mini-redesign etc.)."""
        from django.utils import timezone as _tz
        self.revision_count = 0
        self.revisions_reset_at = _tz.now()
        if save:
            self.save(update_fields=[
                'revision_count', 'revisions_reset_at', 'updated_at'])

    # Backward-compat alias for code that still reads `.live_url`
    # (templates, emails) during the transition.
    @property
    def live_url(self):
        return self.url or ''

    # Convenience — slug autogeneration on save.
    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _slugify_unique(self.name, Website)
        super().save(*args, **kwargs)


# ── WebsiteStageLog ─────────────────────────────────────────────────────────

class WebsiteStageLog(TimestampedModel):
    """
    Append-only record of every Website stage transition. Direct
    replacement for ``ProjectStageLog`` — same shape, FK to Website
    instead of Project / ClientProfile.
    """

    website = models.ForeignKey(
        Website, on_delete=models.CASCADE,
        related_name='stage_logs',
    )
    from_stage = models.CharField(max_length=20, blank=True)
    to_stage = models.CharField(max_length=20, blank=True)
    note = models.TextField(blank=True)
    set_by = models.CharField(
        max_length=255, blank=True,
        help_text='Who triggered this transition (staff name, "system", "sync").',
    )
    client_notified = models.BooleanField(default=False)
    notification_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Website Stage Log'
        verbose_name_plural = 'Website Stage Logs'

    def __str__(self):
        return f'{self.website.name}: {self.from_stage} → {self.to_stage}'


# ── SubscriptionPaymentMethod ───────────────────────────────────────────────

class SubscriptionPaymentMethod(TimestampedModel):
    """
    Override which payment method a specific Stripe Subscription uses.

    Stripe natively supports per-subscription ``default_payment_method``
    that takes precedence over the Customer-level default. This model
    is our local mirror of that mapping — one row per pinned
    subscription. Absence of a row = "use account default" (which is
    Customer.invoice_settings.default_payment_method).

    The portal renders a dropdown per subscription on the Payment
    Methods page: "Default" + each saved card → the selection is
    pushed to Stripe via stripe.Subscription.modify() AND stored here.
    """

    KIND_CHOICES = [
        ('hosting', 'Annual Hosting'),
        ('maintenance', 'Monthly Maintenance'),
        ('domain', 'Domain Registration'),
        ('other', 'Other'),
    ]

    account = models.ForeignKey(
        Account, on_delete=models.CASCADE,
        related_name='subscription_payment_methods',
    )
    # The Stripe Subscription this override applies to. Unique so a
    # subscription has at most one pinned card row.
    stripe_subscription_id = models.CharField(
        max_length=255, unique=True, db_index=True)
    # The Stripe PaymentMethod ID (pm_xxx) to charge. Blank = use the
    # account default. We keep the row anyway so the UI knows the
    # user has "actively chosen default" vs "never picked".
    payment_method_id = models.CharField(max_length=255, blank=True)
    # For display only — what kind of subscription this is.
    kind = models.CharField(
        max_length=20, choices=KIND_CHOICES, default='other')
    # Free-text label rendered alongside the card pick (e.g. the
    # Website name, the domain name). Set by the writer; not the
    # source of truth.
    display_label = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Subscription Payment Method'
        verbose_name_plural = 'Subscription Payment Methods'
        indexes = [
            models.Index(fields=['account', 'kind']),
        ]

    def __str__(self):
        pm = self.payment_method_id or 'DEFAULT'
        return f'{self.account.name} — {self.kind} — {pm}'
