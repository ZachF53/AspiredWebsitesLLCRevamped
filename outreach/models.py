from django.db import models


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
    """Outreach email we sent to a Lead. Tracks engagement signals."""

    lead = models.ForeignKey(
        Lead, on_delete=models.CASCADE, related_name='emails_sent'
    )

    subject = models.CharField(max_length=255)
    body = models.TextField()
    from_email = models.EmailField()

    sequence_step = models.IntegerField(default=1)
    opened = models.BooleanField(default=False)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked = models.BooleanField(default=False)
    clicked_at = models.DateTimeField(null=True, blank=True)
    replied = models.BooleanField(default=False)
    replied_at = models.DateTimeField(null=True, blank=True)

    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-sent_at']

    def __str__(self):
        return f'Email to {self.lead.firm_name} — Step {self.sequence_step}'


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
