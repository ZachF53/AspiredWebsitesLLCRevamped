"""
Copy account-level data from each legacy ClientProfile onto its Account.

Part of the Phase-D migration: Account becomes the source of truth for
account-level data (contact, billing, PIN, notification prefs, Moonieful
sync state). The create-signal only copied a subset at creation time and
never on later edits, so this pulls the *current* CP values across.

ClientProfile is the current live store, so CP wins — its values
overwrite the Account's account-level fields. The CP table is retained
(not dropped), so this is safe + re-runnable; re-run any time you spot
something that didn't come across.

Idempotent + dry-run by default. Run with --apply to write.
"""

from django.core.management.base import BaseCommand


# Account-level fields that live on BOTH ClientProfile and Account.
# (Per-build fields — stage, payment, revisions, etc. — are NOT here;
# those belong to Website and were migrated separately.)
ACCOUNT_FIELDS = [
    'contact_name', 'phone', 'address', 'city', 'state', 'zip_code',
    'status', 'is_tester', 'internal_notes', 'stripe_customer_id',
    'preferred_contact_method', 'notify_on_stage_change',
    'onboarding_complete',
    'client_pin_hash', 'client_pin_salt', 'client_pin_set',
    'client_pin_failed_attempts', 'client_pin_lockout_until',
    'moonieful_client_id', 'synced_from_moonieful', 'last_synced_at',
    'sync_conflict_flagged',
    'comp_build_package', 'comp_maintenance_package', 'comp_social_tier',
    'comp_notes',
]


class Command(BaseCommand):
    help = "Copy account-level data from ClientProfile onto its Account."

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write changes (default: dry-run, no writes).')

    def handle(self, *args, **opts):
        apply = opts['apply']
        from clients.account_models import Account

        accounts = Account.objects.filter(
            legacy_client_profile__isnull=False
        ).select_related('legacy_client_profile')

        self.stdout.write(
            f'Accounts linked to a ClientProfile: {accounts.count()}')
        self.stdout.write('DRY RUN - no writes\n' if not apply
                          else 'APPLYING changes\n')

        total_changes = 0
        for acc in accounts:
            cp = acc.legacy_client_profile
            changed = []
            for f in ACCOUNT_FIELDS:
                if not hasattr(cp, f) or not hasattr(acc, f):
                    continue
                cp_val = getattr(cp, f)
                if getattr(acc, f) != cp_val:
                    setattr(acc, f, cp_val)
                    changed.append(f)
            # Account.name mirrors the business/firm name.
            firm = getattr(cp, 'firm_name', '') or ''
            if firm and acc.name != firm:
                acc.name = firm
                changed.append('name')
            if changed:
                total_changes += len(changed)
                self.stdout.write(
                    f'  {acc.name or acc.id}: {", ".join(changed)}')
                if apply:
                    acc.save(update_fields=changed + ['updated_at'])

        self.stdout.write(
            f'\nTotal field updates {"applied" if apply else "pending"}: '
            f'{total_changes}')
