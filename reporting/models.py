"""
Reporting models — GBP NAP sync checks, keyword rank tracking, and
conversion-element events. All feed the monthly PDF report and the portal.
"""

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
