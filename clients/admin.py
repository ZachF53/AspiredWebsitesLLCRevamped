"""
Admin registrations for the client portal models.

Phase-D cutover. Two things changed together here, and they are the same
change:

**Account and Website are registered; ClientProfile and Project are not.**
The canonical models had no admin at all, so the only way to inspect a
client through /admin/ was the legacy table that is being dropped. That is
backwards -- and it made the legacy rows look like the live ones, which is
exactly the confusion the cutover is trying to end.

**Every list, filter and search traverses the canonical FK.** These were
written as ``client__firm_name``. That column disappears with the legacy
table, so each one was a latent ``FieldError`` at drop time -- and worse,
a silent one: ``search_fields`` is not validated until someone types in
the box, so the admin would have looked fine right up until it was used.
"""

from decimal import Decimal

from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html

from .account_models import Account, Website, WebsiteStageLog
from .contract_template import generate_contract_text
from .emails import send_contract_ready_email
from .models import (
    ClientDocument,
    Contract,
    ContractService,
    IntakeResponse,
    PaymentRecord,
    ProjectStageLog,
    RevisionRequest,
    SiteChangelogEntry,
    SupportTicket,
    UptimeAlert,
    UptimeRecord,
)


# Maps a Website.package value to its billing ServiceTier slug.
BUILD_PACKAGE_TO_SLUG = {
    'essential_build': 'website-essential',
    'premium_build': 'website-premium',
}

# Reused search paths, so a rename shows up in one place rather than twelve.
WEBSITE_SEARCH = ('website_new__name', 'website_new__account__name')


@admin.action(description='Generate contract + email signing link')
def generate_contract(modeladmin, request, queryset):
    """
    Create a Contract for each selected website and email the client the
    signing link. The website's `package` must be a build package
    (Essential/Premium) and the matching billing ServiceTier must be seeded.

    Run against websites rather than accounts: a contract covers one build,
    and an account with two sites needs two contracts. Selecting the account
    could only ever produce one, silently against whichever package the
    account happened to carry.
    """
    from billing.pricing_models import ServiceTier

    created = 0
    for site in queryset.select_related('account'):
        slug = BUILD_PACKAGE_TO_SLUG.get(site.package)
        tier = ServiceTier.objects.filter(slug=slug).first() if slug else None
        if tier is None:
            modeladmin.message_user(
                request,
                f'{site.name}: set package to Essential or Premium '
                f'build, and run seed_pricing, before generating a contract.',
                level=messages.WARNING,
            )
            continue
        text = generate_contract_text(_ContractParty(site), tier.slug)
        contract = Contract.objects.create(
            account=site.account,
            website_new=site,
            package=site.package,
            build_price=tier.price,
            deposit_amount=(tier.price / 2).quantize(Decimal('0.01')),
            timeline_weeks=tier.timeline_weeks or 0,
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


class _ContractParty:
    """Adapts a Website to the two names the contract text renders.

    ``generate_contract_text`` reads ``firm_name`` and ``contact_name``.
    Those are account-level facts -- the organisation that signs and the
    person who signs for it -- so they come from the Account, not from the
    site name. A contract for "Vance Mediation Services" is still signed by
    Vance Family Law the firm.
    """

    __slots__ = ('firm_name', 'contact_name')

    def __init__(self, website):
        account = website.account
        self.firm_name = (account.name if account else website.name) or ''
        self.contact_name = (account.contact_name if account else '') or ''


# ── Canonical models ──────────────────────────────────────────────────────

class WebsiteInline(admin.TabularInline):
    model = Website
    extra = 0
    fields = ('name', 'slug', 'stage', 'status', 'package',
              'maintenance_active', 'url')
    readonly_fields = ('created_at', 'updated_at')
    show_change_link = True


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'contact_name', 'status', 'website_count',
        'stripe_customer_id', 'synced_from_moonieful', 'created_at',
    )
    list_filter = (
        'status', 'is_tester', 'synced_from_moonieful',
        'sync_conflict_flagged', 'onboarding_status',
    )
    search_fields = (
        'name', 'contact_name', 'user__email', 'phone', 'stripe_customer_id',
    )
    readonly_fields = (
        'created_at', 'updated_at', 'last_synced_at', 'legacy_client_profile',
    )
    inlines = [WebsiteInline]

    @admin.display(description='Websites')
    def website_count(self, obj):
        return obj.websites.count()


@admin.register(Website)
class WebsiteAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'account', 'stage', 'status', 'package',
        'maintenance_active', 'do_droplet_ip', 'launch_date',
    )
    list_filter = (
        'stage', 'status', 'package', 'maintenance_active',
        'payment_status', 'moonieful_referred',
    )
    search_fields = (
        'name', 'slug', 'account__name', 'url', 'staging_url',
        'do_droplet_id', 'do_droplet_ip',
    )
    readonly_fields = (
        'created_at', 'updated_at', 'legacy_project',
        'do_droplet_id', 'do_droplet_ip', 'do_droplet_created_at',
        'droplet_console',
    )
    actions = [generate_contract]
    list_select_related = ('account',)

    @admin.display(description='DigitalOcean console')
    def droplet_console(self, obj):
        """A 'View in DO' link for this site's provisioned Droplet."""
        if not obj.do_droplet_id:
            return '— not provisioned —'
        return format_html(
            '<a href="https://cloud.digitalocean.com/droplets/{}" '
            'target="_blank" rel="noopener">View Droplet {} in DigitalOcean</a>',
            obj.do_droplet_id, obj.do_droplet_id,
        )


