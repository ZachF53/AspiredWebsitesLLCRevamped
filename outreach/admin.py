from django.contrib import admin

from .models import Lead


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ('business_name', 'name', 'business_type', 'email', 'phone', 'status', 'created_at')
    list_filter = ('status', 'business_type', 'source', 'created_at')
    search_fields = ('business_name', 'name', 'email', 'phone', 'message')
    list_editable = ('status',)
    readonly_fields = ('created_at', 'updated_at', 'ip_address')
    fieldsets = (
        ('Contact', {
            'fields': ('name', 'business_name', 'business_type', 'email', 'phone'),
        }),
        ('Inquiry', {
            'fields': ('source', 'message'),
        }),
        ('Tracking', {
            'fields': ('status', 'ip_address', 'created_at', 'updated_at'),
        }),
    )
    date_hierarchy = 'created_at'
