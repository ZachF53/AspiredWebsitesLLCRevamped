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

from clients.account_setup import ACCOUNT_LEVEL_FIELDS, account_name_for


# Account-level fields that live on BOTH ClientProfile and Account.
# (Per-build fields — stage, payment, revisions, etc. — are NOT here;
# those belong to Website and were migrated separately.)
# Single definition in clients.account_setup so this command, the
# autocreate signal, and the parity validator cannot drift apart.
ACCOUNT_FIELDS = ACCOUNT_LEVEL_FIELDS


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
            expected_name = account_name_for(cp)
            if expected_name and acc.name != expected_name:
                acc.name = expected_name
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