@admin.register(WebsiteStageLog)
class WebsiteStageLogAdmin(admin.ModelAdmin):
    list_display = (
        'website', 'from_stage', 'to_stage', 'set_by',
        'client_notified', 'created_at',
    )
    list_filter = ('to_stage', 'client_notified')
    search_fields = ('website__name', 'website__account__name', 'note',
                     'set_by')
    readonly_fields = ('created_at', 'updated_at')


# ── Portal models ─────────────────────────────────────────────────────────

class ContractServiceInline(admin.TabularInline):
    model = ContractService
    extra = 0
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = (
        'account', 'website_new', 'service_summary', 'package', 'build_price',
        'signed', 'signed_at', 'created_at',
    )
    list_filter = ('signed', 'package')
    search_fields = ('account__name', 'website_new__name', 'signed_name')
    readonly_fields = (
        'created_at', 'updated_at', 'contract_token', 'signed_at',
        'signed_ip', 'signed_user_agent', 'signed_content_hash', 'pdf_path',
    )
    inlines = [ContractServiceInline]
    list_select_related = ('account', 'website_new')


@admin.register(PaymentRecord)
class PaymentRecordAdmin(admin.ModelAdmin):
    list_display = (
        'account', 'website', 'kind', 'amount', 'status', 'paid_at',
        'stripe_id',
    )
    list_filter = ('kind', 'status')
    search_fields = ('account__name', 'website__name', 'stripe_id',
                     'description')
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = ('account', 'website')


@admin.register(ProjectStageLog)
class ProjectStageLogAdmin(admin.ModelAdmin):
    list_display = (
        'website_new', 'from_stage', 'to_stage', 'set_by',
        'client_notified', 'created_at',
    )
    list_filter = ('to_stage', 'client_notified')
    search_fields = WEBSITE_SEARCH + ('note', 'set_by')
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = ('website_new',)


@admin.register(IntakeResponse)
class IntakeResponseAdmin(admin.ModelAdmin):
    list_display = ('website_new', 'completed', 'completed_at', 'created_at')
    list_filter = ('completed', 'domain_registrar', 'google_business_access')
    search_fields = WEBSITE_SEARCH + ('domain_name',)
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = ('website_new',)


@admin.register(RevisionRequest)
class RevisionRequestAdmin(admin.ModelAdmin):
    list_display = (
        'website_new', 'status', 'is_major', 'counts_against_limit',
        'source', 'created_at',
    )
    list_filter = ('status', 'is_major', 'counts_against_limit', 'source')
    search_fields = WEBSITE_SEARCH + ('description',)
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = ('website_new',)


@admin.register(ClientDocument)
class ClientDocumentAdmin(admin.ModelAdmin):
    list_display = ('label', 'website_new', 'direction', 'created_at')
    list_filter = ('direction',)
    search_fields = WEBSITE_SEARCH + ('label', 'description')
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = ('website_new',)


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = (
        'subject', 'account_new', 'website_new', 'priority', 'status',
        'billable', 'created_at',
    )
    list_filter = ('status', 'priority', 'billable')
    search_fields = ('subject', 'description', 'account_new__name',
                     'website_new__name')
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = ('account_new', 'website_new')


@admin.register(SiteChangelogEntry)
class SiteChangelogEntryAdmin(admin.ModelAdmin):
    list_display = (
        'website_new', 'change_type', 'title', 'date_of_change',
        'is_client_visible',
    )
    list_filter = ('change_type', 'is_client_visible', 'date_of_change')
    search_fields = WEBSITE_SEARCH + ('title', 'description')
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = ('website_new',)


@admin.register(UptimeRecord)
class UptimeRecordAdmin(admin.ModelAdmin):
    list_display = (
        'website_new', 'is_up', 'status_code', 'response_time_ms',
        'checked_at',
    )
    list_filter = ('is_up',)
    search_fields = WEBSITE_SEARCH + ('error_message',)
    readonly_fields = ('created_at', 'updated_at', 'checked_at')
    list_select_related = ('website_new',)


@admin.register(UptimeAlert)
class UptimeAlertAdmin(admin.ModelAdmin):
    list_display = (
        'website_new', 'is_resolved', 'alerted_at', 'resolved_at',
        'alert_sent',
    )
    list_filter = ('is_resolved', 'alert_sent')
    search_fields = WEBSITE_SEARCH
    readonly_fields = ('created_at', 'updated_at', 'alerted_at')
    list_select_related = ('website_new',)
