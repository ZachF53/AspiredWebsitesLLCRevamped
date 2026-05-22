"""
Client portal data models — Phase 3.

Every model inherits TimestampedModel (UUID primary key) per CLAUDE.md, so
Aspired and Moonieful record IDs never collide across the sync bridge.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import TimestampedModel


# ── Shared choice sets ───────────────────────────────────────────────────────

PROJECT_STAGES = [
    ('intake', 'Intake'),
    ('structure', 'Structure'),
    ('design', 'Design'),
    ('content', 'Content'),
    ('review', 'Review'),
    ('revisions', 'Revisions'),
    ('pre_launch', 'Pre-Launch'),
    ('live', 'Live'),
]

BUILD_PACKAGE_CHOICES = [
    ('essential_build', 'Essential Website Build'),
    ('premium_build', 'Premium Website Build'),
]


def client_document_path(instance, filename):
    """Upload path: portal/clients/<client_id>/docs/<filename>."""
    return f'portal/clients/{instance.client_id}/docs/{filename}'


# ── Models ───────────────────────────────────────────────────────────────────

class ClientProfile(TimestampedModel):
    """A paying (or onboarding) client. One-to-one with a Django User."""

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
    CONTACT_METHOD_CHOICES = [
        ('email', 'Email'),
        ('phone', 'Phone'),
        ('text', 'Text'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='client_profile',
    )
    firm_name = models.CharField(max_length=255)
    contact_name = models.CharField(max_length=255, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    website = models.URLField(blank=True)
    business_type = models.CharField(
        max_length=100,
        blank=True,
        help_text='Blank for Moonieful-synced clients — never the Law Firm default.',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    package = models.CharField(max_length=30, choices=PACKAGE_CHOICES, blank=True)

    # ── Moonieful sync ──
    moonieful_client_id = models.UUIDField(null=True, blank=True, unique=True)
    synced_from_moonieful = models.BooleanField(default=False)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    sync_conflict_flagged = models.BooleanField(default=False)
    moonieful_package = models.CharField(
        max_length=100, blank=True, help_text='The package Miki sold them.',
    )
    # Tracks which handoff follow-up emails have been sent, e.g.
    # {"day3": "2026-05-20T...", "day7": "..."}.
    handoff_followup_sent = models.JSONField(default=dict, blank=True)

    # ── Stripe / maintenance ──
    stripe_customer_id = models.CharField(max_length=255, blank=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True)
    maintenance_active = models.BooleanField(default=False)
    maintenance_started_at = models.DateTimeField(null=True, blank=True)

    # ── DigitalOcean Droplet (one per client) ──
    do_droplet_id = models.CharField(max_length=50, blank=True)
    do_droplet_ip = models.GenericIPAddressField(null=True, blank=True)
    do_droplet_created_at = models.DateTimeField(null=True, blank=True)

    # ── Client-editable preferences (portal settings page) ──
    preferred_contact_method = models.CharField(
        max_length=10, choices=CONTACT_METHOD_CHOICES, default='email',
    )
    notify_on_stage_change = models.BooleanField(default=True)

    internal_notes = models.TextField(
        blank=True, help_text='Staff only — never shown to the client.',
    )
    onboarding_complete = models.BooleanField(default=False)

    # ── Client credentials vault (portal /credentials/ PIN gate) ──
    # A per-client 4-digit PIN, entirely separate from the admin vault PIN.
    # It is a pure access gate — only a verification hash is stored and it
    # derives no encryption key, so a forgotten PIN means no data loss
    # (staff can clear these fields to reset it).
    client_pin_hash = models.CharField(max_length=256, blank=True)
    client_pin_salt = models.BinaryField(max_length=32, null=True, blank=True)
    client_pin_set = models.BooleanField(default=False)
    client_pin_failed_attempts = models.IntegerField(default=0)
    client_pin_lockout_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Client Profile'
        verbose_name_plural = 'Client Profiles'

    def __str__(self):
        return self.firm_name


class Project(TimestampedModel):
    """A single website build for a client. One client may have several."""

    PAYMENT_STATUS_CHOICES = [
        ('awaiting_deposit', 'Awaiting Deposit'),
        ('deposit_paid', 'Deposit Paid'),
        ('fully_paid', 'Fully Paid'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE, related_name='projects',
    )
    stage = models.CharField(max_length=20, choices=PROJECT_STAGES, default='intake')
    package = models.CharField(max_length=20, choices=BUILD_PACKAGE_CHOICES, blank=True)
    staging_url = models.URLField(blank=True)
    live_url = models.URLField(blank=True)
    launch_date = models.DateField(null=True, blank=True)
    support_window_ends = models.DateField(
        null=True, blank=True, help_text='Launch date + 14 days.',
    )
    payment_status = models.CharField(
        max_length=20, choices=PAYMENT_STATUS_CHOICES, default='awaiting_deposit',
    )
    deposit_paid_at = models.DateTimeField(null=True, blank=True)
    final_paid_at = models.DateTimeField(null=True, blank=True)
    revision_count = models.PositiveIntegerField(default=0)
    revision_limit = models.PositiveIntegerField(default=2)

    # ── Moonieful sync ──
    moonieful_referred = models.BooleanField(default=False)
    moonieful_handoff_at = models.DateTimeField(null=True, blank=True)
    moonieful_stage_history = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.client.firm_name} — {self.get_stage_display()}'

    @property
    def revisions_remaining(self):
        return max(self.revision_limit - self.revision_count, 0)

    @property
    def over_revision_limit(self):
        return self.revision_count > self.revision_limit


class ProjectStageLog(TimestampedModel):
    """An append-only record of every project stage transition."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='stage_logs',
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
        verbose_name = 'Project Stage Log'
        verbose_name_plural = 'Project Stage Logs'

    def __str__(self):
        return f'{self.project.client.firm_name}: {self.from_stage} → {self.to_stage}'


