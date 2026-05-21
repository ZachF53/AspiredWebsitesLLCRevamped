"""Admin registrations for the client portal models."""

from decimal import Decimal

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html

from .contract_template import generate_contract_text
from .emails import send_contract_ready_email
from .models import (
    ClientDocument,
    ClientProfile,
    Contract,
    IntakeResponse,
    Project,
    ProjectStageLog,
    RevisionRequest,
    SupportTicket,
)


# Build-package terms: total price + timeline in weeks.
PACKAGE_TERMS = {
    'essential_build': {'price': Decimal('2500.00'), 'timeline': 3},
    'premium_build': {'price': Decimal('4500.00'), 'timeline': 4},
}


@admin.action(description='Generate contract + email signing link')
def generate_contract(modeladmin, request, queryset):
    """
    Create a Contract for each selected client and email them the signing
    link. The client's `package` must be a build package (Essential/Premium).
    """
    created = 0
    for client in queryset:
        terms = PACKAGE_TERMS.get(client.package)
        if terms is None:
            modeladmin.message_user(
                request,
                f'{client.firm_name}: set package to Essential or Premium '
                f'build before generating a contract.',
                level=messages.WARNING,
            )
            continue
        price = terms['price']
        text = generate_contract_text(
            client, client.package, price, terms['timeline'],
        )
        contract = Contract.objects.create(
            client=client,
            package=client.package,
            build_price=price,
            deposit_amount=(price / 2).quantize(Decimal('0.01')),
            timeline_weeks=terms['timeline'],
            contract_text=text,
        )
        sign_url = request.build_absolute_uri(
            reverse('clients:contract_sign', args=[contract.contract_token])
        )
        send_contract_ready_email(contract, sign_url)
        created += 1
    if created:
        modeladmin.message_user(
            request,
            f'Generated {created} contract(s) and emailed the signing link(s).',
            level=messages.SUCCESS,
        )


@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    list_display = (
        'firm_name', 'contact_name', 'status', 'package',
        'maintenance_active', 'do_droplet_ip', 'synced_from_moonieful',
        'created_at',
    )
    list_filter = (
        'status', 'package', 'maintenance_active', 'synced_from_moonieful',
        'sync_conflict_flagged', 'onboarding_complete',
    )
    search_fields = (
        'firm_name', 'contact_name', 'user__email', 'phone',
        'stripe_customer_id', 'do_droplet_id', 'do_droplet_ip',
    )
    readonly_fields = (
        'created_at', 'updated_at', 'last_synced_at',
        'do_droplet_id', 'do_droplet_ip', 'do_droplet_created_at',
        'droplet_console',
    )
    actions = [generate_contract]

    @admin.display(description='DigitalOcean console')
    def droplet_console(self, obj):
        """A 'View in DO' link for the client's provisioned Droplet."""
        if not obj.do_droplet_id:
            return '— not provisioned —'
        return format_html(
            '<a href="https://cloud.digitalocean.com/droplets/{}" '
            'target="_blank" rel="noopener">View Droplet {} in DigitalOcean</a>',
            obj.do_droplet_id, obj.do_droplet_id,
        )


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        'client', 'stage', 'package', 'payment_status',
        'revision_count', 'moonieful_referred', 'created_at',
    )
    list_filter = ('stage', 'package', 'payment_status', 'moonieful_referred')
    search_fields = ('client__firm_name', 'staging_url', 'live_url')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = (
        'client', 'package', 'build_price', 'deposit_amount',
        'signed', 'signed_at', 'created_at',
    )
    list_filter = ('signed', 'package')
    search_fields = ('client__firm_name', 'signed_name')
    readonly_fields = (
        'created_at', 'updated_at', 'contract_token', 'signed_at',
        'signed_ip', 'pdf_path',
    )


@admin.register(ProjectStageLog)
class ProjectStageLogAdmin(admin.ModelAdmin):
    list_display = (
        'project', 'from_stage', 'to_stage', 'set_by',
        'client_notified', 'created_at',
    )
    list_filter = ('to_stage', 'client_notified')
    search_fields = ('project__client__firm_name', 'note', 'set_by')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(IntakeResponse)
class IntakeResponseAdmin(admin.ModelAdmin):
    list_display = ('project', 'completed', 'completed_at', 'created_at')
    list_filter = ('completed', 'domain_registrar', 'google_business_access')
    search_fields = ('project__client__firm_name', 'domain_name')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(RevisionRequest)
class RevisionRequestAdmin(admin.ModelAdmin):
    list_display = (
        'project', 'status', 'is_major', 'counts_against_limit',
        'source', 'created_at',
    )
    list_filter = ('status', 'is_major', 'counts_against_limit', 'source')
    search_fields = ('project__client__firm_name', 'description')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ClientDocument)
class ClientDocumentAdmin(admin.ModelAdmin):
    list_display = ('label', 'client', 'project', 'direction', 'created_at')
    list_filter = ('direction',)
    search_fields = ('label', 'description', 'client__firm_name')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = (
        'subject', 'client', 'priority', 'status', 'billable', 'created_at',
    )
    list_filter = ('status', 'priority', 'billable')
    search_fields = ('subject', 'description', 'client__firm_name')
    readonly_fields = ('created_at', 'updated_at')
