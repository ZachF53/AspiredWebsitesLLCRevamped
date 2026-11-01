"""Admin registrations for billing models."""

from django.contrib import admin, messages
from django.utils.html import format_html

from .models import (
    AddonPricing, MiniInvoice, ServiceTier, StripeWebhookEvent,
    TermsAcceptance, TierFeature,
)


@admin.register(StripeWebhookEvent)
class StripeWebhookEventAdmin(admin.ModelAdmin):
    """The processed-event ledger. Read-only, with one exception.

    Deleting a row is allowed, and it is the whole operational point of
    having this in the admin: if an event was claimed and then failed
    (last_error is set), removing the row is what lets a Stripe
    "Resend" actually re-run it. Without that, a half-processed event
    would be permanently un-replayable.

    Adding and editing are not allowed. A hand-written event id would
    silently suppress a real event the next time Stripe sent it.
    """

    list_display = ('event_type', 'event_id', 'processed_at', 'has_error')
    list_filter = ('event_type', 'processed_at')
    search_fields = ('event_id', 'event_type', 'last_error')
    date_hierarchy = 'processed_at'
    ordering = ('-processed_at',)

    @admin.display(boolean=True, description='Errored')
    def has_error(self, obj):
        return bool(obj.last_error)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(TermsAcceptance)
class TermsAcceptanceAdmin(admin.ModelAdmin):
    """Evidence, so deliberately not editable from here.

    The whole value of this table is that a row says what the buyer's
    browser actually did. An admin who can retype the version or the
    timestamp turns it from evidence into an assertion, which is exactly
    what it would need to stop being in a chargeback or a regulator
    enquiry. Rows are created by the checkout view and completed by the
    Stripe webhook; nothing else should write them.

    Delete is left off too. A trial signup that someone wants erased is
    a data-subject request, which goes through the normal deletion path
    with a record of why, not a quiet row removal.
    """

    list_display = ('email', 'product_slug', 'terms_version', 'accepted_at',
                    'ip_address', 'stripe_subscription_id')
    list_filter = ('product_slug', 'terms_version', 'accepted_at')
    search_fields = ('email', 'stripe_customer_id', 'stripe_subscription_id',
                     'stripe_checkout_session_id')
    date_hierarchy = 'accepted_at'
    ordering = ('-accepted_at',)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # False would hide the row entirely on some Django versions.
        # View permission plus an all-readonly fieldset is what makes it
        # inspectable but not writable.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return True


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
