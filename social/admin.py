"""Phase 5a — Django admin registrations for social models."""

from django.contrib import admin

from .models import PostResult, ScheduledPost, SocialToken


@admin.register(SocialToken)
class SocialTokenAdmin(admin.ModelAdmin):
    list_display = (
        'channel', 'provider_account_id', 'expires_at',
        'last_refresh_at', 'created_at',
    )
    list_filter = ('scopes',)
    search_fields = (
        'channel__handle', 'provider_account_id',
        'connected_by__username',
    )
    # Ciphertext fields are read-only — never let the admin paste plaintext.
    readonly_fields = (
        'created_at', 'updated_at',
        'access_token_encrypted', 'refresh_token_encrypted',
    )


@admin.register(ScheduledPost)
class ScheduledPostAdmin(admin.ModelAdmin):
    list_display = (
        'channel', 'status', 'scheduled_for', 'published_at',
        'ai_generated', 'created_at',
    )
    list_filter = ('status', 'ai_generated')
    search_fields = (
        'channel__handle', 'account_new__name', 'body',
    )
    readonly_fields = ('created_at', 'updated_at')


@admin.register(PostResult)
class PostResultAdmin(admin.ModelAdmin):
    list_display = (
        'scheduled_post', 'success', 'attempted_at',
        'provider_post_id', 'likes', 'comments', 'reach',
    )
    list_filter = ('success',)
    search_fields = (
        'scheduled_post__channel__handle',
        'provider_post_id', 'permalink', 'error_detail',
    )
    readonly_fields = ('created_at', 'updated_at')
