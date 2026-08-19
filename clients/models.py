"""
Client portal data models — Phase 3.

Every model inherits TimestampedModel (UUID primary key) per CLAUDE.md, so
Aspired and Moonieful record IDs never collide across the sync bridge.
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from clients.display import owner_label
from core.models import TimestampedModel

# Account / Website live in a separate module — re-export so Django's
# app registry discovers them via clients.models, and downstream code
# can write `from clients.models import Account, Website` without
# touching the new file directly.
from clients.account_models import (  # noqa: E402,F401
    Account,
    Website,
    WebsiteStageLog,
    SubscriptionPaymentMethod,
)
# Phase D service models — discovered via this re-export so Django's
# app loader picks them up without a duplicate `from clients.service_models`
# in every caller. See clients/service_models.py for the why.
from clients.service_models import (  # noqa: E402,F401
    MaintenancePlan,
    SocialMediaPlan,
    SocialChannel,
    Droplet,
)


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
    # `website` is the canonical live URL for the client (the address
    # visitors actually go to). Not unique — different clients can
    # share temporary preview/staging URLs while in development.
    # `Project.live_url` is the legacy storage; we keep it in sync on
    # every write but `website` is the source of truth so URLs work
    # even on clients without a Project row (e.g. the auxiliary
    # vault-only profiles).
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
    # Phase D7 — DEPRECATED. Maintained for legacy reads only. New
    # code should look at MaintenancePlan / SocialMediaPlan rows on
    # the Account to determine what services the client has. Will be
    # removed in a future migration once every reader is gone.
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
    # Maintenance plan subscription (Essentials / Growth / Dominant).
    # Created when the client subscribes via the maintenance handoff
    # flow.
    stripe_subscription_id = models.CharField(max_length=255, blank=True)
    # Annual hosting subscription. Created when the client buys hosting
    # on the initial admin invoice ($150/yr line item). Uses Stripe's
    # 365-day trial so the first recurring charge fires 365 days after
    # the initial payment. Gated by the `invoice.upcoming` webhook —
    # if the client's Droplet is no longer on our DO account at
    # renewal time, the subscription cancels before charging.
    stripe_hosting_subscription_id = models.CharField(
        max_length=255, blank=True)
    # Phase 5d — social media subscription (Basic / Standard / Full).
    # Mirrors `stripe_subscription_id` (maintenance) — Stripe sub id for
    # the social plan the client signed up for via /portal/social/plans/.
    # Webhook in billing/webhooks.py promotes this to "active" on the
    # SocialMediaPlan row, same pattern as maintenance.
    stripe_social_subscription_id = models.CharField(
        max_length=255, blank=True)
    # Stripe invoice ID for the one-time admin-created onboarding invoice
    # (Part 2 of the onboarding flow). Lets the webhook find this profile
    # by either customer or invoice ID — and lets the billing dashboard
    # link back to Stripe directly.
    stripe_invoice_id = models.CharField(max_length=255, blank=True)
    # Phase D7 — DEPRECATED. Replaced by MaintenancePlan rows on the
    # Account. Use ``account.maintenance_plans.filter(status='active').exists()``
    # instead of ``client_profile.maintenance_active`` in new code.
    maintenance_active = models.BooleanField(default=False)
    maintenance_started_at = models.DateTimeField(null=True, blank=True)

    # Tracks which maintenance-upsell touchpoints have already fired
    # so the nudge cron doesn't re-spam. Keyed by touchpoint code
    # (e.g. 'day_30', 'day_60') with an ISO timestamp. Empty dict
    # means no nudges sent.
    maintenance_upsell_log = models.JSONField(
        default=dict, blank=True,
        help_text=("Touchpoint -> ISO timestamp map for the "
                   "maintenance upsell nudge cadence."),
    )

    # ── Onboarding workflow (admin invoice → setup → intake → live) ──
    ONBOARDING_STATUS_CHOICES = [
        ('pending_setup', 'Pending Account Setup'),
        ('pending_intake', 'Pending Intake'),
        ('onboarding_complete', 'Complete'),
    ]
    onboarding_status = models.CharField(
        max_length=30,
        choices=ONBOARDING_STATUS_CHOICES,
        default='onboarding_complete',
        help_text=(
            'Gate state for the client portal. NEW admin-invoice flow '
            'sets this to pending_setup explicitly; default is '
            'onboarding_complete so legacy paths (contract-signing, '
            'Moonieful sync, manual admin creation) and the existing '
            'portal continue to work without the gate.'
        ),
    )

    # ── Admin "Needs You" task tracking ──
    # Set when the client submits their intake form (or any other
    # event that needs human admin attention). Cleared by the admin
    # via the Needs You page Mark Reviewed button. Both fields nullable
    # so legacy clients don't show up retroactively.
    needs_admin_review_at = models.DateTimeField(null=True, blank=True)
    admin_reviewed_at = models.DateTimeField(null=True, blank=True)

    # ── DigitalOcean Droplet (one per client) ──
    do_droplet_id = models.CharField(max_length=50, blank=True)
    do_droplet_ip = models.GenericIPAddressField(null=True, blank=True)
    do_droplet_created_at = models.DateTimeField(null=True, blank=True)

    # ── Site lifecycle state (payment-failure dunning chain — Phase 1) ──
    # Driven by billing/tasks.py escalation tasks. State machine:
    #   live → maintenance(503) → offline(power-off) → destroyed
    # `do_snapshot_id` is set during destroy as a 60-day retention
    # snapshot for reinstatement; deleted on Day 60 if no payment.
    # `payment_failure_started_at` is the GUARD field — when it's
    # None, the escalation tasks no-op (means: paid up or reinstated).
    SITE_STATUS_CHOICES = [
        ('live', 'Live'),
        ('maintenance', 'Maintenance (503)'),
        ('offline', 'Offline (powered down)'),
        ('destroyed', 'Droplet destroyed'),
    ]
    site_status = models.CharField(
        max_length=20, choices=SITE_STATUS_CHOICES, default='live',
    )
    do_snapshot_id = models.CharField(max_length=50, blank=True)
    payment_failure_started_at = models.DateTimeField(null=True, blank=True)
    # Reinstatement policy: 1st offense free, 2nd+ offense charges
    # $75 via Stripe before site restoration (CLAUDE.md rule #).
    payment_failure_offenses = models.PositiveIntegerField(default=0)

    # ── Google Business Profile (Phase 5a-pivot) ──
    # Resource name like 'accounts/<acc>/locations/<loc>' — bound from
    # the GBP locations picker once the operator's Google account has
    # been invited as a Manager on the client's GMB. Gated by
    # has_gbp_features() (maintenance tier ≥ Growth, OR comp_package).
    gbp_location_name = models.CharField(max_length=200, blank=True)

    # ── Comp tier (Phase 5a-pivot, expanded in 5d) ──
    # Operator-granted access to a paid product/tier without billing.
    # Use cases: dogfooding our own account, a free trial month for a
    # hot prospect, comping a friend, internal QA.
    #
    # Three INDEPENDENT comp buckets so a client can be comped on one,
    # two, or all three products simultaneously:
    #   comp_build_package       essential_build / premium_build
    #   comp_maintenance_package maintenance_essentials/growth/dominant
    #                            or moonieful_referred
    #   comp_social_tier         social-basic / social-standard / social-full
    #                            (when set, an active SocialMediaPlan is
    #                            ensured on the linked Account so the
    #                            Social Media manager picks it up).
    #
    # `package` keeps the BILLED maintenance/build tier (Stripe webhook
    # updates it on real subscriptions). The comp fields are managed
    # independently by the operator from the Account detail page.
    #
    # `comp_package` is RETAINED as a deprecated alias — the data
    # migration on add backfills the right new field from whatever is
    # already stored. Don't add new readers — use the bucket fields.
    BUILD_COMP_CHOICES = [
        ('essential_build', 'Essential Website Build'),
        ('premium_build',   'Premium Website Build'),
    ]
    MAINTENANCE_COMP_CHOICES = [
        ('maintenance_essentials', 'Maintenance — Essentials'),
        ('maintenance_growth',     'Maintenance — Growth'),
        ('maintenance_dominant',   'Maintenance — Dominant'),
        ('moonieful_referred',     'Moonieful Referred'),
    ]
    SOCIAL_COMP_CHOICES = [
        ('social-basic',    'Social — Basic'),
        ('social-standard', 'Social — Standard'),
        ('social-full',     'Social — Full Management'),
    ]

    comp_package = models.CharField(
        max_length=30, choices=PACKAGE_CHOICES, blank=True,
        help_text='DEPRECATED — use comp_build_package / comp_maintenance_package.',
    )
    comp_build_package = models.CharField(
        max_length=30, choices=BUILD_COMP_CHOICES, blank=True,
    )
    comp_maintenance_package = models.CharField(
        max_length=30, choices=MAINTENANCE_COMP_CHOICES, blank=True,
    )
    comp_social_tier = models.CharField(
        max_length=30, choices=SOCIAL_COMP_CHOICES, blank=True,
    )
    comp_notes = models.TextField(
        blank=True,
        help_text='Why this client is comped — internal note.',
    )

    # ── Tier-gating helpers (Phase 5a-pivot) ──
    # GBP features live in growth + dominant maintenance tiers per
    # the user's call. Reply UI / Q&A / listing audit gate to dominant.

    _GBP_TIERS = {'maintenance_growth', 'maintenance_dominant'}
    _GBP_PREMIUM_TIERS = {'maintenance_dominant'}

    def _active_tiers(self):
        """Set of tier slugs this client has access to — billed
        `package` PLUS every operator-granted comp field. The legacy
        `comp_package` is included so historical rows that haven't
        been migrated yet still gate correctly."""
        out = set()
        for slug in (
            self.package,
            self.comp_package,
            self.comp_build_package,
            self.comp_maintenance_package,
            self.comp_social_tier,
        ):
            if slug:
                out.add(slug)
        return out

    def has_gbp_features(self):
        """True if this client's tier (paid OR comped) qualifies for
        GBP management (NAP sync, review monitoring, performance metrics)."""
        return bool(self._active_tiers() & self._GBP_TIERS)

    def has_gbp_premium_features(self):
        """True if this client (paid OR comped) qualifies for GBP reply
        workflow + Q&A + listing audit — Dominant-tier-only features."""
        return bool(self._active_tiers() & self._GBP_PREMIUM_TIERS)

    def has_unpaid_out_of_scope(self):
        """True if any MiniInvoice on this client is not paid or
        cancelled. Used to gate revision work in the portal — Phase 1.4."""
        return self.mini_invoices.exclude(
            status__in=['paid', 'cancelled']).exists()

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

    # ── Project fields merged into ClientProfile (2026-05-25) ──
    # Previously these lived on a separate Project model with a 1:N
    # relationship. Multi-project-per-client was never actually used
    # in production and the cross-model URL/stage lookups were a
    # constant source of `client.projects.first.X`-blows-up-when-None
    # bugs. Now flattened onto ClientProfile as the single source of
    # truth. Project model + table will be dropped in Phase 2.
    PAYMENT_STATUS_CHOICES = [
        ('awaiting_deposit', 'Awaiting Deposit'),
        ('deposit_paid', 'Deposit Paid'),
        ('fully_paid', 'Fully Paid'),
    ]
    stage = models.CharField(
        max_length=20, choices=PROJECT_STAGES, default='intake',
        help_text='Where this client is in the build lifecycle.',
    )
    staging_url = models.URLField(blank=True)
    # `website` (the canonical live URL field) lives further up — it
    # was extracted earlier in the refactor. Stays where it is.
    launch_date = models.DateField(null=True, blank=True)
    support_window_ends = models.DateField(
        null=True, blank=True, help_text='Launch date + 14 days.',
    )
    payment_status = models.CharField(
        max_length=20, choices=PAYMENT_STATUS_CHOICES,
        default='awaiting_deposit',
    )
    deposit_paid_at = models.DateTimeField(null=True, blank=True)
    final_paid_at = models.DateTimeField(null=True, blank=True)

    # Revisions are resettable: admin clicks "Reset revisions" when
    # starting a new effort with the client (e.g. mini-redesign,
    # second build) and the counter goes back to zero.
    revision_count = models.PositiveIntegerField(default=0)
    revision_limit = models.PositiveIntegerField(default=2)
    revisions_reset_at = models.DateTimeField(null=True, blank=True)

    # Moonieful handoff timestamps (kept on ClientProfile because the
    # synced_from_moonieful flag already lives here).
    moonieful_handoff_at = models.DateTimeField(null=True, blank=True)
    moonieful_stage_history = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Client Profile'
        verbose_name_plural = 'Client Profiles'

    def __str__(self):
        return self.firm_name

    # ── Backward-compat aliases ──
    # `live_url` was on Project; the canonical field on ClientProfile
    # is `website`. Keep `live_url` working as a read-only alias so
    # legacy templates / emails that reference {{ project.live_url }}
    # (now aliased to profile) keep rendering.
    @property
    def live_url(self):
        return self.website or ''

    # ── Revision helpers (formerly Project methods) ──

    @property
    def revisions_remaining(self):
        return max(self.revision_limit - self.revision_count, 0)

    @property
    def over_revision_limit(self):
        return self.revision_count > self.revision_limit

    def reset_revisions(self, *, save=True):
        """Wipe the revision counter back to zero. Use when starting a
        new effort with the client (e.g. mini-redesign).
        """
        from django.utils import timezone as _tz
        self.revision_count = 0
        self.revisions_reset_at = _tz.now()
        if save:
            self.save(update_fields=[
                'revision_count', 'revisions_reset_at', 'updated_at'])


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
        return f'{owner_label(self)} — {self.get_stage_display()}'

    @property
    def revisions_remaining(self):
        return max(self.revision_limit - self.revision_count, 0)

    @property
    def over_revision_limit(self):
        return self.revision_count > self.revision_limit


class ProjectStageLog(TimestampedModel):
    """
    An append-only record of every stage transition.

    Will be renamed `ClientStageLog` in Phase 2 once all writers point
    at the client FK. Both `project` and `client` exist during the
    transition; project is nullable so future writes can omit it.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name='stage_logs',
        null=True, blank=True,
    )
    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='stage_logs', null=True, blank=True,
    )
    # Phase A — new FK to the post-refactor Website model. Nullable
    # so the additive migration doesn't require backfill order. Phase
    # C readers prefer ``website`` when set; Phase D drops the legacy
    # ``project`` and ``client`` columns and renames this back to
    # ``website`` without the ``_new`` suffix (kept here for clarity).
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='stage_logs_new', null=True, blank=True,
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
        cname = (
            owner_label(self)
        )
        return f'{cname}: {self.from_stage} → {self.to_stage}'


