"""Admin registrations for admin_dashboard models."""

from django.contrib import admin

from .models import AIAssistantLog, DeploymentLog


@admin.register(DeploymentLog)
class DeploymentLogAdmin(admin.ModelAdmin):
    list_display = (
        'deploy_type', 'domain', 'server_ip', 'success', 'deployed_by',
        'created_at',
    )
    list_filter = ('deploy_type', 'success')
    search_fields = ('domain', 'server_ip', 'notes', 'website_new__name', 'website_new__account__name')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(AIAssistantLog)
class AIAssistantLogAdmin(admin.ModelAdmin):
    list_display = (
        'created_at', 'operator', 'website_new', 'intent', 'success',
    )
    list_filter = ('success', 'intent')
    search_fields = ('intent', 'raw_command', 'result_message',
                     'website_new__name', 'website_new__account__name', 'operator__username')
    readonly_fields = (
        'created_at', 'updated_at', 'operator', 'client',
        'raw_command', 'intent', 'args', 'success', 'result_message',
    )

    def has_add_permission(self, request):
        # Append-only audit log — rows written by the view, never UI.
        return False

    def has_change_permission(self, request, obj=None):
        return False
