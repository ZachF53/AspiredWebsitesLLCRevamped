"""Admin registrations for billing models."""

from django.contrib import admin, messages
from django.utils.html import format_html

from .models import AddonPricing, MiniInvoice, ServiceTier, TierFeature


@admin.register(MiniInvoice)
class MiniInvoiceAdmin(admin.ModelAdmin):
    list_display = (
        'account_new', 'description', 'amount', 'hours', 'status', 'created_at',
    )
    list_filter = ('status',)
    search_fields = ('account_new__name', 'description', 'stripe_invoice_id')
    readonly_fields = ('created_at', 'updated_at')
    actions = ['send_via_stripe']

    @admin.action(description='Send selected pending MiniInvoices via Stripe')
    def send_via_stripe(self, request, queryset):
        """Phase 1.3 — bulk-send pending MiniInvoices through Stripe.
        Skips any with amount <= 0 or already 'sent'/'paid'."""
        from billing.stripe_helpers import StripeNotConfigured, send_mini_invoice

        sent = 0
        skipped = 0
        errors = []
        for mini in queryset:
            if mini.status not in ('pending',):
                skipped += 1
                continue
            if not mini.amount or mini.amount <= 0:
                errors.append(
                    f'{mini}: amount is {mini.amount}; set it first')
                continue
            try:
                send_mini_invoice(mini)
                sent += 1
            except StripeNotConfigured:
                errors.append(f'{mini}: STRIPE_SECRET_KEY not configured')
                break
            except Exception as exc:
                errors.append(f'{mini}: {exc}')

        if sent:
            self.message_user(
                request, f'Sent {sent} MiniInvoice(s) via Stripe.',
                messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                f'Skipped {skipped} (not in "pending" status).',
                messages.WARNING)
        for err in errors:
            self.message_user(request, err, messages.ERROR)


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
