"""
Django-admin registration for the vault.

Only the audit log and vault containers are registered. Credentials are
deliberately NOT exposed here — they live behind the PIN-gated vault UI.
"""

from django.contrib import admin

from .models import (
    ClientVault,
    ServerCommandLibrary,
    SSHSessionLog,
    VaultAccessLog,
)


@admin.register(ServerCommandLibrary)
class ServerCommandLibraryAdmin(admin.ModelAdmin):
    list_display = (
        'credential', 'label', 'category',
        'requires_confirmation', 'is_dangerous', 'sort_order',
    )
    list_filter = ('category', 'is_dangerous', 'requires_confirmation')
    search_fields = ('label', 'command', 'credential__label')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(SSHSessionLog)
class SSHSessionLogAdmin(admin.ModelAdmin):
    list_display = (
        'credential', 'client', 'started_at', 'ended_at',
        'duration_seconds', 'totp_verified',
    )
    list_filter = ('totp_verified',)
    search_fields = ('credential__label', 'client__firm_name')
    readonly_fields = (
        'credential', 'client', 'started_at', 'ended_at', 'duration_seconds',
        'totp_verified', 'ip_address', 'commands_executed',
        'created_at', 'updated_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


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
