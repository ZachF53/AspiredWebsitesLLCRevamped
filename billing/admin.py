"""Admin registrations for billing models."""

from django.contrib import admin
from django.utils.html import format_html

from .models import AddonPricing, MiniInvoice, ServiceTier, TierFeature


@admin.register(MiniInvoice)
class MiniInvoiceAdmin(admin.ModelAdmin):
    list_display = (
        'client', 'description', 'amount', 'hours', 'status', 'created_at',
    )
    list_filter = ('status',)
    search_fields = ('client__firm_name', 'description', 'stripe_invoice_id')
    readonly_fields = ('created_at', 'updated_at')


class TierFeatureInline(admin.TabularInline):
    model = TierFeature
    extra = 1
    fields = ('text', 'sort_order', 'is_highlight')
    ordering = ('sort_order',)


@admin.register(ServiceTier)
class ServiceTierAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'category', 'price', 'stripe_status', 'is_active',
        'is_featured', 'sort_order',
    )
    list_filter = ('category', 'is_active', 'is_featured')
    list_editable = ('is_active', 'is_featured', 'sort_order')
    search_fields = ('name', 'slug', 'stripe_price_id')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [TierFeatureInline]
    readonly_fields = ('created_at', 'updated_at')

    @admin.display(description='Stripe Price ID')
    def stripe_status(self, obj):
        """Flag active recurring tiers that can't yet take payments."""
        if obj.stripe_price_id:
            return obj.stripe_price_id
        if obj.is_active and obj.is_recurring:
            return format_html(
                '<strong style="color:#E8650A;">&#9888; Not set — '
                'cannot accept payments</strong>'
            )
        return format_html('<span style="color:#999;">—</span>')


@admin.register(AddonPricing)
class AddonPricingAdmin(admin.ModelAdmin):
    list_display = ('name', 'price_min', 'price_max', 'unit', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ('created_at', 'updated_at')
