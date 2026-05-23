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
    ]

    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE,
        related_name='vulnerability_scans',
    )
    target_url = models.URLField(blank=True)
    target_ip = models.CharField(max_length=100, blank=True)
    scan_type = models.CharField(
        max_length=10, choices=SCAN_TYPE_CHOICES, default='full')
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default='pending')

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