class IntakeResponse(TimestampedModel):
    """The client's intake questionnaire answers for a project."""

    REGISTRAR_CHOICES = [
        ('namecheap', 'Namecheap'),
        ('godaddy', 'GoDaddy'),
        ('google_domains', 'Google Domains'),
        ('cloudflare', 'Cloudflare'),
        ('other', 'Other'),
    ]

    # Both FKs exist during the Phase 1/2 transition. `client` is the
    # new canonical link; `project` is the legacy. Both nullable so
    # the schema doesn't require backfill order.
    project = models.OneToOneField(
        Project, on_delete=models.CASCADE, related_name='intake',
        null=True, blank=True,
    )
    client = models.OneToOneField(
        ClientProfile, on_delete=models.CASCADE,
        related_name='intake', null=True, blank=True,
    )
    # Phase A — new FK to Website. Nullable for the additive migration.
    website_new = models.OneToOneField(
        'clients.Website', on_delete=models.CASCADE,
        related_name='intake_new', null=True, blank=True,
    )
    completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Step 1 — Brand
    brand_colors = models.CharField(max_length=255, blank=True)
    brand_fonts = models.CharField(max_length=255, blank=True)
    logo = models.FileField(upload_to='portal/intake/logos/', null=True, blank=True)
    # Set when the client opts in to "I don't have a logo yet" on the
    # Logo step. Marks the field as satisfied for wizard-validation +
    # the server-side intake completeness check without requiring an
    # actual file upload.
    no_logo_yet = models.BooleanField(default=False)

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
    # Free-text registrar name when domain_registrar='other'. Required by the
    # form's JS when the dropdown is set to "Other" but stored blank
    # otherwise so a registrar change doesn't strand stale text.
    domain_registrar_other = models.CharField(max_length=120, blank=True)

    # Google Business Profile access used to be a checkbox here; moved out of
    # the client intake (it's a post-launch ops task, not something the
    # client should grant before work even starts). The field is kept on the
    # model so legacy data isn't lost and the admin can still see it.
    google_business_access = models.BooleanField(default=False)

    # ── Google Business Profile (GMB) management opt-in ──
    # Asked on the website intake. Drives the post-intake email + SetupTodo:
    #   have    → they have a GBP → email steps to add us as a Manager
    #   need    → no GBP yet      → email steps to create one + add us
    #   decline → they don't want us managing it → no email, no task
    GMB_STATUS_CHOICES = [
        ('have', 'I have a Google Business Profile'),
        ('need', "I don't have one yet"),
        ('decline', "I don't want you to manage it"),
    ]
    gmb_status = models.CharField(
        max_length=10, choices=GMB_STATUS_CHOICES, blank=True)

    # ── Step 5 — Social profiles ──
    # Split out of the freeform `social_links` blob so we can render proper
    # input boxes for the four common channels. The textarea below now
    # captures only "everything else" (Pinterest, TikTok, Avvo, etc.).
    facebook_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    linkedin_url = models.URLField(blank=True)
    twitter_url = models.URLField(blank=True)
    google_business_url = models.URLField(blank=True)
    social_links = models.TextField(
        blank=True,
        help_text='Catch-all for any social profiles not in the four standard fields.',
    )

    # SOURCE OF TRUTH for Moonieful-synced clients — typed fields above are
    # for direct Aspired clients only.
    moonieful_intake_raw = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Intake Response'
        verbose_name_plural = 'Intake Responses'

    def __str__(self):
        cname = (
            owner_label(self)
        )
        return f'Intake — {cname}'


