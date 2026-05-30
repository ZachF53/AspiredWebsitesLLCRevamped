"""
Reporting models — GBP NAP sync checks, keyword rank tracking,
conversion-element events, monthly reports, content freshness, NPS surveys,
AI blog posts, and the AI chatbot. All feed the monthly PDF and the portal.
"""

import uuid

from django.db import models

from clients.models import ClientProfile
from core.models import TimestampedModel


class GBPSyncCheck(TimestampedModel):
    """One NAP-field comparison between a client's site and their GBP listing."""

    FIELD_CHOICES = [
        ('business_name', 'Business Name'),
        ('phone', 'Phone Number'),
        ('address', 'Address'),
        ('website', 'Website URL'),
        ('hours', 'Business Hours'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='gbp_sync_checks',
    )
    # Phase A — Website-scoped (each build has its own GBP listing).
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='gbp_sync_checks_new', null=True, blank=True,
    )
    checked_at = models.DateTimeField(auto_now_add=True)
    field_name = models.CharField(max_length=20, choices=FIELD_CHOICES)
    website_value = models.TextField(blank=True)
    gbp_value = models.TextField(blank=True)
    is_mismatch = models.BooleanField(default=False)
    flagged_for_fix = models.BooleanField(default=False)
    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-checked_at']
        verbose_name = 'GBP Sync Check'
        verbose_name_plural = 'GBP Sync Checks'

    def __str__(self):
        status = 'MISMATCH' if self.is_mismatch else 'OK'
        return f'{self.client.firm_name} — {self.field_name} — {status}'


class TrackedKeyword(TimestampedModel):
    """A keyword a client's site is being tracked for in search rankings."""

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='tracked_keywords',
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='tracked_keywords_new', null=True, blank=True,
    )
    keyword = models.CharField(max_length=200)
    target_url = models.URLField(
        blank=True,
        help_text='The page on their site targeting this keyword.',
    )
    is_active = models.BooleanField(default=True)
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = ['client', 'keyword']
        ordering = ['keyword']

    def __str__(self):
        return f'{self.client.firm_name} — {self.keyword}'


class KeywordRankRecord(TimestampedModel):
    """A dated ranking snapshot for one tracked keyword."""

    keyword = models.ForeignKey(
        TrackedKeyword, on_delete=models.CASCADE,
        related_name='rank_records',
    )
    checked_at = models.DateField(auto_now_add=True)
    position = models.IntegerField(
        null=True, blank=True,
        help_text='1-100 ranking position; null = not in the top 100.',
    )
    impressions = models.IntegerField(default=0)
    clicks = models.IntegerField(default=0)

    class Meta:
        ordering = ['-checked_at']
        unique_together = ['keyword', 'checked_at']

    def __str__(self):
        pos = self.position or 'Not ranked'
        return f'{self.keyword.keyword} — #{pos}'


