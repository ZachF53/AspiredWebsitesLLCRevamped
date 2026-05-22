"""Django-admin registration for the reporting models."""

from django.contrib import admin

from .models import (
    ConversionEvent,
    GBPSyncCheck,
    KeywordRankRecord,
    TrackedKeyword,
)


@admin.register(GBPSyncCheck)
class GBPSyncCheckAdmin(admin.ModelAdmin):
    list_display = (
        'client', 'field_name', 'is_mismatch', 'flagged_for_fix',
        'resolved', 'checked_at',
    )
    list_filter = ('field_name', 'is_mismatch', 'flagged_for_fix', 'resolved')
    search_fields = ('client__firm_name', 'website_value', 'gbp_value')
    readonly_fields = ('created_at', 'updated_at', 'checked_at')


@admin.register(TrackedKeyword)
class TrackedKeywordAdmin(admin.ModelAdmin):
    list_display = ('client', 'keyword', 'target_url', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('client__firm_name', 'keyword')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(KeywordRankRecord)
class KeywordRankRecordAdmin(admin.ModelAdmin):
    list_display = ('keyword', 'position', 'impressions', 'clicks', 'checked_at')
    list_filter = ('checked_at',)
    search_fields = ('keyword__keyword', 'keyword__client__firm_name')
    readonly_fields = ('created_at', 'updated_at', 'checked_at')


@admin.register(ConversionEvent)
class ConversionEventAdmin(admin.ModelAdmin):
    list_display = (
        'client', 'event_type', 'element_text', 'page_title', 'event_timestamp',
    )
    list_filter = ('event_type',)
    search_fields = ('client__firm_name', 'element_text', 'page_url')
    readonly_fields = ('created_at', 'updated_at')
