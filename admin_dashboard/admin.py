"""Admin registrations for admin_dashboard models."""

from django.contrib import admin

from .models import DeploymentLog


@admin.register(DeploymentLog)
class DeploymentLogAdmin(admin.ModelAdmin):
    list_display = (
        'deploy_type', 'domain', 'server_ip', 'success', 'deployed_by',
        'created_at',
    )
    list_filter = ('deploy_type', 'success')
    search_fields = ('domain', 'server_ip', 'notes', 'client__firm_name')
    readonly_fields = ('created_at', 'updated_at')