def intake_photo_path(instance, filename):
    """Upload path: portal/intake/photos/<client_id>/<filename>."""
    return (
        f'portal/intake/photos/'
        f'{instance.intake.project.client_id}/{filename}'
    )


class IntakePhoto(TimestampedModel):
    """
    A photo uploaded as part of the intake — headshots, team shots, office
    photos. Separate from ClientDocument so the intake step can render its
    own gallery + delete UI without dragging unrelated files into it.
    """

    intake = models.ForeignKey(
        IntakeResponse, on_delete=models.CASCADE, related_name='photos',
    )
    file = models.FileField(upload_to=intake_photo_path)
    label = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = 'Intake Photo'
        verbose_name_plural = 'Intake Photos'

    def __str__(self):
        return self.label or self.file.name


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
        null=True, blank=True,
    )
    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='revisions', null=True, blank=True,
    )
    # Phase A — new FK to Website.
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='revisions_new', null=True, blank=True,
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
        cname = (
            owner_label(self)
        )
        return f'{cname}: {self.description[:50]}'


class ClientDocument(TimestampedModel):
    """A file exchanged between Aspired and a client (either direction)."""

    DIRECTION_CHOICES = [
        ('to_client', 'To Client'),
        ('from_client', 'From Client'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE, related_name='documents',
        null=True, blank=True,
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, blank=True,
        related_name='documents',
    )
    # Phase A — new FK to Website (per user spec, files belong per-build).
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='documents_new', null=True, blank=True,
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
        null=True, blank=True,
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, null=True, blank=True,
        related_name='tickets',
    )
    # Phase A — tickets are account-scoped with an optional website link
    # (a ticket can be about a specific build or about the account in
    # general, e.g. billing).
    account_new = models.ForeignKey(
        'clients.Account', on_delete=models.CASCADE,
        related_name='tickets_new', null=True, blank=True,
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='tickets_new', null=True, blank=True,
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
        return f'{owner_label(self)}: {self.subject}'


class Contract(TimestampedModel):
    """
    A services contract for a client. The first step of onboarding:
    generated by staff, signed by the client via an unguessable token URL.

    Originally website-build only; now a contract can cover any mix of
    website development, maintenance, and social media (one ``ContractService``
    row per selected service). The legacy build columns (``package``,
    ``build_price``, ``deposit_amount``, ``timeline_weeks``) are kept and
    populated from the build line when one is present, so the existing
    deposit-invoice and PDF code paths keep working unchanged. They are now
    nullable/blank so a maintenance- or social-only contract is valid.
    """

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE, related_name='contracts',
        null=True, blank=True,
    )
    # Phase C — contracts can be raised from the Account dashboard. Nullable
    # so legacy build contracts (created via the Django admin action, which
    # only knows the ClientProfile) still validate.
    account = models.ForeignKey(
        'clients.Account', on_delete=models.CASCADE,
        related_name='contracts', null=True, blank=True,
    )
    # Phase A — contracts are per-build.
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='contracts_new', null=True, blank=True,
    )
    # ── Legacy build columns (populated from the build line when present) ──
    package = models.CharField(
        max_length=20, choices=BUILD_PACKAGE_CHOICES, blank=True)
    build_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True)
    deposit_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True)
    timeline_weeks = models.IntegerField(default=4)
    contract_text = models.TextField()
    signed = models.BooleanField(default=False)
    signed_at = models.DateTimeField(null=True, blank=True)
    signed_ip = models.GenericIPAddressField(null=True, blank=True)
    signed_name = models.CharField(max_length=200, blank=True)
    # Phase 2.3 — audit-trail hardening for ESIGN/UETA enforceability.
    # signed_user_agent captures the browser at signing time so we can
    # show "what the signer was looking at." signed_content_hash is the
    # SHA-256 of contract_text at the exact moment of signing — re-
    # hashing the stored contract_text must reproduce this value, proving
    # the document hasn't been tampered with after the fact.
    signed_user_agent = models.CharField(max_length=400, blank=True)
    signed_content_hash = models.CharField(max_length=64, blank=True)
    contract_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    pdf_path = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Contract — {owner_label(self)}'

    @property
    def final_amount(self):
        return (self.build_price or Decimal('0')) - (self.deposit_amount or Decimal('0'))

    @property
    def includes_build(self):
        """True if this contract covers a website build.

        New multi-service contracts are driven by their ``services`` rows.
        Legacy contracts (created before multi-service support, so they have
        no service rows) are always builds — fall back to ``package``.
        """
        if self.services.exists():
            return self.services.filter(service_type='build').exists()
        return bool(self.package)

    @property
    def status_label(self):
        return 'Signed' if self.signed else 'Sent — awaiting signature'

    @property
    def service_summary(self):
        """Human list of the services on this contract, e.g.
        'Website Development, Website Maintenance'. Falls back to the build
        package display for legacy contracts with no service rows."""
        rows = list(self.services.all())
        if rows:
            return ', '.join(r.get_service_type_display() for r in rows)
        return self.get_package_display() or '—'


