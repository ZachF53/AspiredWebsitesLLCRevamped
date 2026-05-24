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
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=50, blank=True)
    zip_code = models.CharField(max_length=10, blank=True)
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

    # ── Video testimonial request (one-time, ~30 days post-launch) ──
    testimonial_requested_at = models.DateTimeField(null=True, blank=True)
    testimonial_received = models.BooleanField(default=False)
    testimonial_url = models.URLField(blank=True)

    # ── Security scan delivery preferences (Phase 6c Part 3) ──
    # When True, completed scans auto-email the PDF report to the client
    # via SendGrid; when False, an admin gets a Needs You alert instead
    # and decides per-scan whether to send it.
    auto_send_scan_reports = models.BooleanField(default=False)

    # ── Tier 2 session-recording addon (Phase 7) ──
    # Free on Growth + Dominant maintenance plans; $50/mo addon on
    # Essentials. When True the Tier 2 recorder script tag is shown
    # in the snippet generator. False = Tier 1 analytics only.
    session_recording_enabled = models.BooleanField(default=False)

    # ── Internal classification ──
    # True for Aspired's own test / dev properties (Aspired AI, Food
    # Trucks, etc.) so they can be excluded from external dashboards,
    # billing summaries, NPS rotations, and scheduled report emails.
    # Replaces the freeform "Tester: True" line previously stored in
    # `internal_notes` by the legacy seed command.
    is_tester = models.BooleanField(default=False)

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


class UptimeRecord(TimestampedModel):
    """A single uptime check result for a client's live site."""

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='uptime_records',
    )
    checked_at = models.DateTimeField(auto_now_add=True)
    response_time_ms = models.IntegerField(null=True, blank=True)
    status_code = models.IntegerField(null=True, blank=True)
    is_up = models.BooleanField(default=True)
    error_message = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-checked_at']
        indexes = [
            models.Index(fields=['client', 'checked_at']),
        ]

    def __str__(self):
        status = 'UP' if self.is_up else 'DOWN'
        return f'{self.client.firm_name} — {status} — {self.checked_at}'


class UptimeAlert(TimestampedModel):
    """An open / resolved downtime incident — one per outage, no spam."""

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='uptime_alerts',
    )
    alerted_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.IntegerField(default=3)
    is_resolved = models.BooleanField(default=False)
    alert_sent = models.BooleanField(default=False)

    class Meta:
        ordering = ['-alerted_at']

    def __str__(self):
        status = 'Resolved' if self.is_resolved else 'Active'
        return f'{self.client.firm_name} — DOWN — {status}'



# ── Phase 7 Part 1 — Business Intelligence ─────────────────────────────────

class RevenueSnapshot(TimestampedModel):
    """
    Monthly revenue snapshot — captured by the Celery beat on the 1st
    of each month so we have a real history table to plot the MRR
    trend chart against, rather than recalculating from scratch every
    page render.

    `mrr_total` is the source of truth for THAT month; `mrr_new` and
    `mrr_churned` are derived by comparing against the previous
    snapshot at write time.
    """

    snapshot_month = models.DateField(
        unique=True,
        help_text='First day of the month, e.g. 2026-05-01.',
    )

    mrr_total = models.DecimalField(
        max_digits=10, decimal_places=2, default=0)
    mrr_new = models.DecimalField(
        max_digits=10, decimal_places=2, default=0)
    mrr_churned = models.DecimalField(
        max_digits=10, decimal_places=2, default=0)
    mrr_net_change = models.DecimalField(
        max_digits=10, decimal_places=2, default=0)

    active_maintenance_clients = models.IntegerField(default=0)
    active_project_clients = models.IntegerField(default=0)
    pipeline_value = models.DecimalField(
        max_digits=10, decimal_places=2, default=0)
    one_time_revenue = models.DecimalField(
        max_digits=10, decimal_places=2, default=0)

    class Meta:
        ordering = ['-snapshot_month']
        verbose_name = 'Revenue Snapshot'
        verbose_name_plural = 'Revenue Snapshots'

    def __str__(self):
        return (f'Revenue snapshot — '
                f'{self.snapshot_month.strftime("%B %Y")}')


