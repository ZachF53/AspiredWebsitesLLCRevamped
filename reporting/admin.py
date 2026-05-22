"""Django-admin registration for the reporting models."""

from django.contrib import admin

from .models import (
    BlogPost,
    ChatbotConversation,
    ClientChatbot,
    ContentFreshnessReport,
    ConversionEvent,
    GBPSyncCheck,
    KeywordRankRecord,
    MonthlyReport,
    NPSSurvey,
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


@admin.register(MonthlyReport)
class MonthlyReportAdmin(admin.ModelAdmin):
    list_display = (
        'client', 'report_month', 'status', 'uptime_30d',
        'form_submissions', 'sent_at',
    )
    list_filter = ('status',)
    search_fields = ('client__firm_name',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ContentFreshnessReport)
class ContentFreshnessReportAdmin(admin.ModelAdmin):
    list_display = (
        'client', 'generated_at', 'pages_analyzed', 'pages_needing_update',
    )
    search_fields = ('client__firm_name',)
    readonly_fields = ('created_at', 'updated_at', 'generated_at')


@admin.register(NPSSurvey)
class NPSSurveyAdmin(admin.ModelAdmin):
    list_display = (
        'client', 'score', 'response_action_taken', 'sent_at', 'responded_at',
    )
    list_filter = ('response_action_taken',)
    search_fields = ('client__firm_name', 'feedback')
    readonly_fields = ('created_at', 'updated_at', 'sent_at', 'survey_token')


@admin.register(BlogPost)
class BlogPostAdmin(admin.ModelAdmin):
    list_display = ('client', 'topic', 'status', 'word_count', 'created_at')
    list_filter = ('status', 'generated_by_ai')
    search_fields = ('client__firm_name', 'topic', 'title')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ClientChatbot)
class ClientChatbotAdmin(admin.ModelAdmin):
    list_display = (
        'client', 'is_active', 'total_conversations', 'leads_captured',
    )
    list_filter = ('is_active',)
    search_fields = ('client__firm_name',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ChatbotConversation)
class ChatbotConversationAdmin(admin.ModelAdmin):
    list_display = (
        'chatbot', 'session_id', 'lead_captured', 'started_at',
    )
    list_filter = ('lead_captured',)
    search_fields = ('chatbot__client__firm_name', 'visitor_email')
    readonly_fields = ('created_at', 'updated_at', 'started_at', 'last_message_at')
