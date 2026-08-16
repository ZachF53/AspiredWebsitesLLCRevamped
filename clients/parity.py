"""Read-only Account/Website migration parity checks.

The legacy and canonical schemas currently coexist.  This module reports the
structural gaps that must reach zero before destructive Phase-D migrations can
remove ClientProfile, Project, and their foreign keys.
"""

from dataclasses import asdict, dataclass, field

from django.apps import apps
from django.db.models import Count, F

from clients.account_models import Account, Website
from clients.account_setup import ACCOUNT_LEVEL_FIELDS, account_name_for
from clients.models import ClientProfile, Project


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    count: int
    examples: tuple[str, ...] = ()


@dataclass
class ParityReport:
    counts: dict[str, int]
    findings: list[Finding] = field(default_factory=list)

    @property
    def error_count(self):
        return sum(item.count for item in self.findings
                   if item.severity == 'error')

    @property
    def warning_count(self):
        return sum(item.count for item in self.findings
                   if item.severity == 'warning')

    @property
    def operational_count(self):
        """Findings about the business state of the data, not its structure.

        Kept separate from errors and warnings because they are not
        migration blockers — the schema is sound and nothing is lost at
        drop time — but they must not disappear either. They stay
        unresolved until a human checks the real world and confirms.
        """
        return sum(item.count for item in self.findings
                   if item.severity == 'operational')

    def as_dict(self):
        return {
            'counts': self.counts,
            'error_count': self.error_count,
            'warning_count': self.warning_count,
            'operational_count': self.operational_count,
            'findings': [asdict(item) for item in self.findings],
        }


# Legacy field name -> canonical field name. The names are identical today,
# so this is derived from the one list every writer uses (see
# clients.account_setup); a field the signal syncs but the validator does
# not check is drift nobody sees.
ACCOUNT_FIELD_MAP = {name: name for name in ACCOUNT_LEVEL_FIELDS}

WEBSITE_FIELD_MAP = {
    'firm_name': 'name',
    'business_type': 'business_type',
    'website': 'url',
    'staging_url': 'staging_url',
    'stage': 'stage',
    'package': 'package',
    'payment_status': 'payment_status',
    'deposit_paid_at': 'deposit_paid_at',
    'final_paid_at': 'final_paid_at',
    'revision_count': 'revision_count',
    'revision_limit': 'revision_limit',
    'launch_date': 'launch_date',
    'support_window_ends': 'support_window_ends',
    'do_droplet_id': 'do_droplet_id',
    'do_droplet_ip': 'do_droplet_ip',
}


def _examples(queryset, limit):
    return tuple(str(value) for value in queryset.values_list(
        'pk', flat=True)[:limit])


def _field_for(model, names, related_model):
    for name in names:
        try:
            candidate = model._meta.get_field(name)
        except Exception:
            continue
        if getattr(candidate, 'related_model', None) is related_model:
            return candidate
    return None


def _add(report, severity, code, queryset, detail_limit):
    count = queryset.count()
    if count:
        report.findings.append(Finding(
            severity=severity,
            code=code,
            count=count,
            examples=_examples(queryset, detail_limit),
        ))


def _is_unset(value):
    """True when a field holds no value at all.

    None and '' only. ``False`` and ``0`` are values somebody chose —
    treating them as empty is how a stale legacy row silently resets
    ``revision_count`` or flips ``session_recording_enabled``.
    """
    return value is None or value == ''


def _classify(legacy_value, canonical_value):
    """How a legacy/canonical disagreement matters to the cutover.

    The validator's question is not "do these two rows agree?" — during the
    transition both are written, so they often will not. The question is
    "what breaks when the legacy row is dropped?"

    ``gap``       canonical is empty and legacy holds a value. The value
                  disappears at drop time unless it is backfilled. A real
                  problem, and one the backfill can fix on its own.
    ``conflict``  both hold different real values. Only one can survive and
                  neither store is universally authoritative during the
                  transition, so an operator has to say which. Reported for
                  a decision, never auto-resolved.
    ``stale``     legacy is empty and canonical holds a value. Expected —
                  the canonical row moved on and the legacy column was
                  never written. Nothing is at risk; not a finding.
    """
    if legacy_value == canonical_value:
        return None
    if _is_unset(canonical_value) and not _is_unset(legacy_value):
        return 'gap'
    if _is_unset(legacy_value):
        return 'stale'
    return 'conflict'


