"""
Phase D — service-specific models.

These split the legacy "everything on ClientProfile" world into per-
service rows. An Account can have 0..N of each. Sidebar nav inspects
which sets are non-empty to decide what to show.

MaintenancePlan — D1. Each row = one active maintenance subscription
                  belonging to an Account, optionally tied to a
                  specific Website (the site we maintain). When
                  `website` is NULL, the customer brings their own
                  hosting elsewhere.

SocialMediaPlan + SocialChannel — D2.

Droplet         — D3. Split from Website.do_droplet_id so we can
                  represent move-over droplets that aren't attached
                  to one of our builds.

Phase D is intentionally **additive** — none of the legacy
ClientProfile fields get touched until D7. Reads of the new models
roll out gradually; existing reads stay working the whole time.
"""

import uuid

from django.db import models

from core.models import TimestampedModel


# ── D1 — Maintenance ──────────────────────────────────────────────────

class MaintenancePlan(TimestampedModel):
    """One active maintenance subscription belonging to an Account."""

    TIER_CHOICES = [
        ('maintenance-essentials', 'Essentials'),
        ('maintenance-growth',     'Growth'),
        ('maintenance-dominant',   'Dominant'),
    ]
    STATUS_CHOICES = [
        ('active',    'Active'),
        ('paused',    'Paused — paid but suspended'),
        ('cancelled', 'Cancelled — at period end'),
        ('ended',     'Ended — fully cancelled'),
    ]

    account = models.ForeignKey(
        'clients.Account', on_delete=models.CASCADE,
        related_name='maintenance_plans',
    )
    # Nullable — when a customer brings us a site we did NOT build
    # AND they don't opt for hosting move-over, there's no Website
    # row. In that case the maintenance contract references an
    # external URL on the plan itself.
    website = models.ForeignKey(
        'clients.Website', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='maintenance_plans',
    )
    # For sites we don't host or build, the customer's URL of record.
    external_site_url = models.URLField(blank=True)

    tier_slug = models.CharField(
        max_length=50, choices=TIER_CHOICES,
        default='maintenance-essentials',
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='active',
    )
    stripe_subscription_id = models.CharField(max_length=80, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    # Hosting move-over add-on (one-time $50-off-first-year purchase)
    hosting_move_over = models.BooleanField(default=False)
    hosting_move_over_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-started_at', '-created_at']

    def __str__(self):
        which = (self.website.url if self.website
                 else self.external_site_url or 'no site')
        return f'{self.account.name} — {self.get_tier_slug_display()} ({which})'


# ── D2 — Social media ────────────────────────────────────────────────

class SocialMediaPlan(TimestampedModel):
    """One active social media subscription belonging to an Account."""

    TIER_CHOICES = [
        ('social-basic',    'Basic'),
        ('social-standard', 'Standard'),
        ('social-full',     'Full Management'),
    ]
    STATUS_CHOICES = [
        ('active',    'Active'),
        ('paused',    'Paused'),
        ('cancelled', 'Cancelled — at period end'),
        ('ended',     'Ended'),
    ]

    account = models.ForeignKey(
        'clients.Account', on_delete=models.CASCADE,
        related_name='social_media_plans',
    )
    tier_slug = models.CharField(
        max_length=50, choices=TIER_CHOICES, default='social-basic',
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='active',
    )
    stripe_subscription_id = models.CharField(max_length=80, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    # Max channel count for the tier — cached at signup so we don't
    # have to re-query ServiceTier every time.
    max_channels = models.IntegerField(default=2)

    class Meta:
        ordering = ['-started_at', '-created_at']

    def __str__(self):
        return f'{self.account.name} — {self.get_tier_slug_display()}'


class SocialChannel(TimestampedModel):
    """One social media channel attached to a SocialMediaPlan."""

    PLATFORM_CHOICES = [
        ('facebook',  'Facebook'),
        ('instagram', 'Instagram'),
        ('linkedin',  'LinkedIn'),
        # Phase 5a — Google Business Profile. Same OAuth token (with
        # scopes business.manage + webmasters.readonly) ALSO unblocks
        # Phase 6 NAP sync + Search Console keyword tracking.
        ('gbp',       'Google Business Profile'),
        ('twitter',   'X (Twitter)'),
        ('tiktok',    'TikTok'),
        ('youtube',   'YouTube'),
        ('pinterest', 'Pinterest'),
        ('threads',   'Threads'),
        ('other',     'Other'),
    ]
    STATUS_CHOICES = [
        ('active',  'Active — posting'),
        ('dormant', 'Has account, dormant'),
        ('pending', 'Need to create'),
    ]
    ACCESS_CHOICES = [
        ('meta_bm',         'Meta Business Manager invite'),
        ('linkedin_admin',  'LinkedIn page admin'),
        ('vault_share',     'Direct login via vault'),
        ('defer',           'Will figure it out later'),
    ]

    plan = models.ForeignKey(
        SocialMediaPlan, on_delete=models.CASCADE,
        related_name='channels',
    )
    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    handle = models.CharField(
        max_length=200,
        help_text='Account URL or @handle.',
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='active',
    )
    follower_count = models.CharField(
        max_length=40, blank=True,
        help_text='Free text — "approximately 2,500".',
    )
    access_method = models.CharField(
        max_length=30, choices=ACCESS_CHOICES, blank=True,
    )

    class Meta:
        ordering = ['plan', 'platform']

    def __str__(self):
        return f'{self.get_platform_display()} — {self.handle}'


# ── D3 — Droplet ─────────────────────────────────────────────────────

class Droplet(TimestampedModel):
    """One DigitalOcean droplet tied to an Account."""

    SOURCE_CHOICES = [
        ('build',        'Provisioned for a build'),
        ('move_over',    'Move-over from existing host'),
        ('self_provision', 'Self-provisioned by customer'),
    ]
    STATUS_CHOICES = [
        ('provisioning', 'Provisioning'),
        ('active',       'Active'),
        ('maintenance',  'Maintenance mode (503)'),
        ('offline',      'Offline'),
        ('destroyed',    'Destroyed'),
    ]

    account = models.ForeignKey(
        'clients.Account', on_delete=models.CASCADE,
        related_name='droplets',
    )
    # Optional — when the droplet hosts one of our built sites,
    # tie it back. NULL = move-over / self-provision droplet that
    # isn't tied to a Website row.
    website = models.ForeignKey(
        'clients.Website', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='droplets',
    )

    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default='build',
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='provisioning',
    )

    do_droplet_id = models.CharField(max_length=40, blank=True)
    do_droplet_ip = models.GenericIPAddressField(null=True, blank=True)
    do_region = models.CharField(max_length=20, blank=True, default='nyc1')
    do_size = models.CharField(max_length=40, blank=True)
    do_snapshot_id = models.CharField(max_length=40, blank=True)

    provisioned_at = models.DateTimeField(null=True, blank=True)
    destroyed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-provisioned_at', '-created_at']

    def __str__(self):
        return (f'{self.account.name} — Droplet '
                f'{self.do_droplet_id or "(no DO id)"}')
