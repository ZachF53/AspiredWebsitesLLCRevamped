"""
Django-admin registration for the vault.

Only the audit log and vault containers are registered. Credentials are
deliberately NOT exposed here — they live behind the PIN-gated vault UI.
"""

from django.contrib import admin

from .models import ClientVault, VaultAccessLog


@admin.register(VaultAccessLog)
class VaultAccessLogAdmin(admin.ModelAdmin):
    list_display = ('action', 'client_name', 'credential_label',
                    'ip_address', 'created_at')
    list_filter = ('action',)
    search_fields = ('client_name', 'credential_label', 'note')
    readonly_fields = ('action', 'client_name', 'credential_label',
                       'ip_address', 'note', 'created_at', 'updated_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ClientVault)
class ClientVaultAdmin(admin.ModelAdmin):
    list_display = ('client', 'created_at')
    search_fields = ('client__firm_name',)
    readonly_fields = ('created_at', 'updated_at')