def _split_fields(legacy, canonical, field_map):
    """Return (gaps, conflicts) descriptions for one legacy/canonical pair."""
    gaps = []
    conflicts = []
    for legacy_name, canonical_name in field_map.items():
        legacy_value = getattr(legacy, legacy_name)
        canonical_value = getattr(canonical, canonical_name)
        kind = _classify(legacy_value, canonical_value)
        if kind == 'gap':
            gaps.append(canonical_name)
        elif kind == 'conflict':
            conflicts.append(
                f'{canonical_name}: canonical={canonical_value!r} '
                f'legacy={legacy_value!r}')
    return gaps, conflicts


def _add_drift_findings(report, detail_limit):
    account_gaps = []
    account_conflicts = []
    website_gaps = []
    website_conflicts = []
    multi_website = []

    linked_accounts = Account.objects.filter(
        legacy_client_profile__isnull=False,
    ).select_related('legacy_client_profile').prefetch_related('websites')

    for account in linked_accounts:
        legacy = account.legacy_client_profile
        gaps, conflicts = _split_fields(
            legacy, account, ACCOUNT_FIELD_MAP)
        expected_name = account_name_for(legacy)
        if account.name != expected_name:
            (gaps if _is_unset(account.name) else conflicts).append(
                f'name: {account.name!r} vs {expected_name!r}')
        if gaps:
            account_gaps.append(f'{account.pk} ({", ".join(gaps)})')
        if conflicts:
            account_conflicts.append(
                f'{account.pk} ({"; ".join(conflicts)})')

        websites = list(account.websites.all())
        if len(websites) > 1:
            # An account with several websites cannot have its legacy
            # client-level rows allocated automatically, so it needs a
            # recorded manual mapping (see repair_account_website_parity).
            # A website added after that review invalidates it — the mapping
            # never considered the new site.
            reviewed = account.multi_website_reviewed_at
            newest = max(site.created_at for site in websites)
            if reviewed is None:
                multi_website.append(
                    f'{account.pk} ({len(websites)} websites, '
                    'no mapping review)')
            elif newest > reviewed:
                multi_website.append(
                    f'{account.pk} (website added {newest:%Y-%m-%d}, after '
                    f'the {reviewed:%Y-%m-%d} mapping review)')
            # Per-website field drift is meaningless here: the legacy row
            # describes one build, not several.
            continue
        if len(websites) != 1:
            continue

        website = websites[0]
        gaps, conflicts = _split_fields(legacy, website, WEBSITE_FIELD_MAP)
        if gaps:
            website_gaps.append(f'{website.pk} ({", ".join(gaps)})')
        if conflicts:
            website_conflicts.append(
                f'{website.pk} ({"; ".join(conflicts)})')

    for code, values in (
        ('account-field-gap', account_gaps),
        ('account-field-conflict', account_conflicts),
        ('website-field-gap', website_gaps),
        ('website-field-conflict', website_conflicts),
        ('multi-website-manual-review', multi_website),
    ):
        if values:
            report.findings.append(Finding(
                severity='warning',
                code=code,
                count=len(values),
                examples=tuple(values[:detail_limit]),
            ))


def _add_dependent_fk_findings(report, detail_limit):
    """Find current rows that still have only their legacy owner FK."""
    for model in apps.get_models():
        account_field = _field_for(
            model, ('account_new', 'account'), Account)
        website_field = _field_for(
            model, ('website_new', 'website'), Website)
        client_field = _field_for(model, ('client',), ClientProfile)
        project_field = _field_for(model, ('project',), Project)
        vault_field = _field_for(
            model, ('vault',), apps.get_model('vault', 'ClientVault'))

        label = model._meta.label

        if client_field is not None:
            legacy_rows = model.objects.filter(client__isnull=False)
            if account_field is not None:
                _add(
                    report, 'error', f'{label}.missing-canonical-account',
                    legacy_rows.filter(**{
                        f'{account_field.name}__isnull': True,
                    }), detail_limit,
                )
            if website_field is not None:
                website_rows = legacy_rows.filter(
                    client__migrated_account__websites__isnull=False,
                    **{f'{website_field.name}__isnull': True},
                ).distinct()
                _add(
                    report, 'error', f'{label}.missing-canonical-website',
                    website_rows, detail_limit,
                )

        if project_field is not None and website_field is not None:
            _add(
                report, 'error', f'{label}.project-missing-website',
                model.objects.filter(
                    project__isnull=False,
                    **{f'{website_field.name}__isnull': True},
                ), detail_limit,
            )

        # VaultCredential reaches ClientProfile through ClientVault rather
        # than a direct `client` FK.  The Phase-D backfill handles this same
        # shape, so the validator must cover it too.
        if vault_field is not None:
            legacy_rows = model.objects.filter(vault__client__isnull=False)
            if account_field is not None:
                _add(
                    report, 'error', f'{label}.missing-canonical-account',
                    legacy_rows.filter(**{
                        f'{account_field.name}__isnull': True,
                    }), detail_limit,
                )
            if website_field is not None:
                _add(
                    report, 'error', f'{label}.missing-canonical-website',
                    legacy_rows.filter(
                        vault__client__migrated_account__websites__isnull=False,
                        **{f'{website_field.name}__isnull': True},
                    ).distinct(), detail_limit,
                )