class ClientHealthScore(TimestampedModel):
    """
    Daily health score per client. One row per calculation so we keep
    a history (the dashboard plots trends; the churn-alert task
    de-duplicates on the most recent row). Recalculated by the Celery
    beat at 06:00 every day.

    Score weights:
      Payment 30 · Engagement 20 · NPS 20 · Uptime 20 · Support 10
    """

    HEALTH_CHOICES = [
        ('healthy', 'Healthy'),     # score >= 70
        ('at_risk', 'At Risk'),     # 40 <= score < 70
        ('critical', 'Critical'),   # score < 40
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='health_scores',
    )
    calculated_at = models.DateTimeField(auto_now_add=True)

    # Overall (0-100). Component scores are also kept so the dashboard
    # can show the per-axis mini-bars without recalculating.
    score = models.IntegerField(default=0)
    payment_score = models.IntegerField(default=0)
    engagement_score = models.IntegerField(default=0)
    nps_score_component = models.IntegerField(default=0)
    uptime_score = models.IntegerField(default=0)
    support_score = models.IntegerField(default=0)

    health_status = models.CharField(
        max_length=10, choices=HEALTH_CHOICES, default='healthy')

    # True iff health_status == 'critical' or payment_score == 0.
    # Used by the churn-risk Celery alert + the Intelligence dashboard
    # banner.
    churn_risk = models.BooleanField(default=False)

    class Meta:
        ordering = ['-calculated_at']
        verbose_name = 'Client Health Score'
        verbose_name_plural = 'Client Health Scores'
        indexes = [
            models.Index(fields=['client', '-calculated_at']),
            models.Index(fields=['health_status', '-calculated_at']),
        ]

    def __str__(self):
        return (f'{self.client.firm_name} — '
                f'Health: {self.score}/100')


# ── Phase 7 Part 2 — Referrals · Proposals · Case Studies ───────────────────

def generate_referral_code(firm_name):
    """
    Build a short unique referral code from a firm name + 2-digit year.
    Caller must import `ReferralLink` lazily — circular import otherwise.

    Returns something like ``BERMEA26`` (first 6 alpha-num chars +
    YY). Appends a single digit before the year if the base collides
    (``BERME126``, ``BERME226`` …) so we never block on a popular name.
    """
    import re
    clean = re.sub(r'[^A-Z0-9]', '', (firm_name or '').upper()) or 'CLIENT'
    year_suffix = str(timezone.now().year)[-2:]

    base = clean[:6] + year_suffix
    code = base
    counter = 1
    while ReferralLink.objects.filter(code=code).exists():
        # Drop one char off the firm-name portion so the suffix fits in
        # the same 8-ish chars. Cap iterations so we never spin forever.
        code = clean[:5] + str(counter) + year_suffix
        counter += 1
        if counter > 99:
            # Last-resort UUID tail — guaranteed unique, ugly but rare.
            code = (clean[:4] + uuid.uuid4().hex[:4].upper())[:20]
            break
    return code