class ConversionEvent(TimestampedModel):
    """A conversion-element interaction reported by the on-site JS tracker."""

    EVENT_TYPE_CHOICES = [
        ('form_submit', 'Form Submission'),
        ('phone_click', 'Phone Number Click'),
        ('cta_click', 'CTA Button Click'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='conversion_events',
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='conversion_events_new', null=True, blank=True,
    )
    event_type = models.CharField(max_length=20, choices=EVENT_TYPE_CHOICES)
    element_id = models.CharField(max_length=100, blank=True)
    element_text = models.CharField(max_length=100, blank=True)
    page_url = models.URLField(blank=True)
    page_title = models.CharField(max_length=200, blank=True)
    event_timestamp = models.DateTimeField()
    # Hashed visitor IP — used only for dedup, never the raw address.
    ip_hash = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['-event_timestamp']
        indexes = [
            models.Index(fields=['client', 'event_timestamp']),
        ]

    def __str__(self):
        return (f'{self.client.firm_name} — {self.event_type} — '
                f'{self.event_timestamp.date()}')


class MonthlyReport(TimestampedModel):
    """A generated monthly performance report PDF for a client."""

    STATUS_CHOICES = [
        ('generating', 'Generating'),
        ('ready', 'Ready'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='monthly_reports',
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='monthly_reports_new', null=True, blank=True,
    )
    report_month = models.DateField(help_text='First day of the reported month.')
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='generating')
    pdf_path = models.CharField(max_length=500, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    opened = models.BooleanField(default=False)
    opened_at = models.DateTimeField(null=True, blank=True)

    # Snapshot of the data at generation time.
    uptime_30d = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True)
    avg_response_ms = models.IntegerField(null=True, blank=True)
    keywords_on_page_1 = models.IntegerField(default=0)
    keywords_improved = models.IntegerField(default=0)
    form_submissions = models.IntegerField(default=0)
    phone_clicks = models.IntegerField(default=0)
    sessions = models.IntegerField(default=0)
    organic_traffic = models.IntegerField(default=0)

    class Meta:
        ordering = ['-report_month']
        unique_together = ['client', 'report_month']

    def __str__(self):
        return f"{self.client.firm_name} — {self.report_month.strftime('%B %Y')}"


class ContentFreshnessReport(TimestampedModel):
    """An admin-only crawl scoring each page of a client's site for freshness."""

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='freshness_reports',
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='freshness_reports_new', null=True, blank=True,
    )
    generated_at = models.DateTimeField(auto_now_add=True)
    pages_analyzed = models.IntegerField(default=0)
    pages_needing_update = models.IntegerField(default=0)
    # List of {url, title, last_modified, word_count_estimate,
    #          freshness_score, priority}.
    report_data = models.JSONField(default=list)

    class Meta:
        ordering = ['-generated_at']
        verbose_name = 'Content Freshness Report'
        verbose_name_plural = 'Content Freshness Reports'

    def __str__(self):
        return f'{self.client.firm_name} — Freshness — {self.generated_at.date()}'


class NPSSurvey(TimestampedModel):
    """A Net Promoter Score survey sent to a maintenance client."""

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='nps_surveys',
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='nps_surveys_new', null=True, blank=True,
    )
    sent_at = models.DateTimeField(auto_now_add=True)
    survey_token = models.UUIDField(default=uuid.uuid4, unique=True)
    score = models.IntegerField(
        null=True, blank=True, help_text='0-10; null = not yet responded.')
    feedback = models.TextField(blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    response_action_taken = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['-sent_at']
        verbose_name = 'NPS Survey'
        verbose_name_plural = 'NPS Surveys'

    def __str__(self):
        score = str(self.score) if self.score is not None else 'No response'
        return f'{self.client.firm_name} — NPS {score}'


class BlogPost(TimestampedModel):
    """An AI-generated blog post draft awaiting staff review."""

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('review', 'Needs Review'),
        ('approved', 'Approved'),
        ('published', 'Published'),
        ('rejected', 'Rejected'),
    ]

    LENGTH_CHOICES = [
        ('short', 'Short (~500w)'),
        ('medium', 'Medium (~800w)'),
        ('long', 'Long (~1200w)'),
    ]
    TONE_CHOICES = [
        ('professional', 'Professional'),
        ('conversational', 'Conversational'),
        ('authoritative', 'Authoritative'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='blog_posts',
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='blog_posts_new', null=True, blank=True,
    )
    topic = models.CharField(max_length=200)
    target_keyword = models.CharField(max_length=200, blank=True)
    # Original generation parameters — reused by "Regenerate".
    requested_length = models.CharField(
        max_length=10, blank=True, choices=LENGTH_CHOICES, default='medium')
    requested_tone = models.CharField(
        max_length=20, blank=True, choices=TONE_CHOICES, default='professional')
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='draft')
    title = models.CharField(max_length=300, blank=True)
    content = models.TextField(blank=True, help_text='Full HTML body.')
    meta_description = models.CharField(max_length=160, blank=True)
    word_count = models.IntegerField(default=0)
    generated_by_ai = models.BooleanField(default=True)
    reviewed_by = models.CharField(max_length=100, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_url = models.URLField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.client.firm_name} — {self.topic[:50]}'