class IntakeResponse(TimestampedModel):
    """The client's intake questionnaire answers for a project."""

    REGISTRAR_CHOICES = [
        ('namecheap', 'Namecheap'),
        ('godaddy', 'GoDaddy'),
        ('google_domains', 'Google Domains'),
        ('cloudflare', 'Cloudflare'),
        ('other', 'Other'),
    ]

    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name='intake',
    )
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Step 1 — Brand
    brand_colors = models.CharField(max_length=255, blank=True)
    brand_fonts = models.CharField(max_length=255, blank=True)
    logo = models.FileField(upload_to='portal/intake/logos/', null=True, blank=True)

    # Step 2 — Photos
    photos_provided = models.BooleanField(default=False)
    photos_note = models.TextField(blank=True)

    # Step 3 — Website copy
    about_copy = models.TextField(blank=True)
    practice_areas = models.TextField(blank=True)
    attorney_bios = models.TextField(blank=True)

    # Step 4 — References
    reference_sites = models.TextField(blank=True)
    competitors = models.TextField(blank=True)

    # Step 5 — Domain & access
    domain_name = models.CharField(max_length=255, blank=True)
    domain_registrar = models.CharField(
        max_length=30, choices=REGISTRAR_CHOICES, blank=True,
    )
    google_business_access = models.BooleanField(default=False)
    social_links = models.TextField(blank=True)

    # SOURCE OF TRUTH for Moonieful-synced clients — typed fields above are
    # for direct Aspired clients only.
    moonieful_intake_raw = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Intake Response'
        verbose_name_plural = 'Intake Responses'

    def __str__(self):
        return f'Intake — {self.project.client.firm_name}'


class RevisionRequest(TimestampedModel):
    """A change request submitted against a project."""

    SOURCE_CHOICES = [
        ('aspired_portal', 'Aspired Portal'),
        ('moonieful_portal', 'Moonieful Portal'),
        ('email', 'Email'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('complete', 'Complete'),
        ('out_of_scope', 'Out of Scope'),
    ]

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='revisions',
    )
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default='aspired_portal',
    )
    description = models.TextField()
    is_major = models.BooleanField(default=True)
    counts_against_limit = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    moonieful_revision_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Revision Request'
        verbose_name_plural = 'Revision Requests'

    def __str__(self):
        return f'{self.project.client.firm_name}: {self.description[:50]}'


