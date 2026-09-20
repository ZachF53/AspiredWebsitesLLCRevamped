"""
Backfill VaultCredential.automation_ssh_private_key_encrypted for SSH
credentials created before that field existed.

Why this exists: automated background jobs (the monthly droplet health
audit, and the existing payment-failure maintenance-mode/reinstatement
SSH steps) can only decrypt the automation-safe copy of a key. Every
credential provisioned before this field was added has an empty one.

Two cases:
  - Still encrypted_with_server_key=True (no admin has opened this
    credential in the vault UI yet) — safe to backfill headlessly,
    no PIN needed, same server key both copies use.
  - Already PIN-encrypted (an admin has opened it at least once) —
    this command CANNOT recover the plaintext without a PIN session.
    Reported as needing the one-click fix: open the credential in the
    vault UI and save the edit form once (the private key round-trips
    through the form either way, which repopulates the automation
    copy — see vault/views.py::_apply_ssh_fields).

Idempotent — skips rows that already have the automation copy set.

Usage:
  python manage.py backfill_automation_ssh_keys --dry-run
  python manage.py backfill_automation_ssh_keys
"""

import sys

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        'Backfill automation_ssh_private_key_encrypted on SSH '
        'credentials created before that field existed. Reports which '
        'ones need a manual vault-UI save because they are already '
        'PIN-encrypted.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would change; write nothing.')

    def handle(self, *args, **options):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

        dry_run = options['dry_run']

        from vault.crypto import decrypt_value, derive_server_key, encrypt_value
        from vault.models import VaultCredential

        targets = (VaultCredential.objects
                   .filter(is_ssh_credential=True,
                           automation_ssh_private_key_encrypted='')
                   .exclude(ssh_private_key_encrypted=''))

        server_key = derive_server_key()
        backfilled = 0
        needs_manual = 0

        for cred in targets:
            if cred.encrypted_with_server_key:
                plaintext = decrypt_value(
                    cred.ssh_private_key_encrypted, server_key)
                if not plaintext or plaintext.startswith('['):
                    self.stdout.write(self.style.WARNING(
                        f'  ✗ {cred.label} ({cred.pk}) — server-key '
                        f'decrypt failed, skipping'))
                    continue
                self.stdout.write(self.style.SUCCESS(
                    f'  ✓ {cred.label} ({cred.pk}) — backfilled from '
                    f'server-key copy'
                    + (' [DRY RUN]' if dry_run else '')))
                if not dry_run:
                    cred.automation_ssh_private_key_encrypted = encrypt_value(
                        plaintext, server_key)
                    cred.save(update_fields=[
                        'automation_ssh_private_key_encrypted',
                        'updated_at'])
                backfilled += 1
            else:
                self.stdout.write(self.style.WARNING(
                    f'  ! {cred.label} ({cred.pk}) — PIN-encrypted, needs '
                    f'a manual save: open it in the vault UI and hit '
                    f'Save Changes once.'))
                needs_manual += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. backfilled: {backfilled}  needs-manual-save: '
            f'{needs_manual}'))
        if dry_run:
            self.stdout.write(self.style.WARNING(
                'No writes performed. Re-run without --dry-run to apply.'))