class ClientChatbot(TimestampedModel):
    """Per-client AI chatbot configuration."""

    POSITION_CHOICES = [
        ('bottom-right', 'Bottom Right'),
        ('bottom-left', 'Bottom Left'),
    ]
    DEFAULT_GREETING = (
        "Hi! I'm here to help answer questions about our services. "
        "How can I assist you?"
    )

    client = models.OneToOneField(
        ClientProfile, on_delete=models.CASCADE, related_name='chatbot',
    )
    # Phase A — chatbot is per-Website (each site has its own visitor JS).
    website_new = models.OneToOneField(
        'clients.Website', on_delete=models.CASCADE,
        related_name='chatbot_new', null=True, blank=True,
    )
    is_active = models.BooleanField(default=False)
    greeting_message = models.TextField(default=DEFAULT_GREETING)
    system_prompt = models.TextField(
        blank=True, help_text='Built from practice areas + FAQs.')
    faq_text = models.TextField(
        blank=True, help_text='Raw FAQ notes used to build the system prompt.')
    primary_color = models.CharField(max_length=7, default='#E8650A')
    position = models.CharField(
        max_length=12, choices=POSITION_CHOICES, default='bottom-right')
    total_conversations = models.IntegerField(default=0)
    leads_captured = models.IntegerField(default=0)

    def __str__(self):
        status = 'Active' if self.is_active else 'Inactive'
        return f'{self.client.firm_name} — Chatbot ({status})'


class ChatbotConversation(TimestampedModel):
    """A single visitor conversation with a client's chatbot."""

    chatbot = models.ForeignKey(
        ClientChatbot, on_delete=models.CASCADE, related_name='conversations',
    )
    session_id = models.CharField(max_length=100)
    # [{"role": "user/assistant", "content": "...", "timestamp": "..."}]
    messages = models.JSONField(default=list)
    visitor_name = models.CharField(max_length=100, blank=True)
    visitor_email = models.EmailField(blank=True)
    visitor_phone = models.CharField(max_length=20, blank=True)
    lead_captured = models.BooleanField(default=False)
    started_at = models.DateTimeField(auto_now_add=True)
    # auto_now so it tracks the most recent message, as the name implies.
    last_message_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-started_at']

    def __str__(self):
        return f'{self.chatbot.client.firm_name} — Chat {self.session_id[:8]}'


# ── Phase 6c — vulnerability scanner ──────────────────────────────────────