class ContractService(TimestampedModel):
    """One service line on a :class:`Contract`.

    A contract can bundle a website build (one-time, 50% deposit) with
    recurring maintenance and/or social plans (monthly). Each selected
    service gets a row capturing the tier and its price at signing time.
    """

    SERVICE_TYPE_CHOICES = [
        ('build', 'Website Development'),
        ('maintenance', 'Website Maintenance'),
        ('social', 'Social Media Marketing'),
    ]

    contract = models.ForeignKey(
        Contract, on_delete=models.CASCADE, related_name='services',
    )
    service_type = models.CharField(
        max_length=20, choices=SERVICE_TYPE_CHOICES)
    # billing.ServiceTier slug, e.g. 'website-essential', 'maintenance-growth'.
    tier_slug = models.CharField(max_length=50)
    tier_name = models.CharField(max_length=120, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    # Only set for the build line (50% upfront). Null for recurring services.
    deposit_amount = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True)
    is_recurring = models.BooleanField(default=False)
    billing_interval = models.CharField(
        max_length=10, blank=True, help_text="'month', 'year', or '' (one-time)")

    class Meta:
        ordering = ['service_type']
        unique_together = [('contract', 'service_type')]

    def __str__(self):
        return f'{self.get_service_type_display()} — {self.tier_name or self.tier_slug}'


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
        null=True, blank=True,
    )
    # Phase A — per-build.
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='changelog_entries_new', null=True, blank=True,
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
        return (f'{owner_label(self)} — '
                f'{self.get_change_type_display()} — '
                f'{self.date_of_change}')