class ReferralLink(TimestampedModel):
    """
    One referral link per client. The portal renders this; the public
    ``/ref/<code>/`` view counts clicks and drops a referral_code on
    any contact-form lead created in the same session.
    """

    client = models.OneToOneField(
        ClientProfile, on_delete=models.CASCADE,
        related_name='referral_link',
    )
    code = models.CharField(max_length=20, unique=True)
    clicks = models.IntegerField(default=0)
    leads_generated = models.IntegerField(default=0)
    conversions = models.IntegerField(default=0)
    total_reward_value = models.DecimalField(
        max_digits=8, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['-conversions', '-leads_generated']
        verbose_name = 'Referral Link'
        verbose_name_plural = 'Referral Links'

    def __str__(self):
        return f'{self.client.firm_name} — ref/{self.code}'

    def get_referral_url(self):
        return f'https://aspiredwebsites.com/ref/{self.code}/'


class ReferralEvent(TimestampedModel):
    """A single click, lead, or conversion attributed to a ReferralLink."""

    EVENT_CHOICES = [
        ('click', 'Link Click'),
        ('lead', 'Lead Created'),
        ('conversion', 'Client Converted'),
    ]

    referral_link = models.ForeignKey(
        ReferralLink, on_delete=models.CASCADE, related_name='events',
    )
    event_type = models.CharField(
        max_length=15, choices=EVENT_CHOICES)

    lead = models.ForeignKey(
        'outreach.Lead', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='referral_events',
    )

    # SHA-256 of the visitor's IP — used to de-duplicate clicks inside
    # a 24-hour window. We never store the raw IP per CLAUDE.md privacy.
    ip_hash = models.CharField(max_length=64, blank=True)

    reward_given = models.BooleanField(default=False)
    reward_amount = models.DecimalField(
        max_digits=8, decimal_places=2, default=0)
    reward_note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Referral Event'
        verbose_name_plural = 'Referral Events'
        indexes = [
            models.Index(fields=['referral_link', '-created_at']),
            models.Index(fields=['event_type', '-created_at']),
        ]

    def __str__(self):
        return (f'{self.referral_link.client.firm_name} — '
                f'{self.event_type}')


class Proposal(TimestampedModel):
    """
    Branded sales proposal. Generates a WeasyPrint PDF and tracks
    open / accept signals via a UUID `tracking_token`.
    """

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('sent', 'Sent'),
        ('viewed', 'Viewed'),
        ('accepted', 'Accepted'),
        ('declined', 'Declined'),
        ('expired', 'Expired'),
    ]

    # Optional Lead link — proposals can be cold (no lead row yet).
    lead = models.ForeignKey(
        'outreach.Lead', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='proposals',
    )

    prospect_name = models.CharField(max_length=200)
    prospect_email = models.EmailField(blank=True)
    prospect_business = models.CharField(max_length=200, blank=True)
    prospect_city = models.CharField(max_length=100, blank=True)
    prospect_state = models.CharField(max_length=50, blank=True)

    package = models.CharField(max_length=100, blank=True)
    project_price = models.DecimalField(
        max_digits=8, decimal_places=2, default=0)
    maintenance_price = models.DecimalField(
        max_digits=8, decimal_places=2, default=0)

    goals = models.TextField(blank=True)
    pain_points = models.TextField(blank=True)

    # JSON list of CaseStudy UUIDs (string form) to render on Page 5.
    case_study_ids = models.JSONField(default=list, blank=True)

    pdf_path = models.CharField(max_length=500, blank=True)

    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default='draft')
    sent_at = models.DateTimeField(null=True, blank=True)
    viewed_at = models.DateTimeField(null=True, blank=True)
    view_count = models.IntegerField(default=0)

    tracking_token = models.UUIDField(
        default=uuid.uuid4, unique=True, editable=False)

    expires_at = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Proposal'
        verbose_name_plural = 'Proposals'
        indexes = [
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self):
        return f'Proposal — {self.prospect_name} — {self.status}'

    def get_tracking_url(self):
        return (f'https://aspiredwebsites.com'
                f'/proposals/view/{self.tracking_token}/')

    def is_expired(self):
        return bool(self.expires_at
                    and self.expires_at < timezone.now().date())


class CaseStudy(TimestampedModel):
    """
    Client success story. Renders into proposals and (when
    `is_published`) onto the public portfolio page.
    """

    client = models.ForeignKey(
        ClientProfile, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='case_studies',
    )

    title = models.CharField(max_length=300)
    business_type = models.CharField(max_length=100, blank=True)
    location = models.CharField(max_length=100, blank=True)

    challenge = models.TextField(blank=True)
    solution = models.TextField(blank=True)
    results = models.TextField(blank=True)

    metric_1_label = models.CharField(max_length=100, blank=True)
    metric_1_value = models.CharField(max_length=50, blank=True)
    metric_2_label = models.CharField(max_length=100, blank=True)
    metric_2_value = models.CharField(max_length=50, blank=True)
    metric_3_label = models.CharField(max_length=100, blank=True)
    metric_3_value = models.CharField(max_length=50, blank=True)

    testimonial_quote = models.TextField(blank=True)
    testimonial_name = models.CharField(max_length=100, blank=True)

    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)

    pdf_path = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Case Study'
        verbose_name_plural = 'Case Studies'

    def __str__(self):
        return self.title[:60]

    def metrics(self):
        """Iterable of populated (label, value) tuples — convenience for
        templates that don't want to repeat empty-string checks."""
        pairs = [
            (self.metric_1_label, self.metric_1_value),
            (self.metric_2_label, self.metric_2_value),
            (self.metric_3_label, self.metric_3_value),
        ]
        return [(lbl, val) for lbl, val in pairs if lbl and val]


# ── Phase 7 Part 3 — Website Intelligence & Upsell Engine ──────────────────