class VulnerabilityScan(TimestampedModel):
    """
    One scan run for one client — orchestrates nmap / Nikto / SSL Labs /
    WPScan against the client's live URL and Droplet IP. The raw_*
    JSON fields hold the unfiltered tool output for forensic re-parsing;
    the parsed-and-classified bits land as VulnerabilityFinding rows.
    """

    SCAN_TYPE_CHOICES = [
        ('full', 'Full Scan'),
        ('ssl', 'SSL/TLS Only'),
        ('ports', 'Port Scan Only'),
        ('web', 'Web Vulnerabilities Only'),
        ('quick', 'Quick Scan'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('complete', 'Complete'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='vulnerability_scans',
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='vulnerability_scans_new', null=True, blank=True,
    )
    target_url = models.URLField(blank=True)
    target_ip = models.CharField(max_length=100, blank=True)
    scan_type = models.CharField(
        max_length=10, choices=SCAN_TYPE_CHOICES, default='full')
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default='pending')

    # Celery task ID — stored on dispatch so admin can revoke a
    # stuck/long-running scan from the detail page without SSHing in.
    # Empty string for pre-Celery-tracked scans (older rows).
    celery_task_id = models.CharField(max_length=80, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Raw tool outputs — kept verbatim so a finding can always be traced
    # back to the underlying scanner run that produced it.
    raw_nmap = models.JSONField(default=dict, blank=True)
    raw_nikto = models.JSONField(default=dict, blank=True)
    raw_ssl = models.JSONField(default=dict, blank=True)
    raw_wpscan = models.JSONField(default=dict, blank=True)

    # Parsed summary — denormalised so the list page renders without
    # an N+1 over findings.
    findings_count = models.IntegerField(default=0)
    critical_count = models.IntegerField(default=0)
    high_count = models.IntegerField(default=0)
    medium_count = models.IntegerField(default=0)
    low_count = models.IntegerField(default=0)
    info_count = models.IntegerField(default=0)

    # Report delivery
    pdf_path = models.CharField(max_length=500, blank=True)
    sent_to_client = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)

    # True if triggered by Celery beat, False if triggered manually
    # from the admin dashboard.
    is_scheduled = models.BooleanField(default=False)

    # Flipped True the first time an admin opens scan_detail. Drives
    # the Today's Focus widget on the admin home: only scans with
    # critical findings that have NOT been reviewed appear there, so
    # the list stays focused on "unseen" work instead of nagging
    # about every old scan with criticals on it.
    been_reviewed = models.BooleanField(default=False)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Vulnerability Scan'
        verbose_name_plural = 'Vulnerability Scans'

    def __str__(self):
        return (f'{self.client.firm_name} — '
                f'{self.scan_type} — {self.created_at.date()}')

    def get_severity_summary(self):
        return {
            'critical': self.critical_count,
            'high': self.high_count,
            'medium': self.medium_count,
            'low': self.low_count,
            'info': self.info_count,
        }


class VulnerabilityFinding(TimestampedModel):
    """
    One parsed finding from a single tool. `evidence` keeps the exact
    output snippet that triggered the classification so the admin can
    verify, and `status` lets a finding be marked Accepted Risk / False
    Positive / Resolved without losing the audit trail.
    """

    SEVERITY_CHOICES = [
        ('critical', 'Critical'),
        ('high', 'High'),
        ('medium', 'Medium'),
        ('low', 'Low'),
        ('info', 'Informational'),
    ]
    TOOL_CHOICES = [
        ('nmap', 'nmap'),
        ('nikto', 'Nikto'),
        ('ssl', 'SSL Labs'),
        ('wpscan', 'WPScan'),
        ('manual', 'Manual'),
    ]
    STATUS_CHOICES = [
        ('open', 'Open'),
        ('accepted_risk', 'Accepted Risk'),
        ('false_positive', 'False Positive'),
        ('resolved', 'Resolved'),
    ]
    # Index-friendly severity ordering for sort/group operations.
    SEVERITY_ORDER = {
        'critical': 0, 'high': 1, 'medium': 2, 'low': 3, 'info': 4,
    }

    scan = models.ForeignKey(
        VulnerabilityScan, on_delete=models.CASCADE,
        related_name='findings',
    )
    severity = models.CharField(
        max_length=10, choices=SEVERITY_CHOICES, default='info')
    tool = models.CharField(
        max_length=10, choices=TOOL_CHOICES, default='manual')

    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    recommendation = models.TextField(blank=True)
    evidence = models.TextField(
        blank=True,
        help_text='Raw output snippet that triggered this finding.',
    )
    cve_id = models.CharField(max_length=50, blank=True)

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='open')
    accepted_by = models.CharField(max_length=100, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    acceptance_note = models.TextField(blank=True)

    class Meta:
        ordering = ['severity', 'tool', 'title']
        verbose_name = 'Vulnerability Finding'
        verbose_name_plural = 'Vulnerability Findings'

    def __str__(self):
        return f'[{self.severity.upper()}] {self.title[:60]}'

    @property
    def cve_id_list(self):
        """Split the comma/space-separated cve_id field into clean IDs."""
        if not self.cve_id:
            return []
        return [c.strip() for c in self.cve_id.replace(',', ' ').split()
                if c.strip()]


# ── Tier 1 analytics — one row per page view ──────────────────────────────

class PageSession(TimestampedModel):
    """
    One record per page view from the v2 aspired-tracker.js. The
    tracker batches everything that happens on a single page (scroll,
    clicks, exit intent, time on page) and ships it in one beacon
    on unload — `track_batch` lands here.

    `raw_events` keeps the full event stream (capped at 100) so we can
    rebuild a session view later without re-instrumenting the page.
    Conversion event counts (form_submits, phone_clicks, cta_clicks)
    are denormalised for fast aggregate queries on the dashboard.
    """

    client = models.ForeignKey(
        'clients.ClientProfile',
        on_delete=models.CASCADE,
        related_name='page_sessions',
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='page_sessions_new', null=True, blank=True,
    )
    session_id = models.CharField(max_length=100)
    # Browser-generated UUID per page view (no cookies).

    page_url = models.URLField(max_length=2000, blank=True)
    page_title = models.CharField(max_length=200, blank=True)

    # Time metrics.
    time_on_page_seconds = models.IntegerField(
        null=True, blank=True)

    # Scroll metrics — 0-100 percentage.
    max_scroll_depth = models.IntegerField(null=True, blank=True)
    scroll_milestones_hit = models.JSONField(default=list, blank=True)
    # e.g. [25, 50, 75]

    # Engagement signal.
    exit_intent_fired = models.BooleanField(default=False)

    # Click coordinates — list of
    # {x_pct, y_pct, tag, text, ts}.
    click_heatmap = models.JSONField(default=list, blank=True)

    # Conversion events on this page (denormalised counts).
    form_submits = models.IntegerField(default=0)
    phone_clicks = models.IntegerField(default=0)
    cta_clicks = models.IntegerField(default=0)

    # Raw event stream (capped at 100 in the view).
    raw_events = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Page Session'
        verbose_name_plural = 'Page Sessions'
        indexes = [
            models.Index(fields=['client', '-created_at']),
            models.Index(fields=['client', 'session_id']),
        ]

    def __str__(self):
        return (f'{self.client.firm_name} — '
                f'{(self.page_url or "")[:50]} — '
                f'{self.created_at.date()}')


# ── Tier 2 session recording (rrweb) ──────────────────────────────────────

class SessionRecording(TimestampedModel):
    """
    Full rrweb session replay for one page view. Only created when
    `ClientProfile.session_recording_enabled` is True. Each row holds
    the recording chunks (each chunk = list of rrweb events) the
    in-browser recorder beacons up every ~10 seconds.

    Auto-deleted 30 days after creation by the
    `delete_expired_recordings` Celery beat task. Clients can download
    any session as a self-contained HTML before that.
    """

    STATUS_CHOICES = [
        ('recording', 'Recording'),  # still receiving chunks
        ('complete', 'Complete'),    # final chunk received
        ('expired', 'Expired'),
    ]

    client = models.ForeignKey(
        'clients.ClientProfile',
        on_delete=models.CASCADE,
        related_name='session_recordings',
    )
    website_new = models.ForeignKey(
        'clients.Website', on_delete=models.CASCADE,
        related_name='session_recordings_new', null=True, blank=True,
    )
    session_id = models.CharField(max_length=100, db_index=True)
    # Same session_id as the matching PageSession row.

    page_url = models.URLField(max_length=2000, blank=True)
    page_title = models.CharField(max_length=200, blank=True)

    # Recording data — list of rrweb event chunks.
    recording_chunks = models.JSONField(default=list, blank=True)

    viewport_width = models.IntegerField(null=True, blank=True)
    viewport_height = models.IntegerField(null=True, blank=True)
    duration_seconds = models.IntegerField(null=True, blank=True)

    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default='recording')

    # 30-day retention.
    expires_at = models.DateTimeField()

    # Size tracking (rough — sum of JSON-serialised chunk sizes).
    estimated_size_kb = models.IntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Session Recording'
        verbose_name_plural = 'Session Recordings'
        indexes = [
            models.Index(
                fields=['client', 'status', '-created_at']),
            # Index on expires_at so the nightly delete task can
            # find every expired row without scanning the table.
            models.Index(fields=['expires_at']),
        ]

    def save(self, *args, **kwargs):
        if not self.expires_at:
            from datetime import timedelta as _td

            from django.utils import timezone as _tz
            self.expires_at = _tz.now() + _td(days=30)
        super().save(*args, **kwargs)

    def __str__(self):
        return (f'{self.client.firm_name} — '
                f'{(self.page_url or "")[:40]} — '
                f'{self.created_at.date()}')

    @property
    def days_until_expiry(self):
        from django.utils import timezone as _tz
        delta = self.expires_at - _tz.now()
        return max(0, delta.days)

    @property
    def size_display(self):
        kb = self.estimated_size_kb or 0
        if kb >= 1024:
            return f'{kb / 1024:.1f} MB'
        return f'{kb} KB'

    @property
    def duration_display(self):
        s = self.duration_seconds
        if not s:
            return '—'
        if s < 60:
            return f'{s}s'
        return f'{s // 60}m {s % 60}s'

    def get_all_events(self):
        """All rrweb events merged from every chunk, in order."""
        out = []
        for chunk in (self.recording_chunks or []):
            if isinstance(chunk, list):
                out.extend(chunk)
        return out


