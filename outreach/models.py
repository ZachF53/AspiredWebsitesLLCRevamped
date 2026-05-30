from django.db import models
from django.utils import timezone


class Lead(models.Model):
    """
    Canonical Lead model for ALL sources:
    scraped (Google Maps, State Bar), inbound (contact form, audit tool),
    or manual entry. See CLAUDE.md → Data Model Decisions.
    """

    SOURCE_CHOICES = [
        ('google_maps', 'Google Maps'),
        ('state_bar', 'State Bar Directory'),
        ('contact_form', 'Contact Form'),
        ('audit_tool', 'Audit Tool'),
        ('manual', 'Manual Entry'),
        ('counsel_south', 'Counsel South'),
    ]

    STATUS_CHOICES = [
        ('new', 'New'),
        ('contacted', 'Contacted'),
        ('replied', 'Replied'),
        ('call_booked', 'Call Booked'),
        ('proposal_sent', 'Proposal Sent'),
        ('won', 'Won'),
        ('lost', 'Lost'),
        ('unsubscribed', 'Unsubscribed'),
        ('archived', 'Archived'),
    ]

    TEMPERATURE_CHOICES = [
        ('hot', 'Hot'),
        ('warm', 'Warm'),
        ('cold', 'Cold'),
    ]

    # Business info
    firm_name = models.CharField(max_length=255)
    attorney_name = models.CharField(max_length=255, blank=True)
    practice_area = models.CharField(max_length=100, blank=True)
    business_type = models.CharField(max_length=100, default='Law Firm')

    # Contact info
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    website = models.URLField(blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=50, blank=True)

    # Google presence
    google_rating = models.DecimalField(
        max_digits=3, decimal_places=1, null=True, blank=True
    )
    google_review_count = models.IntegerField(default=0)
    has_google_business = models.BooleanField(default=False)

    # Website audit results (from PageSpeed)
    website_performance_score = models.IntegerField(null=True, blank=True)
    website_seo_score = models.IntegerField(null=True, blank=True)
    website_mobile_score = models.IntegerField(null=True, blank=True)
    website_issues = models.JSONField(default=list, blank=True)
    audit_run_at = models.DateTimeField(null=True, blank=True)

    # ── Enrichment (post-scrape signals — see outreach/enricher.py) ──
    # Populated by enrich_lead() Celery task, fired after import_leads
    # saves the lead row. Two phases:
    #   1. Homepage scrape — emails, social URLs, SSL, copyright year,
    #      PageSpeed.
    #   2. Google Custom Search fallback (only when website is blank) —
    #      tries to find FB / IG / a real website by name + city + state.

    # Social presence. Three biggest channels for SMBs; others go in
    # other_social_urls so we don't keep adding columns.
    facebook_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    linkedin_url = models.URLField(blank=True)
    other_social_urls = models.JSONField(default=list, blank=True)

    # Site-quality signals — cheap to derive from the homepage HTML,
    # all feed the scorer.
    has_ssl = models.BooleanField(
        null=True, blank=True,
        help_text=(
            "True when site is reachable on https://, False when only "
            "http:// works, NULL when not yet checked."))
    copyright_year = models.IntegerField(
        null=True, blank=True,
        help_text=("Year parsed from the footer's © string. Stale "
                   "(3+ years old) is a scoring signal."))
    has_generic_email = models.BooleanField(
        null=True, blank=True,
        help_text=(
            "True when the email we found lives on a free provider "
            "(gmail.com, yahoo.com, hotmail.com, aol.com, outlook.com) "
            "rather than the firm's own domain."))

    # Enrichment lifecycle — task picks up rows where _completed_at
    # is NULL, sets _attempted_at on entry, _completed_at on success.
    # log is plain text appended by each enrichment step for forensics.
    enrichment_attempted_at = models.DateTimeField(null=True, blank=True)
    enrichment_completed_at = models.DateTimeField(null=True, blank=True)
    enrichment_log = models.TextField(blank=True)

    # Lead scoring
    score = models.IntegerField(default=0)
    temperature = models.CharField(
        max_length=10, choices=TEMPERATURE_CHOICES, default='cold'
    )

    # CRM status + source
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='new'
    )
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default='manual'
    )

    # Outreach tracking
    last_contacted_at = models.DateTimeField(null=True, blank=True)
    next_followup_at = models.DateTimeField(null=True, blank=True)
    sequence_step = models.IntegerField(default=0)
    sequence_paused = models.BooleanField(default=False)
    unsubscribed = models.BooleanField(default=False)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    # Tags — comma-separated. Also stores "heard about us" answer from
    # contact form (see CLAUDE.md → Data Model Decisions).
    tags = models.CharField(max_length=500, blank=True)

    # Visitor-supplied free text (contact form message, audit-tool email
    # capture notes, etc). Distinct from `notes` which is internal-only.
    inquiry_text = models.TextField(blank=True)

    # Internal CRM scratch — not visible to the lead.
    notes = models.TextField(blank=True)

    # IP captured for contact-form / audit-tool / scraped leads.
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    # Referral attribution — set from `request.session['referral_code']`
    # when a Lead is created from the contact form (Phase 7 Part 2).
    referral_code = models.CharField(max_length=20, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-score', '-created_at']
        # No unique_together — uniqueness is enforced in code via
        # outreach.deduplication.is_duplicate (fuzzy match on
        # firm_name + city + state). See CLAUDE.md.

    def __str__(self):
        loc = f', {self.city}, {self.state}' if self.city else ''
        return f'{self.firm_name}{loc}'


class LeadNote(models.Model):
    """Internal CRM note attached to a Lead, with its own timestamp."""

    lead = models.ForeignKey(
        Lead, on_delete=models.CASCADE, related_name='lead_notes'
    )
    note = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Note for {self.lead.firm_name} at {self.created_at:%Y-%m-%d}'


class EmailSent(models.Model):
    """
    Outreach email — generated by ``outreach.sender.generate_cold_email``
    (cold) or ``outreach.reply_sender.draft_reply`` (reply). Lifecycle:

        pending_approval ─→ approved ─→ sent
                       └─→ rejected

    ``pending_approval`` rows wait for a human in the Approvals queue;
    ``approved`` rows are picked up by ``send_approved_emails_task`` and
    actually dispatched via SendGrid (``sent_at`` set then, not at
    creation). The trust-level dial in OutreachSettings decides whether
    new rows are auto-promoted past ``pending_approval`` at generation
    time — see ``outreach.gating.should_queue_for_approval``.
    """

    STATUS_CHOICES = [
        ('pending_approval', 'Pending approval'),
        ('approved', 'Approved — waiting to send'),
        ('sent', 'Sent'),
        ('rejected', 'Rejected'),
    ]
    KIND_CHOICES = [
        ('cold', 'Cold outreach'),
        ('reply', 'Reply'),
    ]

    lead = models.ForeignKey(
        Lead, on_delete=models.CASCADE, related_name='emails_sent'
    )
    # Reply emails point back at the inbound EmailReply they answer; cold
    # emails leave this null.
    in_reply_to = models.ForeignKey(
        'EmailReply', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='outbound_replies',
    )

    kind = models.CharField(
        max_length=10, choices=KIND_CHOICES, default='cold'
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending_approval',
        db_index=True,
    )

    subject = models.CharField(max_length=255)
    body = models.TextField()
    from_email = models.EmailField()

    sequence_step = models.IntegerField(default=1)

    # Engagement (set after status='sent' by inbound trackers).
    opened = models.BooleanField(default=False)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked = models.BooleanField(default=False)
    clicked_at = models.DateTimeField(null=True, blank=True)
    replied = models.BooleanField(default=False)
    replied_at = models.DateTimeField(null=True, blank=True)

    # Approval/dispatch metadata. created_at is the generation moment;
    # sent_at is when SendGrid accepted it. They will differ by minutes
    # (auto-send) or hours/days (queued for approval).
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    rejected_reason = models.CharField(max_length=255, blank=True)

    # Message-ID we wrote into the outgoing email's headers; reply
    # ingestion uses this to thread inbound replies back to the right
    # EmailSent. Populated when the drainer actually dispatches.
    message_id_header = models.CharField(
        max_length=255, blank=True, db_index=True,
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'kind', '-created_at']),
        ]

    def __str__(self):
        return (
            f'Email to {self.lead.firm_name} — '
            f'Step {self.sequence_step} ({self.status})'
        )


