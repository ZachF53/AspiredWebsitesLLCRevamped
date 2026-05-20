from django.contrib import admin

from .models import AuditLead


@admin.register(AuditLead)
class AuditLeadAdmin(admin.ModelAdmin):
    list_display = ('url', 'email', 'performance_score', 'seo_score',
                    'best_practices_score', 'accessibility_score', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('url', 'email')
    readonly_fields = ('url', 'performance_score', 'seo_score',
                       'best_practices_score', 'accessibility_score',
                       'issues', 'email', 'ip_address', 'created_at')
    date_hierarchy = 'created_at'
