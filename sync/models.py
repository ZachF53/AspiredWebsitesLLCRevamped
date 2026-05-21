"""
Moonieful sync bridge models — Phase 3.

SyncJob is the outbound queue (Aspired → Moonieful). SyncLog is the inbound
audit trail (Moonieful → Aspired). Both inherit TimestampedModel per CLAUDE.md.
"""

from django.db import models

from clients.models import ClientProfile
from core.models import TimestampedModel


SYNC_EVENT_CHOICES = [
    ('client_created', 'Client Created'),
    ('client_updated', 'Client Updated'),
    ('revision_created', 'Revision Created'),
    ('stage_changed', 'Stage Changed'),
    ('project_complete', 'Project Complete'),
    ('document_added', 'Document Added'),
    ('maintenance_activated', 'Maintenance Activated'),
]


class SyncJob(TimestampedModel):
    """An outbound sync event queued for delivery to the other site."""

    TARGET_CHOICES = [
        ('moonieful', 'Moonieful'),
        ('aspired', 'Aspired'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
    ]

    target = models.CharField(max_length=20, choices=TARGET_CHOICES)
    client = models.ForeignKey(
        ClientProfile, on_delete=models.CASCADE, null=True, blank=True,
        related_name='sync_jobs',
    )
    moonieful_client_id = models.UUIDField(null=True, blank=True)
    event_type = models.CharField(max_length=30, choices=SYNC_EVENT_CHOICES)
    payload = models.JSONField(default=dict, blank=True)
    # Frozen at job creation — the HMAC/timestamp are recomputed fresh on every
    # send attempt, but this snapshot of the data never changes.
    payload_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='pending')
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Sync Job'
        verbose_name_plural = 'Sync Jobs'

    def __str__(self):
        return f'{self.event_type} → {self.target} ({self.status})'


class SyncLog(TimestampedModel):
    """An audit-trail record of every inbound sync event received."""

    STATUS_CHOICES = [
        ('processed', 'Processed'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
    ]

    source_site = models.CharField(max_length=100, blank=True)
    event_type = models.CharField(max_length=100, blank=True)
    payload_received = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Sync Log'
        verbose_name_plural = 'Sync Logs'

    def __str__(self):
        return f'{self.source_site}/{self.event_type} ({self.status})'