class EmailReply(models.Model):
    """Inbound reply from a Lead — classified for routing."""

    CLASSIFICATION_CHOICES = [
        ('interested', 'Interested'),
        ('not_interested', 'Not Interested'),
        ('wrong_person', 'Wrong Person'),
        ('maybe_later', 'Maybe Later'),
        ('already_have_someone', 'Already Have Someone'),
        ('question', 'Question — Needs You'),
        ('unclear', 'Unclear — Needs You'),
        ('hostile', 'Hostile — Needs You'),
        ('unsubscribe', 'Unsubscribe Request'),
    ]

    lead = models.ForeignKey(
        Lead, on_delete=models.CASCADE, related_name='replies'
    )
    email_sent = models.ForeignKey(
        EmailSent, on_delete=models.SET_NULL, null=True, blank=True
    )

    classification = models.CharField(
        max_length=30, choices=CLASSIFICATION_CHOICES, blank=True
    )

    subject = models.CharField(max_length=255, blank=True)
    body = models.TextField()
    received_at = models.DateTimeField(auto_now_add=True)
    needs_human = models.BooleanField(default=False)
    handled = models.BooleanField(default=False)
    handled_at = models.DateTimeField(null=True, blank=True)

    ai_suggested_reply = models.TextField(blank=True)

    # RFC 5322 Message-ID of the inbound mail itself — used by
    # ``outreach.reply_ingest`` to guarantee idempotency across IMAP
    # poll runs. ``null=True`` because some old EmailReply rows
    # pre-date this column; ``unique=True`` is safe because NULL
    # values don't collide in unique indexes on either SQLite or
    # Postgres.
    inbound_message_id = models.CharField(
        max_length=255, null=True, blank=True,
        unique=True, db_index=True,
    )

    class Meta:
        ordering = ['-received_at']

    def __str__(self):
        label = self.classification or 'unclassified'
        return f'Reply from {self.lead.firm_name} — {label}'