class UptimeRecord(TimestampedModel):
    """A single uptime check result for a client's live site."""

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='uptime_records',
        null=True, blank=True,
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='uptime_records_new', null=True, blank=True,
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
            # The canonical mirror. Every uptime read is now keyed on
            # `website_new` (see reporting/uptime_helpers.py), so without
            # this the composite above indexes a column nothing queries
            # while the queries that replaced it run unindexed past the
            # FK. This is the largest table in the schema — ~75k rows —
            # and `get_uptime_chart_data` alone issues 90 of these per
            # call.
            models.Index(fields=['website_new', 'checked_at']),
        ]

    def __str__(self):
        status = 'UP' if self.is_up else 'DOWN'
        return f'{owner_label(self)} — {status} — {self.checked_at}'


class UptimeAlert(TimestampedModel):
    """An open / resolved downtime incident — one per outage, no spam."""

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='uptime_alerts',
        null=True, blank=True,
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='uptime_alerts_new', null=True, blank=True,
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
        return f'{owner_label(self)} — DOWN — {status}'



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
        null=True, blank=True,
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='health_scores_new', null=True, blank=True,
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
            models.Index(fields=['website_new', '-calculated_at']),
            models.Index(fields=['health_status', '-calculated_at']),
        ]

    def __str__(self):
        return (f'{owner_label(self)} — '
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
        null=True, blank=True,
    )
    # Phase A — referral links are account-level (one per Account).
    account_new = models.OneToOneField(
        'clients.Account', on_delete=models.CASCADE,
        related_name='referral_link_new', null=True, blank=True,
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
        return f'{owner_label(self)} — ref/{self.code}'

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
        return (f'{owner_label(self.referral_link)} — '
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
    # Phase A — case study is about one Website (SET_NULL because the
    # case study survives a website deletion).
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='case_studies_new',
    )

    title = models.CharField(max_length=300)
    business_type = models.CharField(max_length=100, blank=True)
    location = models.CharField(max_length=100, blank=True)

    # ── What Aspired actually did for this client ────────────────────
    # The portfolio holds genuinely different relationships: sites we
    # built, and sites somebody else built that we maintain and improve.
    # Templates generate headings and image alt text from the study
    # rather than hand-writing them per page, so without this field every
    # study is described as "built by Aspired Websites" — which is false
    # for a maintenance engagement and is the exact misrepresentation the
    # Denis Law Group correction exists to fix.
    #
    # Blank is deliberately the default and means "not stated". A row
    # that has never been reviewed must fall back to neutral wording
    # rather than inherit a build claim nobody verified.
    ENGAGEMENT_TYPE_CHOICES = [
        ('built', 'Built by Aspired'),
        ('redesigned', 'Redesigned by Aspired'),
        ('maintained', 'Maintained and improved by Aspired'),
        ('consulted', 'Consulting engagement'),
    ]
    engagement_type = models.CharField(
        max_length=20, choices=ENGAGEMENT_TYPE_CHOICES, blank=True,
        help_text=('What Aspired did for this client. Leave blank if it '
                   'has not been verified — public copy then stays '
                   'neutral instead of claiming a build.'),
    )

    # The platform the site runs on, when naming it is accurate and
    # useful (e.g. an existing WordPress site we maintain). Described
    # neutrally; never used to disparage the platform.
    platform = models.CharField(
        max_length=60, blank=True,
        help_text='e.g. "WordPress". Blank when not relevant.',
    )

    # ── Public case-study page (Master Plan §11) ────────────────────
    # Every portfolio project needs its own indexable URL; they used to
    # collapse into one flat /portfolio/ page, which meant four
    # projects competing to be described by a single URL.
    slug = models.SlugField(
        max_length=140, unique=True, null=True, blank=True,
        help_text='URL segment for /portfolio/<slug>/. Leave blank to '
                  'auto-generate from the title.',
    )
    summary = models.CharField(
        max_length=300, blank=True,
        help_text='One-line description used on the portfolio card and '
                  'as the meta description.',
    )
    live_url = models.URLField(
        blank=True, help_text='The published client site, if public.')
    screenshot = models.ImageField(
        upload_to='portfolio/', blank=True,
        help_text='Screenshot of the live site, 16:10 to match the card. '
                  'Captured by `capture_case_study_screenshots`; falls '
                  'back to card_gradient when empty.',
    )
    # The gradient is no longer the only option, but it stays as the
    # fallback: a client site can go offline, get redesigned by someone
    # else, or simply not be public, and a coloured card is a better
    # answer than a broken image or a stale screenshot of work that is
    # no longer ours.
    card_gradient = models.CharField(
        max_length=40, blank=True, default='gradient-blue',
        help_text='CSS class for the card visual, e.g. gradient-blue. '
                  'Used when no screenshot is set.',
    )

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

    # ── Relationship-aware public wording ────────────────────────────
    # One place decides how a study describes itself, so a heading, an
    # alt text and a proof block cannot drift apart.

    @property
    def work_heading(self):
        """Heading for the narrative section of the case study."""
        return {
            'built': 'What We Built',
            'redesigned': 'What We Redesigned',
            'maintained': 'What We Improved',
            'consulted': 'What We Advised',
        }.get(self.engagement_type, 'What We Did')

    @property
    def relationship_label(self):
        """Short badge describing Aspired's role, or '' when unverified."""
        return dict(self.ENGAGEMENT_TYPE_CHOICES).get(
            self.engagement_type, '')

    @property
    def image_alt(self):
        """Alt text that states the real relationship.

        Never says "built by Aspired Websites" for a site Aspired did not
        build, and says nothing about authorship at all when the
        engagement type has not been verified.
        """
        base = f'Homepage of {self.title}'
        suffix = {
            'built': ' — built by Aspired Websites',
            'redesigned': ' — redesigned by Aspired Websites',
            'maintained': ' — maintained and improved by Aspired Websites',
            'consulted': '',
        }.get(self.engagement_type, '')
        return f'{base}{suffix}'

    def __str__(self):
        return self.title[:60]

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            base = slugify(self.title)[:130] or 'case-study'
            slug, n = base, 2
            while CaseStudy.objects.filter(slug=slug).exclude(
                    pk=self.pk).exists():
                slug = f'{base}-{n}'
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse('public:case_study_detail',
                       kwargs={'slug': self.slug})

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
        null=True, blank=True,
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='intelligence_reports_new', null=True, blank=True,
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
        # Site-keyed. `client` is nullable during the cutover and a
        # NULL is distinct from every other NULL in a unique index,
        # so it can no longer carry a uniqueness guarantee.
        unique_together = ['website_new', 'report_month']
        verbose_name = 'Intelligence Report'
        verbose_name_plural = 'Intelligence Reports'
        indexes = [
            models.Index(fields=['client', '-report_month']),
            models.Index(fields=['website_new', '-report_month']),
        ]

    def __str__(self):
        return (f'{owner_label(self)} — Intelligence '
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
        null=True, blank=True,
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='intelligence_suggestions_new',
        null=True, blank=True,
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
            models.Index(fields=['website_new', '-generated_at']),
        ]

    def __str__(self):
        return f'{owner_label(self)} — {self.title[:60]}'

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
        null=True, blank=True,
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='annual_reports_new', null=True, blank=True,
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
        # Site-keyed. `client` is nullable during the cutover and a
        # NULL is distinct from every other NULL in a unique index,
        # so it can no longer carry a uniqueness guarantee.
        unique_together = ['website_new', 'report_year']
        verbose_name = 'Annual Report'
        verbose_name_plural = 'Annual Reports'
        indexes = [
            models.Index(fields=['client', '-report_year']),
            models.Index(fields=['website_new', '-report_year']),
            models.Index(fields=['status', '-created_at']),
        ]

    def __str__(self):
        return (f'{owner_label(self)} — '
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
        null=True, blank=True,
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='competitors_new', null=True, blank=True,
    )
    name = models.CharField(max_length=200)
    domain = models.URLField()
    notes = models.CharField(max_length=300, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'created_at']
        # Site-keyed. `client` is nullable during the cutover and a
        # NULL is distinct from every other NULL in a unique index,
        # so it can no longer carry a uniqueness guarantee.
        unique_together = ['website_new', 'domain']
        verbose_name = 'Client Competitor'
        verbose_name_plural = 'Client Competitors'

    def __str__(self):
        return f'{owner_label(self)} — {self.name}'


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
        null=True, blank=True,
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='competitor_gap_reports_new',
        null=True, blank=True,
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
        # Site-keyed. `client` is nullable during the cutover and a
        # NULL is distinct from every other NULL in a unique index,
        # so it can no longer carry a uniqueness guarantee.
        unique_together = ['website_new', 'report_month']
        verbose_name = 'Competitor Gap Report'
        verbose_name_plural = 'Competitor Gap Reports'
        indexes = [
            models.Index(fields=['client', '-report_month']),
            models.Index(fields=['website_new', '-report_month']),
            models.Index(fields=['status', '-report_month']),
        ]

    def __str__(self):
        return (f'{owner_label(self)} — Gap Report '
                f'{self.report_month.strftime("%B %Y")}')


# ── Onboarding token ────────────────────────────────────────────────────────

class OnboardingToken(TimestampedModel):
    """
    One-time token gating the account-setup page (/onboarding/setup/<token>/).

    Created when the admin generates the onboarding invoice; the token URL is
    emailed to the client after Stripe confirms payment. The token never
    expires — the link stays valid until the client uses it — but it can only
    be consumed once (`used=True` is set on first successful submit).
    """

    client = models.OneToOneField(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name='onboarding_token',
        null=True, blank=True,
    )
    # Phase A — onboarding token gates the account setup page (WHOIS +
    # PIN), so it's account-level (one per Account).
    account_new = models.OneToOneField(
        'clients.Account', on_delete=models.CASCADE,
        related_name='onboarding_token_new', null=True, blank=True,
    )
    token = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        db_index=True,
    )
    used = models.BooleanField(default=False)
    used_at = models.DateTimeField(null=True, blank=True)

    # Reminder tracking — keys the Celery reminder task uses to throttle
    # (24h between setup nudges, 48h between intake nudges).
    setup_reminders_sent = models.IntegerField(default=0)
    last_setup_reminder_at = models.DateTimeField(null=True, blank=True)
    intake_reminders_sent = models.IntegerField(default=0)
    last_intake_reminder_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Onboarding Token'
        verbose_name_plural = 'Onboarding Tokens'

    def get_setup_url(self):
        """Absolute URL of the public setup page. Safe to embed in emails."""
        base = getattr(
            settings, 'SITE_BASE_URL',
            'https://aspiredwebsites.com').rstrip('/')
        return f'{base}/onboarding/setup/{self.token}/'

    def __str__(self):
        state = 'Used' if self.used else 'Pending'
        return f'{owner_label(self)} — {state}'