def _add_duplicate_identifier_findings(report, detail_limit):
    checks = (
        (Account, 'stripe_customer_id'),
        (Website, 'stripe_hosting_subscription_id'),
        (Website, 'stripe_maintenance_subscription_id'),
        (apps.get_model('clients', 'MaintenancePlan'),
         'stripe_subscription_id'),
        (apps.get_model('clients', 'SocialMediaPlan'),
         'stripe_subscription_id'),
        (apps.get_model('clients', 'Droplet'), 'do_droplet_id'),
    )
    for model, field_name in checks:
        duplicates = list(
            model.objects.exclude(**{field_name: ''})
            .values(field_name)
            .annotate(row_count=Count('pk'))
            .filter(row_count__gt=1)
            .order_by(field_name)
        )
        if duplicates:
            report.findings.append(Finding(
                severity='error',
                code=f'{model._meta.label}.{field_name}-duplicate',
                count=len(duplicates),
                examples=tuple(
                    f'{row[field_name]} ({row["row_count"]} rows)'
                    for row in duplicates[:detail_limit]
                ),
            ))


def _add_operational_findings(report, detail_limit):
    """Business-state problems the migration surfaces but does not cause.

    ``fully_paid`` is the flag that releases the launch gate. A site
    carrying it with nothing in the ledger behind it is claiming money the
    system has no record of receiving — possibly true (paid by cheque and
    marked by hand), possibly a mis-set field. Either way it must be
    checked by a person before that site launches, so it is reported until
    somebody does.
    """
    from clients.payment_evidence import is_fully_paid_without_evidence

    unverified = [
        f'{website.pk} ({website.name}, stage={website.stage})'
        for website in Website.objects.filter(
            payment_status='fully_paid').select_related('account')
        if is_fully_paid_without_evidence(website)
    ]
    if unverified:
        report.findings.append(Finding(
            severity='operational',
            code='website-fully-paid-without-ledger-evidence',
            count=len(unverified),
            examples=tuple(unverified[:detail_limit]),
        ))


def audit_account_website_parity(detail_limit=20):
    """Return a read-only structural and identifier parity report."""
    report = ParityReport(counts={
        'legacy_client_profiles': ClientProfile.objects.count(),
        'legacy_projects': Project.objects.count(),
        'accounts': Account.objects.count(),
        'websites': Website.objects.count(),
    })

    _add(
        report, 'error', 'client-profile-missing-account',
        ClientProfile.objects.filter(migrated_account__isnull=True),
        detail_limit,
    )

    accounts_with_wrong_user = Account.objects.filter(
        legacy_client_profile__isnull=False,
    ).exclude(user_id=F('legacy_client_profile__user_id'))
    _add(
        report, 'error', 'account-user-mismatch',
        accounts_with_wrong_user, detail_limit,
    )

    _add(
        report, 'error', 'legacy-project-missing-website',
        Project.objects.filter(migrated_website__isnull=True),
        detail_limit,
    )

    mismatched_websites = []
    linked_websites = Website.objects.filter(
        legacy_project__isnull=False,
    ).select_related(
        'account', 'legacy_project__client__migrated_account')
    for website in linked_websites:
        try:
            expected_account = website.legacy_project.client.migrated_account
        except Exception:
            continue
        if website.account_id != expected_account.pk:
            mismatched_websites.append(str(website.pk))
    if mismatched_websites:
        report.findings.append(Finding(
            severity='error',
            code='website-legacy-project-account-mismatch',
            count=len(mismatched_websites),
            examples=tuple(mismatched_websites[:detail_limit]),
        ))

    _add_dependent_fk_findings(report, detail_limit)
    _add_duplicate_identifier_findings(report, detail_limit)
    _add_drift_findings(report, detail_limit)
    _add_operational_findings(report, detail_limit)
    return report
