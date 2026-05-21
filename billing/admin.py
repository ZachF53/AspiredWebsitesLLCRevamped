"""Admin registrations for billing models."""

from django.contrib import admin

from .models import MiniInvoice


@admin.register(MiniInvoice)
class MiniInvoiceAdmin(admin.ModelAdmin):
    list_display = (
        'client', 'description', 'amount', 'hours', 'status', 'created_at',
    )
    list_filter = ('status',)
    search_fields = ('client__firm_name', 'description', 'stripe_invoice_id')
    readonly_fields = ('created_at', 'updated_at')
