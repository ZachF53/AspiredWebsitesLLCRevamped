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
