"""CancellationReason — capture optional 'why' on subscription cancels."""

import uuid

from django.conf import settings
from django.db import models


class CancellationReason(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='cancellation_reasons',
    )
    subscription_id = models.CharField(max_length=80)
    reason = models.TextField()
    cancelled_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-cancelled_at']