# ── Onboarding invoice ─────────────────────────────────────────────────────

class OnboardingInvoice(TimestampedModel):
    """
    One-off invoice the admin generates at the start of a new engagement
    (build fee + optional first-month maintenance + optional hosting).

    Replaces the old "create a Stripe Invoice and let Stripe send the email"
    flow. Now the client lands on aspiredwebsites.com/pay/<payment_token>/
    where they pay via Stripe Elements on our own page. Stripe handles
    card processing but the entire UX is on our domain.

    Line items + total are snapshotted on creation so the payment page
    can render them deterministically (the Stripe PaymentIntent itself
    doesn't carry line items).
    """

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('sent', 'Sent'),
        ('paid', 'Paid'),
        ('canceled', 'Canceled'),
    ]

    # 1:1 with ClientProfile — at most one onboarding invoice per client.
    # Follow-on / out-of-scope invoices use the existing MiniInvoice model.
    client = models.OneToOneField(
        ClientProfile,
        on_delete=models.CASCADE,
        related_name='onboarding_invoice',
        null=True, blank=True,
    )
    # Phase A — onboarding invoice is per-build (a second Website needs
    # its own deposit + build fee invoice). 1:1 with Website. Account
    # FK is also kept so Stripe Customer resolution is unambiguous
    # even before Website backfill on legacy rows.
    account_new = models.ForeignKey(
        'clients.Account', on_delete=models.CASCADE,
        related_name='onboarding_invoices_new',
        null=True, blank=True,
    )
    website_new = models.OneToOneField(
        'clients.Website', on_delete=models.CASCADE,
        related_name='onboarding_invoice_new',
        null=True, blank=True,
    )

    # Snapshot of what's being billed — list of
    # `[{"description": "...", "amount": "2500.00"}]` rendered on the
    # payment page and the receipt PDF.
    line_items = models.JSONField(default=list)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)

    # ── Contract deposit flow (Phase C) ──
    # When this invoice is the deposit/full payment for a signed build
    # Contract (client lands here straight after signing), link it back so
    # the webhook can set payment_status correctly. `is_deposit` is True for
    # the 50% deposit (→ payment_status='deposit_paid', final invoiced later)
    # and False when the client chose to pay in full (→ 'fully_paid').
    contract = models.ForeignKey(
        'clients.Contract', on_delete=models.SET_NULL,
        related_name='onboarding_invoices', null=True, blank=True,
    )
    is_deposit = models.BooleanField(default=False)

    # ── Stripe references ──
    stripe_payment_intent_id = models.CharField(max_length=255, blank=True)
    stripe_client_secret = models.CharField(max_length=255, blank=True)

    # Public payment-page token. UUID is unguessable enough that the URL
    # `/pay/<token>/` is safe to embed in the invoice email without
    # additional auth.
    payment_token = models.UUIDField(
        default=uuid.uuid4, unique=True, db_index=True,
    )

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='draft',
    )

    sent_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    # ── Branded receipt PDF (generated after payment) ──
    # Relative to MEDIA_ROOT — same pattern as monthly reports and
    # contract PDFs. Falls back to .html on Windows dev.
    receipt_pdf_path = models.CharField(max_length=500, blank=True)
    receipt_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Onboarding Invoice'
        verbose_name_plural = 'Onboarding Invoices'

    def __str__(self):
        return (f'{owner_label(self)} — '
                f'${self.total_amount:,.2f} ({self.status})')

    def get_pay_url(self):
        """Absolute URL of the public payment page."""
        base = getattr(
            settings, 'SITE_BASE_URL',
            'https://aspiredwebsites.com').rstrip('/')
        return f'{base}/pay/{self.payment_token}/'


