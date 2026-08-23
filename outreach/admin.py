from django.contrib import admin
from django.utils import timezone

from .models import (
    EmailReply,
    EmailSent,
    InstantlyEvent,
    Lead,
    LeadReviewQueue,
    Offer,
    LeadNote,
    OutreachCampaign,
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


@admin.register(OutreachCampaign)
class OutreachCampaignAdmin(admin.ModelAdmin):
    """One niche x geography segment, mapped to one Instantly campaign.

    ``instantly_campaign_id`` is the field that matters: blank means
    nothing can be pushed, no matter what ``active`` says. Both are shown
    in the list for exactly that reason.
    """

    list_display = (
        'name', 'niche', 'city', 'state', 'active',
        'instantly_campaign_id', 'leads_pushed', 'last_push_at',
    )
    list_filter = ('active', 'state')
    search_fields = ('name', 'niche', 'city', 'instantly_campaign_id')
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = (
        'leads_pushed', 'last_push_at', 'last_push_error',
        'created_at', 'updated_at',
    )
    fieldsets = (
        ('Segment', {
            'fields': ('name', 'slug', 'niche', 'business_type',
                       'city', 'state'),
        }),
        ('Instantly', {
            'fields': ('instantly_campaign_id', 'active'),
            'description': (
                'Create the campaign in Instantly first, then paste its '
                'id here. Leads are only pushed when BOTH an id is set '
                'and active is ticked.'),
        }),
        ('History', {
            'fields': ('leads_pushed', 'last_push_at', 'last_push_error',
                       'created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )


@admin.register(InstantlyEvent)
class InstantlyEventAdmin(admin.ModelAdmin):
    """Raw webhook events. Read-only on purpose.

    These rows are the audit trail for what the reply filter accepted
    and rejected. Editing them would destroy the only evidence of a
    misclassification, which is precisely what was missing when ten
    Google Ads notifications became ten prospect replies.
    """

    list_display = (
        'received_at', 'event_type', 'lead_email', 'lead',
        'campaign', 'processed', 'short_error',
    )
    list_filter = ('event_type', 'processed', 'received_at')
    search_fields = ('lead_email', 'raw_event_type', 'dedupe_key')
    readonly_fields = (
        'event_type', 'raw_event_type', 'lead', 'lead_email', 'campaign',
        'payload', 'dedupe_key', 'processed', 'processed_at', 'error',
        'received_at',
    )
    date_hierarchy = 'received_at'

    def short_error(self, obj):
        return (obj.error or '')[:60]
    short_error.short_description = 'Filter / error note'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ── Manual review queue ────────────────────────────────────────────────

@admin.register(LeadReviewQueue)
class LeadReviewQueueAdmin(admin.ModelAdmin):
    """Leads whose company NAME contradicts the industry the source gave.

    Apollo tags "Bwa Video, Inc." and "Kinney Recruiting" as Legal
    Services, and no actor-side filter can exclude a recruiting company
    the source calls a law practice -- a tightened run returned 100
    identical rows and excluded none of them.

    Held, not dropped. push_leads refuses a flagged lead, so clearing one
    here is what releases it to the next push.
    """

    list_display = ('firm_name', 'business_type', 'city', 'state',
                    'email', 'review_reason')
    list_filter = ('business_type', 'state', 'source')
    search_fields = ('firm_name', 'email', 'review_reason')
    actions = ('approve_leads', 'reject_leads')
    readonly_fields = ('review_reason',)

    def get_queryset(self, request):
        return super().get_queryset(request).filter(needs_review=True)

    @admin.action(description='Approve - genuine, release to campaigns')
    def approve_leads(self, request, queryset):
        n = queryset.update(
            needs_review=False,
            reviewed_at=timezone.now(),
            reviewed_by=request.user,
        )
        self.message_user(request, f'{n} lead(s) approved and released.')

    @admin.action(description='Reject - not our target, archive')
    def reject_leads(self, request, queryset):
        n = queryset.update(
            needs_review=False,
            status='archived',
            reviewed_at=timezone.now(),
            reviewed_by=request.user,
        )
        self.message_user(
            request,
            f'{n} lead(s) archived. They stay out of every campaign.')


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    """Offers live here, not in code, so changing one needs no deploy.

    ``active`` defaults False on purpose. An agent-proposed offer is a
    proposal awaiting a human, never something that starts going out on
    its own -- the same guardrail EmailTemplateVariant has carried since
    it was written.
    """

    list_display = ('name', 'key', 'active', 'appeals_to', 'proposed_by',
                    'sends', 'replies', 'reply_rate_display')
    list_filter = ('active', 'proposed_by')
    search_fields = ('key', 'name', 'pitch', 'appeals_to')
    readonly_fields = ('sends', 'replies', 'positive_replies', 'bookings',
                       'created_at', 'updated_at')
    fieldsets = (
        ('Identity', {'fields': ('key', 'name', 'appeals_to', 'active',
                                 'proposed_by')}),
        ('Copy', {
            'fields': ('pitch', 'restate', 'ask'),
            'description': (
                'pitch goes in touch 1. restate goes in touch 2 as '
                '"...what I am offering: &lt;restate&gt;." so it should read '
                'as a clause, not a sentence. ask is the call to action '
                'and appears in touches 1, 2 and 3.'),
        }),
        ('What it costs you', {
            'fields': ('fulfilment_cost',),
            'description': (
                'Be honest here. An offer with a great reply rate that '
                'takes four hours to honour is a trap - succeed and you '
                'have sold yourself into unpaid full-time work.'),
        }),
        ('Performance', {
            'fields': ('sends', 'replies', 'positive_replies', 'bookings',
                       'created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    def reply_rate_display(self, obj):
        return f'{obj.reply_rate * 100:.1f}%' if obj.sends else '-'
    reply_rate_display.short_description = 'Reply rate'