class IntelligenceReport(TimestampedModel):
    """
    One monthly Claude-driven analysis run per client. Groups every
    `IntelligenceSuggestion` generated in the same pass and records the
    raw data snapshot Claude was reasoning over (so we can replay or
    audit any individual suggestion later).
    """

    STATUS_CHOICES = [
        ('complete', 'Complete'),
        ('failed', 'Failed'),
        ('no_suggestions', 'No Suggestions'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='intelligence_reports',
    )
    report_month = models.DateField(
        help_text='First day of the month, e.g. 2026-05-01.')
    generated_at = models.DateTimeField(auto_now_add=True)

    # Everything Claude saw — uptime, keywords, scan counts, GBP
    # mismatches, content freshness, health score, etc.
    data_snapshot = models.JSONField(default=dict, blank=True)

    suggestions_count = models.IntegerField(default=0)

    # Plain-English summary Claude returned alongside the suggestions.
    overall_assessment = models.TextField(blank=True)

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='complete')

    total_tokens_used = models.IntegerField(default=0)

    class Meta:
        ordering = ['-report_month']
        unique_together = ['client', 'report_month']
        verbose_name = 'Intelligence Report'
        verbose_name_plural = 'Intelligence Reports'
        indexes = [
            models.Index(fields=['client', '-report_month']),
        ]

    def __str__(self):
        return (f'{self.client.firm_name} — Intelligence '
                f'{self.report_month.strftime("%B %Y")}')