class ClientDocument(TimestampedModel):
    """A file exchanged between Aspired and a client (either direction)."""

    DIRECTION_CHOICES = [
        ('to_client', 'To Client'),
        ('from_client', 'From Client'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE, related_name='documents',
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, blank=True,
        related_name='documents',
    )
    direction = models.CharField(max_length=20, choices=DIRECTION_CHOICES)
    file = models.FileField(upload_to=client_document_path)
    label = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='uploaded_documents',
    )
    moonieful_document_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Client Document'
        verbose_name_plural = 'Client Documents'

    def __str__(self):
        return self.label or self.file.name


class SupportTicket(TimestampedModel):
    """A client-raised support request."""

    PRIORITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ]
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('in_progress', 'In Progress'),
        ('resolved', 'Resolved'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE, related_name='tickets',
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, blank=True,
        related_name='tickets',
    )
    subject = models.CharField(max_length=255)
    description = models.TextField()
    priority = models.CharField(
        max_length=10, choices=PRIORITY_CHOICES, default='medium',
    )
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='open')
    resolved_at = models.DateTimeField(null=True, blank=True)
    billable = models.BooleanField(default=False)
    hours_spent = models.DecimalField(max_digits=6, decimal_places=2, default=0)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Support Ticket'
        verbose_name_plural = 'Support Tickets'

    def __str__(self):
        return f'{self.client.firm_name}: {self.subject}'


class Contract(TimestampedModel):
    """
    A website-build contract for a client. The first step of onboarding:
    generated by staff, signed by the client via an unguessable token URL.
    """

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE, related_name='contracts',
    )
    package = models.CharField(max_length=20, choices=BUILD_PACKAGE_CHOICES)
    build_price = models.DecimalField(max_digits=10, decimal_places=2)
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2)
    timeline_weeks = models.IntegerField(default=4)
    contract_text = models.TextField()
    signed = models.BooleanField(default=False)
    signed_at = models.DateTimeField(null=True, blank=True)
    signed_ip = models.GenericIPAddressField(null=True, blank=True)
    signed_name = models.CharField(max_length=200, blank=True)
    contract_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    pdf_path = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Contract — {self.client.firm_name} ({self.get_package_display()})'

    @property
    def final_amount(self):
        return self.build_price - self.deposit_amount


class SiteChangelogEntry(TimestampedModel):
    """
    A single logged change to a client's live website. Surfaced in the
    client portal Activity Log unless flagged internal-only.
    """

    CHANGE_TYPE_CHOICES = [
        ('page_added', 'Page Added'),
        ('page_updated', 'Page Updated'),
        ('security_patch', 'Security Patch'),
        ('dependency_update', 'Dependency Update'),
        ('blog_published', 'Blog Post Published'),
        ('image_optimization', 'Image Optimization'),
        ('seo_update', 'SEO Update'),
        ('bug_fix', 'Bug Fix'),
        ('performance', 'Performance Improvement'),
        ('deployment', 'Deployment'),
        ('content_update', 'Content Update'),
        ('other', 'Other'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='changelog_entries',
    )
    change_type = models.CharField(
        max_length=20, choices=CHANGE_TYPE_CHOICES, default='other',
    )
    title = models.CharField(
        max_length=200,
        help_text='Short summary shown as the entry label.',
    )
    description = models.TextField(
        blank=True,
        help_text='Optional longer explanation shown on expand.',
    )
    url_changed = models.URLField(
        blank=True,
        help_text='Optional — the specific page that was changed.',
    )
    is_client_visible = models.BooleanField(
        default=True,
        help_text='Untick to keep this entry internal — never shown to the client.',
    )
    date_of_change = models.DateField(
        default=timezone.localdate,
        help_text='Defaults to today; can be backdated.',
    )

    class Meta:
        ordering = ['-date_of_change', '-created_at']
        verbose_name = 'Site Changelog Entry'
        verbose_name_plural = 'Site Changelog Entries'

    def __str__(self):
        return (f'{self.client.firm_name} — '
                f'{self.get_change_type_display()} — '
                f'{self.date_of_change}')

