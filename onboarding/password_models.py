"""
PasswordSetupToken — the one-time magic link a customer clicks to set
their password after a self-checkout purchase. Token expires in 7 days.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class PasswordSetupToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='password_setup_tokens',
    )
    token = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.expires_at:
            import datetime
            self.expires_at = timezone.now() + datetime.timedelta(days=7)
        super().save(*args, **kwargs)

    @property
    def is_valid(self):
        return (
            self.consumed_at is None and self.expires_at > timezone.now()
        )

    @classmethod
    def create_for(cls, user):
        """Always create a fresh token; old ones for the user stay
        valid until they're explicitly consumed or expired naturally."""
        return cls.objects.create(user=user)
