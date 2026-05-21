from django.contrib import admin

from .models import (
    EmailReply,
    EmailSent,
    Lead,
    LeadNote,
    OutreachSettings,
    SuppressionList,
)


class LeadNoteInline(admin.TabularInline):
    model = LeadNote
    extra = 0
    fields = ('note', 'created_at')
    readonly_fields = ('created_at',)


class EmailSentInline(admin.TabularInline):
    model = EmailSent
    extra = 0
    fields = ('subject', 'sequence_step', 'opened', 'clicked', 'replied', 'sent_at')
    readonly_fields = ('sent_at',)
    can_delete = False
    show_change_link = True


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = (
        'firm_name', 'attorney_name', 'city', 'state',
        'score', 'temperature', 'status', 'source', 'created_at',
    )
    list_filter = (
        'status', 'temperature', 'source', 'state',
        'practice_area', 'sequence_paused', 'unsubscribed',
    )
    search_fields = (
        'firm_name', 'attorney_name', 'email', 'phone', 'city', 'tags',
    )
    list_editable = ('status',)
    readonly_fields = ('created_at', 'updated_at', 'ip_address')
    date_hierarchy = 'created_at'
    inlines = [LeadNoteInline, EmailSentInline]
    fieldsets = (
        ('Business', {
            'fields': ('firm_name', 'attorney_name', 'practice_area', 'business_type'),
        }),
        ('Contact', {
            'fields': ('email', 'phone', 'website', 'address', 'city', 'state'),
        }),
        ('Scoring', {
            'fields': ('score', 'temperature'),
        }),
        ('CRM', {
            'fields': ('status', 'source', 'tags', 'inquiry_text', 'notes'),
        }),
        ('Google presence', {
            'classes': ('collapse',),
            'fields': ('google_rating', 'google_review_count', 'has_google_business'),
        }),
        ('Website audit', {
            'classes': ('collapse',),
            'fields': (
                'website_performance_score', 'website_seo_score',
                'website_mobile_score', 'website_issues', 'audit_run_at',
            ),
        }),
        ('Outreach', {
            'classes': ('collapse',),
            'fields': (
                'last_contacted_at', 'next_followup_at',
                'sequence_step', 'sequence_paused',
                'unsubscribed', 'unsubscribed_at',
            ),
        }),
        ('Tracking', {
            'classes': ('collapse',),
            'fields': ('ip_address', 'created_at', 'updated_at'),
        }),
    )


@admin.register(LeadNote)
class LeadNoteAdmin(admin.ModelAdmin):
    list_display = ('lead', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('note', 'lead__firm_name')
    readonly_fields = ('created_at',)
    date_hierarchy = 'created_at'


@admin.register(EmailSent)
class EmailSentAdmin(admin.ModelAdmin):
    list_display = (
        'lead', 'subject', 'sequence_step',
        'opened', 'clicked', 'replied', 'sent_at',
    )
    list_filter = ('opened', 'clicked', 'replied', 'sequence_step', 'sent_at')
    search_fields = ('subject', 'body', 'lead__firm_name', 'lead__email')
    readonly_fields = ('sent_at',)
    date_hierarchy = 'sent_at'


@admin.register(EmailReply)
class EmailReplyAdmin(admin.ModelAdmin):
    list_display = (
        'lead', 'classification', 'needs_human', 'handled', 'received_at',
    )
    list_filter = ('classification', 'needs_human', 'handled', 'received_at')
    search_fields = ('subject', 'body', 'lead__firm_name', 'lead__email')
    readonly_fields = ('received_at',)
    date_hierarchy = 'received_at'


@admin.register(SuppressionList)
class SuppressionListAdmin(admin.ModelAdmin):
    list_display = ('email', 'domain', 'reason', 'added_at')
    list_filter = ('added_at', 'reason')
    search_fields = ('email', 'domain')
    readonly_fields = ('added_at',)


@admin.register(OutreachSettings)
class OutreachSettingsAdmin(admin.ModelAdmin):
    list_display = (
        'trust_level', 'daily_send_cap',
        'outreach_active', 'emails_sent_today', 'last_reset_date',
    )
    readonly_fields = ('emails_sent_today', 'last_reset_date')

    def has_add_permission(self, request):
        # Singleton — only one row allowed.
        return not OutreachSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        # Never delete the singleton.
        return False
