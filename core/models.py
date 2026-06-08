"""Shared base models for the project."""

import uuid

from django.db import models
from django.utils import timezone


class TimestampedModel(models.Model):
    """
    Abstract base for all portal (clients/) and sync/ models.

    Uses a UUID primary key so Aspired and Moonieful record IDs never collide
    across the sync bridge (two separate databases, one shared ID space).
    """

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    created_at = models.DateTimeField(
        default=timezone.now,
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SystemAlert(models.Model):
    """
    A single operator-facing alert — usually an exception caught in a
    silent-fallback code path (email failures, webhook crashes, etc.).

    Written via ``core.system_alerts.record_alert``. Surfaced as a
    banner on /admin-dashboard/ until the operator clicks Resolve.
    """

    SEVERITY_CHOICES = [
        ('info',     'Info'),
        ('warning',  'Warning'),
        ('error',    'Error'),
        ('critical', 'Critical'),
    ]

    severity = models.CharField(
        max_length=10, choices=SEVERITY_CHOICES, default='error',
    )
    source = models.CharField(
        max_length=80,
        help_text='Where the alert originated, e.g. '
                  '"scheduler.google_calendar".',
    )
    message = models.CharField(max_length=255)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['resolved_at', '-created_at']),
        ]

    def __str__(self):
        return f'{self.severity}/{self.source}: {self.message[:60]}'