class SuppressionList(models.Model):
    """Permanent do-not-contact list. Unsubscribes are forever."""

    email = models.EmailField(unique=True)
    domain = models.CharField(max_length=255, blank=True)
    reason = models.CharField(max_length=100, blank=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-added_at']
        verbose_name = 'Suppression List Entry'
        verbose_name_plural = 'Suppression List'

    def __str__(self):
        return self.email


class OutreachSettings(models.Model):
    """Singleton — one row, ever. Controls outreach automation behavior."""

    TRUST_LEVEL_CHOICES = [
        (1, 'Level 1 — Approve every email'),
        (2, 'Level 2 — Auto-send cold, approve replies'),
        (3, 'Level 3 — Auto-send cold + simple replies'),
        (4, 'Level 4 — Full auto except flagged'),
        (5, 'Level 5 — Fully autonomous'),
    ]

    trust_level = models.IntegerField(
        choices=TRUST_LEVEL_CHOICES, default=1
    )
    daily_send_cap = models.IntegerField(default=15)
    warming_start_date = models.DateField(null=True, blank=True)
    outreach_active = models.BooleanField(default=False)

    # Counter — resets at midnight via Celery beat task (added in later week).
    emails_sent_today = models.IntegerField(default=0)
    last_reset_date = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = 'Outreach Settings'
        verbose_name_plural = 'Outreach Settings'

    def __str__(self):
        return f'Outreach Settings — Level {self.trust_level}'

    @classmethod
    def load(cls):
        """Singleton accessor — gets or creates the one row at pk=1."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ScrapeJob(models.Model):
    """
    A standing scrape recipe — niche + city/state + source. The daily
    Celery beat task ``run_scrape_jobs_task`` runs every row where
    ``active=True`` once per 24h, feeding the discovered leads into
    ``outreach.pipeline.import_leads`` (which dedupes against existing
    Lead rows automatically).

    Set ``active=False`` to pause without deleting; the dashboard's
    history of last_run / leads_imported stays for tuning.

    The shape mirrors the manual scrape form so the same view function
    handles both — the only difference is one runs synchronously when
    the operator clicks, the other runs via Celery.
    """

    SOURCE_CHOICES = [
        ('google_maps', 'Google Maps'),
        ('texas_bar', 'Texas State Bar'),
        ('georgia_bar', 'Georgia State Bar'),
    ]

    name = models.CharField(
        max_length=120,
        help_text='Friendly label shown on the scrape dashboard.',
    )
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    niche = models.CharField(
        max_length=120,
        help_text=(
            'Google Maps: free-text search niche (e.g. "personal injury '
            'lawyer", "dentist"). Bar scrapers: the practice area string '
            'exactly as it appears in the directory.'),
    )
    city = models.CharField(max_length=100)
    state = models.CharField(
        max_length=2,
        help_text='TX or GA only (bar scrapers cover those two states).',
    )
    max_results = models.IntegerField(default=20)

    active = models.BooleanField(default=True, db_index=True)

    # Run bookkeeping — only the latest pass is kept.
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_run_imported = models.IntegerField(default=0)
    last_run_skipped = models.IntegerField(default=0)
    last_run_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-active', 'name']

    def __str__(self):
        return f'{self.name} ({self.source})'


class BraveSearchUsage(models.Model):
    """
    Per-month Brave Search API query counter. One row per month;
    incremented atomically by ``outreach.enricher._brave_search``
    after each successful API call.

    Drives the usage banner on /admin-dashboard/leads/ so the admin
    can see where they are against Brave's free 2000/mo tier before
    queries start costing $3/1000.

    Why a model + not a cache key: needs to survive Redis restarts,
    needs a 12-month history for trend visibility later, and writes
    are cheap (one INSERT-OR-UPDATE per Brave call ≤ 3/lead).
    """

    # First day of the month — '2026-05-01' covers all of May 2026.
    # Unique so update_or_create(year_month=...) is a single hit.
    year_month = models.DateField(unique=True)
    query_count = models.IntegerField(default=0)
    last_query_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-year_month']
        verbose_name = 'Brave Search Usage'
        verbose_name_plural = 'Brave Search Usage'

    def __str__(self):
        return (f'{self.year_month:%B %Y} — {self.query_count} '
                f'queries')

    @classmethod
    def _current_month(cls):
        today = timezone.now().date()
        return today.replace(day=1)

    @classmethod
    def increment(cls, n=1):
        """Atomic increment for THIS month's row. Creates the row
        on first call of a new month. Safe to call from concurrent
        Celery workers — uses F() expression."""
        from django.db.models import F
        ym = cls._current_month()
        cls.objects.get_or_create(year_month=ym)
        cls.objects.filter(year_month=ym).update(
            query_count=F('query_count') + n)

    @classmethod
    def current(cls):
        """This month's count (0 when no queries yet this month)."""
        ym = cls._current_month()
        row = cls.objects.filter(year_month=ym).first()
        return row.query_count if row else 0