# ── Anthropic / Claude API usage tracking ───────────────────────────────────

# Per-million-token USD pricing for each model the project uses.
# Anthropic updates these occasionally — if the numbers below stop
# matching console.anthropic.com, just edit the dict; the cost
# computation reads it at call time.
CLAUDE_PRICING_USD_PER_MTOK = {
    'claude-sonnet-4-6':            {'input': 3.00,  'output': 15.00},
    'claude-haiku-4-5-20251001':    {'input': 0.80,  'output': 4.00},
    'claude-opus-4-7':              {'input': 15.00, 'output': 75.00},
}


class ClaudeUsage(models.Model):
    """
    Per-month, per-model token + cost accumulator.

    One row per (year_month, model). Every Claude API call across
    the project (reporting.ai.claude_complete + the three direct
    HTTP calls in clients/intelligence.py + the AI assistant in
    admin_dashboard/views.py) calls ``ClaudeUsage.record()`` after
    a successful response and increments the row's counts atomically
    via F() expressions.

    Drives the AI Usage widget on /admin-dashboard/ so the admin
    can see this month's token + dollar burn at a glance. Per-month
    granularity keeps the table tiny — ~3 rows per month maximum
    (one per model in use).
    """

    # First day of the month. Combined with model = unique key.
    year_month = models.DateField()
    model = models.CharField(max_length=64)

    input_tokens = models.BigIntegerField(default=0)
    output_tokens = models.BigIntegerField(default=0)
    request_count = models.IntegerField(default=0)
    last_request_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-year_month', 'model']
        unique_together = ['year_month', 'model']
        verbose_name = 'Claude API Usage'
        verbose_name_plural = 'Claude API Usage'

    def __str__(self):
        return (f'{self.year_month:%B %Y} — {self.model} — '
                f'{self.input_tokens + self.output_tokens} tokens')

    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens

    @property
    def cost_usd(self):
        """USD cost for this row based on current pricing."""
        rates = CLAUDE_PRICING_USD_PER_MTOK.get(self.model)
        if not rates:
            return 0.0
        return (
            (self.input_tokens / 1_000_000) * rates['input']
            + (self.output_tokens / 1_000_000) * rates['output']
        )

    @staticmethod
    def _current_month():
        from django.utils import timezone as _tz
        return _tz.now().date().replace(day=1)

    @classmethod
    def record(cls, model, input_tokens, output_tokens):
        """Atomic-increment THIS month's row for the given model.
        Creates the row on first call of a new month. Safe under
        concurrent writes (uses F() expressions). Quiet no-op if
        model is empty or token counts are zero so callers don't
        need to branch."""
        if not model:
            return
        try:
            input_tokens = int(input_tokens or 0)
            output_tokens = int(output_tokens or 0)
        except (TypeError, ValueError):
            return
        if input_tokens == 0 and output_tokens == 0:
            return

        from django.db.models import F
        ym = cls._current_month()
        cls.objects.get_or_create(
            year_month=ym, model=model)
        cls.objects.filter(year_month=ym, model=model).update(
            input_tokens=F('input_tokens') + input_tokens,
            output_tokens=F('output_tokens') + output_tokens,
            request_count=F('request_count') + 1,
        )

    @classmethod
    def current_month_summary(cls):
        """Aggregated summary for THIS month — one entry per model
        plus a grand-total row. Used by the dashboard widget."""
        ym = cls._current_month()
        rows = list(cls.objects.filter(year_month=ym))
        per_model = []
        total_tokens = 0
        total_cost = 0.0
        total_requests = 0
        for r in rows:
            cost = r.cost_usd
            tokens = r.total_tokens
            per_model.append({
                'model': r.model,
                'input_tokens': r.input_tokens,
                'output_tokens': r.output_tokens,
                'tokens': tokens,
                'requests': r.request_count,
                'cost_usd': cost,
            })
            total_tokens += tokens
            total_cost += cost
            total_requests += r.request_count
        # Stable ordering: cheapest first (Haiku) → priciest (Opus).
        # Sorts on the model name as a proxy (cmp by string) which
        # is fine since the names are stable and there are ≤ 3.
        per_model.sort(key=lambda x: x['model'])
        return {
            'per_model': per_model,
            'total_tokens': total_tokens,
            'total_cost_usd': total_cost,
            'total_requests': total_requests,
        }
