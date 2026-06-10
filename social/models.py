"""
Phase 5a — Social Media Manager (Google Business Profile first).

Three models, all UUID PK via TimestampedModel:

  SocialToken         OAuth credentials per SocialChannel (server-key
                      encrypted via vault.crypto.derive_server_key).
                      Background Celery tasks decrypt without an admin
                      PIN session — that's why we use the server key,
                      not the PIN key.
  ScheduledPost       Operator-drafted post bound to one SocialChannel,
                      with a scheduled_for timestamp. The auto-publisher
                      (social.tasks.publish_due_posts) picks these up
                      every 5 min.
  PostResult          Per-attempt outcome — provider post id, permalink,
                      success bool, error_detail. Insights pull-back
                      (Phase 5b) augments these rows with likes/etc.

Encryption note: rotating VAULT_SERVER_SECRET INVALIDATES every stored
token. Same rule as billing/do_helpers.py SSH credentials. Don't rotate
without a migration that re-encrypts each row first.
"""

from django.conf import settings
from django.db import models

from core.models import TimestampedModel


class SocialToken(TimestampedModel):
    """OAuth credentials for one SocialChannel. One-to-one — each
    channel has at most one connected provider account."""

    channel = models.OneToOneField(
        'clients.SocialChannel',
        on_delete=models.CASCADE,
        related_name='token',
    )
    # Both ciphertext fields — never store plaintext at rest. Wrappers
    # in social.crypto centralise the key choice (server key) so we
    # don't sprinkle derive_server_key() calls across publishers.
    access_token_encrypted = models.TextField(blank=True)
    refresh_token_encrypted = models.TextField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    # Space-separated scope list as returned by the provider. Phase 6
    # checks this before calling Search Console — if the expected scope
    # is missing we surface a "Re-connect for SEO features" CTA rather
    # than silently failing.
    scopes = models.CharField(max_length=500, blank=True)
    # The provider's id for the connected account — for GBP this is the
    # Google account number; for Meta the page id; for LinkedIn the org URN.
    provider_account_id = models.CharField(max_length=200, blank=True)
    connected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='social_tokens_connected',
    )
    last_refresh_at = models.DateTimeField(null=True, blank=True)
    last_refresh_error = models.TextField(blank=True)

    class Meta:
        indexes = [
            # The refresh sweep task hits this every hour. Explicit
            # index keeps the scan O(rows_due) once we have hundreds
            # of channels.
            models.Index(fields=['expires_at']),
        ]

    def __str__(self):
        return f'{self.channel} — token'


class ScheduledPost(TimestampedModel):
    """One draft / scheduled / posted item attached to a SocialChannel.

    State machine:
        draft → scheduled → publishing → published
                               ↘──────→ failed

    The race-safe transition is the {scheduled → publishing} flip;
    publish_due_posts uses an atomic UPDATE with a status-equals
    guard so two Celery workers can't grab the same row.
    """

    STATUS_CHOICES = [
        ('draft',      'Draft'),
        ('scheduled',  'Scheduled'),
        ('publishing', 'Publishing — in flight'),
        ('published',  'Published'),
        ('failed',     'Failed'),
    ]

    channel = models.ForeignKey(
        'clients.SocialChannel',
        on_delete=models.CASCADE,
        related_name='scheduled_posts',
    )
    # FK to ClientProfile alongside channel for direct admin lookup +
    # context (location / tone) without traversing
    # channel.plan.account.legacy_client_profile every time.
    client = models.ForeignKey(
        'clients.ClientProfile',
        on_delete=models.CASCADE,
        related_name='scheduled_posts',
    )
    # GBP local-post hard cap is 1500 chars; the form should validate
    # client-side too. TextField so a future platform with higher caps
    # doesn't need a model change.
    body = models.TextField()
    # Optional media URL (image / video). For 5a we accept a URL string;
    # ImageField upload integration moves to 5b alongside Meta which
    # has stricter media requirements.
    media_url = models.URLField(blank=True)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='draft',
    )
    published_at = models.DateTimeField(null=True, blank=True)
    ai_generated = models.BooleanField(
        default=False,
        help_text='True if body came from social.ai.generate_post_draft.',
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='scheduled_posts_created',
    )

    class Meta:
        # The auto-publisher hits this combo every 5 min — cover it.
        indexes = [
            models.Index(fields=['status', 'scheduled_for']),
        ]
        ordering = ['-scheduled_for', '-created_at']

    def __str__(self):
        return f'{self.channel} — {self.status} @ {self.scheduled_for}'


class PostResult(TimestampedModel):
    """Per-attempt outcome of publishing a ScheduledPost. One row per
    attempt — retries get their own row so we can audit them."""

    scheduled_post = models.ForeignKey(
        ScheduledPost,
        on_delete=models.CASCADE,
        related_name='results',
    )
    provider_post_id = models.CharField(max_length=255, blank=True)
    permalink = models.URLField(blank=True)
    success = models.BooleanField(default=False)
    error_detail = models.TextField(blank=True)
    attempted_at = models.DateTimeField(null=True, blank=True)
    # Phase 5b will populate these from the daily insights pull. Default 0
    # so the column is non-null without a migration when it's added.
    likes = models.IntegerField(default=0)
    comments = models.IntegerField(default=0)
    reach = models.IntegerField(default=0)

    class Meta:
        ordering = ['-attempted_at', '-created_at']

    def __str__(self):
        tag = 'ok' if self.success else 'FAIL'
        return f'PostResult({tag}) — {self.scheduled_post_id}'
