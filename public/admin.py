from django.contrib import admin

from .models import AuditLead, City


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


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    """
    Content editor for the /locations/<city>/ pages — see the City
    docstring in public/models.py. Existing rows (San Antonio, Atlanta,
    Warner Robins) were seeded verbatim from the hand-written templates
    they replaced by the public.0006_seed_cities data migration.
    """
    list_display = ('name', 'slug', 'state', 'is_active', 'sort_order')
    list_filter = ('is_active', 'state')
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ('created_at', 'updated_at')
