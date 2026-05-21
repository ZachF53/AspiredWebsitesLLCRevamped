"""Admin registrations for the Moonieful sync bridge models."""

from django.contrib import admin

from .models import SyncJob, SyncLog


@admin.register(SyncJob)
class SyncJobAdmin(admin.ModelAdmin):
    list_display = (
        'event_type', 'target', 'client', 'status', 'attempts',
        'last_attempt_at', 'sent_at', 'created_at',
    )
    list_filter = ('status', 'target', 'event_type')
    search_fields = ('client__firm_name', 'moonieful_client_id', 'last_error')
    readonly_fields = ('created_at', 'updated_at', 'last_attempt_at', 'sent_at')


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'source_site', 'status', 'created_at')
    list_filter = ('status', 'source_site', 'event_type')
    search_fields = ('event_type', 'source_site', 'error_message')
    readonly_fields = ('created_at', 'updated_at')
