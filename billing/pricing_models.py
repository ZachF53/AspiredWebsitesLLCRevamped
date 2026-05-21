"""
Pricing models — the database is the single source of truth for every
pricing tier, feature bullet, and Stripe Price ID. Managed from the admin
dashboard at /admin-dashboard/pricing/.
"""

from django.db import models

from core.models import TimestampedModel


class ServiceTier(TimestampedModel):
    """One purchasable plan/product (a build, a maintenance plan, etc.)."""

    CATEGORY_CHOICES = [
        ('website_build', 'Website Build'),
        ('maintenance', 'Monthly Maintenance'),
        ('social_media', 'Social Media Management'),
        ('hosting', 'Hosting'),
        ('addon', 'Add-On'),
    ]

    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100, unique=True)
    tagline = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)

    # Pricing
    price = models.DecimalField(max_digits=10, decimal_places=2)
    price_display = models.CharField(
        max_length=50, blank=True,
        help_text='Auto-generated from price if left blank — e.g. "$599/mo".',
    )

    is_recurring = models.BooleanField(default=False)
    billing_interval = models.CharField(
        max_length=10, blank=True,
        help_text="'month', 'year', or blank for one-time.",
    )

    # Stripe — these replace all the STRIPE_PRICE_* env vars.
    stripe_price_id = models.CharField(max_length=100, blank=True)
    stripe_product_id = models.CharField(max_length=100, blank=True)

    # Display
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)

    # Website-build specific
    pages_included = models.IntegerField(null=True, blank=True)
    practice_areas_included = models.IntegerField(null=True, blank=True)
    timeline_weeks = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ['category', 'sort_order', 'price']

    def __str__(self):
        return f'{self.get_category_display()} — {self.name}'

    def get_price_display(self):
        if self.price_display:
            return self.price_display
        if self.is_recurring:
            interval = self.billing_interval or 'mo'
            return f'${self.price:,.0f}/{interval}'
        return f'${self.price:,.0f}'

    @classmethod
    def get_active(cls, category):
        """Active tiers in a category, with their features prefetched."""
        return cls.objects.filter(
            category=category, is_active=True,
        ).prefetch_related('features')


class TierFeature(TimestampedModel):
    """A single feature bullet shown on a tier's card."""

    tier = models.ForeignKey(
        ServiceTier, on_delete=models.CASCADE, related_name='features',
    )
    text = models.CharField(max_length=300)
    sort_order = models.IntegerField(default=0)
    is_highlight = models.BooleanField(default=False)

    class Meta:
        ordering = ['sort_order']

    def __str__(self):
        return f'{self.tier.name}: {self.text[:60]}'


class AddonPricing(TimestampedModel):
    """Per-unit pricing for add-ons (hourly work, extra pages, etc.)."""

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=50, unique=True)
    description = models.TextField(blank=True)
    price_min = models.DecimalField(max_digits=10, decimal_places=2)
    price_max = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
    )
    unit = models.CharField(max_length=50, blank=True)
    stripe_price_id = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def get_price_display(self):
        if self.price_max:
            return f'${self.price_min:,.0f}–${self.price_max:,.0f} {self.unit}'.strip()
        return f'${self.price_min:,.0f} {self.unit}'.strip()