class IntelligenceSuggestion(TimestampedModel):
    """
    A single improvement opportunity Claude surfaced for a client.

    Workflow:
        pending_review → approved_to_send → sent_to_client
           → client_approved → in_scope OR out_of_scope_offered
           → implemented
        (anywhere → dismissed / client_declined)
    """

    SUGGESTION_TYPE_CHOICES = [
        ('seo', 'SEO Improvement'),
        ('performance', 'Performance'),
        ('content', 'Content Update'),
        ('security', 'Security Fix'),
        ('conversion', 'Conversion Optimization'),
        ('keyword', 'Keyword Opportunity'),
        ('competitor', 'Competitor Gap'),
        ('technical', 'Technical Issue'),
        ('design', 'Design Update'),
        ('other', 'Other'),
    ]

    STATUS_CHOICES = [
        ('pending_review', 'Pending Admin Review'),
        ('approved_to_send', 'Approved to Send'),
        ('sent_to_client', 'Sent to Client'),
        ('client_approved', 'Client Approved'),
        ('client_declined', 'Client Declined'),
        ('in_scope', 'In Scope — Approved'),
        ('out_of_scope_offered', 'Out of Scope — Offer Sent'),
        ('implemented', 'Implemented'),
        ('dismissed', 'Dismissed'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='intelligence_suggestions',
    )
    # Set when generated as part of a batch — null if created by hand.
    report = models.ForeignKey(
        IntelligenceReport, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='suggestions',
    )

    generated_at = models.DateTimeField(auto_now_add=True)

    suggestion_type = models.CharField(
        max_length=20, choices=SUGGESTION_TYPE_CHOICES,
        default='other')

    title = models.CharField(max_length=300)
    description = models.TextField()
    expected_impact = models.TextField(blank=True)
    # Internal — never shown to client.
    implementation_notes = models.TextField(blank=True)

    one_time_fee = models.DecimalField(
        max_digits=8, decimal_places=2, default=0)
    maintenance_equivalent = models.TextField(blank=True)

    status = models.CharField(
        max_length=25, choices=STATUS_CHOICES,
        default='pending_review')

    is_in_maintenance_scope = models.BooleanField(default=False)

    # Stripe one-time invoice for out-of-scope upsells.
    stripe_invoice_id = models.CharField(max_length=100, blank=True)
    stripe_invoice_url = models.URLField(blank=True)

    sent_to_client_at = models.DateTimeField(null=True, blank=True)
    client_responded_at = models.DateTimeField(null=True, blank=True)
    implemented_at = models.DateTimeField(null=True, blank=True)

    # Reason captured at dismiss time — admin-only audit trail.
    dismissal_reason = models.CharField(max_length=300, blank=True)

    # Per-suggestion magic-link token for the public approve / decline
    # endpoints. The client never has to log in to respond.
    response_token = models.UUIDField(
        default=uuid.uuid4, unique=True, editable=False)

    # Provenance — which data streams informed this suggestion.
    data_sources = models.JSONField(default=list, blank=True)
    # Raw Claude response for the single suggestion — kept for audit.
    ai_reasoning = models.TextField(blank=True)

    class Meta:
        ordering = ['-generated_at']
        verbose_name = 'Intelligence Suggestion'
        verbose_name_plural = 'Intelligence Suggestions'
        indexes = [
            models.Index(fields=['status', '-generated_at']),
            models.Index(fields=['client', '-generated_at']),
        ]

    def __str__(self):
        return f'{self.client.firm_name} — {self.title[:60]}'

    def get_response_url(self, action):
        """Build the public approve/decline magic-link URL."""
        if action not in ('approve', 'decline'):
            raise ValueError(action)
        return (f'https://aspiredwebsites.com'
                f'/intelligence/respond/{self.response_token}/{action}/')

    @property
    def is_actionable_by_client(self):
        """True when the client can still approve/decline this."""
        return self.status == 'sent_to_client'


# ── Phase 7 Part 4 — Annual Business Health Report ─────────────────────────

class AnnualReport(TimestampedModel):
    """
    Year-in-review PDF auto-generated on each client's anniversary
    month (the month their `Project.launch_date` fell in). Rolls
    uptime / security / conversions / keywords / NPS / changelog /
    intelligence-engine activity for a full calendar year into one
    branded WeasyPrint PDF.

    One row per (client, report_year). The Celery beat that fires on
    the 1st of every month checks `Project.launch_date.month ==
    today.month` and at least 11 months elapsed before queueing
    `generate_annual_report`.
    """

    STATUS_CHOICES = [
        ('generating', 'Generating'),
        ('ready', 'Ready'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='annual_reports',
    )
    report_year = models.IntegerField(
        help_text='Calendar year covered, e.g. 2025.')

    status = models.CharField(
        max_length=15, choices=STATUS_CHOICES, default='generating')

    pdf_path = models.CharField(max_length=500, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    # The full data snapshot driving the PDF — uptime by month,
    # conversion totals, scan counts, keyword changes, intelligence
    # suggestions, changelog totals, NPS averages, etc.
    report_data = models.JSONField(default=dict, blank=True)

    # Claude-generated narrative — three sections rendered into the PDF.
    executive_summary = models.TextField(blank=True)
    year_in_review = models.TextField(blank=True)
    looking_ahead = models.TextField(blank=True)

    total_tokens_used = models.IntegerField(default=0)

    class Meta:
        ordering = ['-report_year']
        unique_together = ['client', 'report_year']
        verbose_name = 'Annual Report'
        verbose_name_plural = 'Annual Reports'
        indexes = [
            models.Index(fields=['client', '-report_year']),
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self):
        return (f'{self.client.firm_name} — '
                f'Annual Report {self.report_year}')


# ── Phase 7 Part 5 — Competitor Content Gap Tracker ────────────────────────

class ClientCompetitor(TimestampedModel):
    """
    A competitor tracked for one client. Capped at 3 per client by
    the admin UI (not the model — operators can override in shell);
    every entry feeds the monthly `CompetitorGapReport` crawl.
    """

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='competitors',
    )
    name = models.CharField(max_length=200)
    domain = models.URLField()
    notes = models.CharField(max_length=300, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'created_at']
        unique_together = ['client', 'domain']
        verbose_name = 'Client Competitor'
        verbose_name_plural = 'Client Competitors'

    def __str__(self):
        return f'{self.client.firm_name} — {self.name}'


class CompetitorGapReport(TimestampedModel):
    """
    Monthly competitor-vs-client content-gap report. Generated by
    `clients.tasks.run_competitor_gap_analysis` which crawls each
    site (client + every competitor) and hands the page lists to
    Claude for gap detection.

    `gaps` is a list of dicts (see `analyze_competitor_gaps` in
    `clients/intelligence.py` for the schema). The first three
    high-priority gaps tend to be the most useful upsell hooks.
    """

    STATUS_CHOICES = [
        ('generating', 'Generating'),
        ('complete', 'Complete'),
        ('failed', 'Failed'),
        ('no_competitors', 'No Competitors Set'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='competitor_gap_reports',
    )
    report_month = models.DateField(
        help_text='First day of the month, e.g. 2026-05-01.')

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='generating')

    # Crawl results.
    client_pages = models.JSONField(default=list, blank=True)
    competitor_data = models.JSONField(default=list, blank=True)

    # Claude output.
    gaps = models.JSONField(default=list, blank=True)
    overall_assessment = models.TextField(blank=True)

    total_gaps_found = models.IntegerField(default=0)
    high_priority_gaps = models.IntegerField(default=0)
    total_tokens_used = models.IntegerField(default=0)

    # Set once the admin has been emailed about high-priority gaps.
    admin_notified = models.BooleanField(default=False)

    class Meta:
        ordering = ['-report_month']
        unique_together = ['client', 'report_month']
        verbose_name = 'Competitor Gap Report'
        verbose_name_plural = 'Competitor Gap Reports'
        indexes = [
            models.Index(fields=['client', '-report_month']),
            models.Index(fields=['status', '-report_month']),
        ]

    def __str__(self):
        return (f'{self.client.firm_name} — Gap Report '
                f'{self.report_month.strftime("%B %Y")}')