class PaymentRecord(TimestampedModel):
    """A ledger entry for every successful payment — one-time website-build
    payments (deposit/final, via PaymentIntent) AND recurring subscription
    charges (maintenance/social/hosting, via Stripe invoices). Written by the
    Stripe webhooks so the client's Invoices page is a complete, durable
    billing history independent of live Stripe calls.
    """

    KIND_CHOICES = [
        ('deposit', 'Website Deposit'),
        ('final', 'Website Final Payment'),
        ('build', 'Website Payment'),
        ('maintenance', 'Maintenance'),
        ('social', 'Social Media'),
        ('hosting', 'Hosting'),
        ('addon', 'Add-on / Out-of-scope'),
        ('other', 'Payment'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='payment_records',
        null=True, blank=True,
    )
    account = models.ForeignKey(
        'clients.Account', on_delete=models.SET_NULL,
        related_name='payment_records', null=True, blank=True,
    )
    website = models.ForeignKey(
        'clients.Website', on_delete=models.SET_NULL,
        related_name='payment_records', null=True, blank=True,
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='other')
    description = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default='paid')
    # Stripe PaymentIntent id or Invoice id — unique for idempotent recording
    # across webhook re-deliveries.
    stripe_id = models.CharField(max_length=255, unique=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    # Hosted Stripe invoice/receipt URL (subscriptions) when available.
    receipt_url = models.URLField(blank=True)

    class Meta:
        ordering = ['-paid_at', '-created_at']

    def __str__(self):
        return (f'{owner_label(self)} — {self.get_kind_display()} '
                f'${self.amount:,.2f}')
