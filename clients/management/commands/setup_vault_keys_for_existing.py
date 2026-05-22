"""
setup_vault_keys_for_existing — SSH into every legacy client server,
generate (or read back) a dedicated Ed25519 vault key, and store it as a
VaultCredential encrypted with the server-provisioning key.

Targets only legacy clients seeded by `seed_existing_clients` — those
with `internal_notes` containing 'Legacy client'. Idempotent: if a
credential already exists for the client the row is skipped.

SSH auth uses the local agent / ~/.ssh/id_* defaults — so the box you
run this on must already be authorised on every legacy Droplet's
authorized_keys (your laptop's key, or the production server's root key
once that key has been added on each legacy box).
"""

import time

import paramiko
from django.core.management.base import BaseCommand

from billing.do_helpers import _create_ssh_vault_credential
from clients.models import ClientProfile
from vault.models import ClientVault, VaultCredential

# Remote script — generate the key if missing, dump the private key
# either way. `set -e` would mask the existing-key shortcut, so the
# script handles errors itself.
GEN_VAULT_KEY_SCRIPT = r"""
mkdir -p /root/.ssh && chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
if [ -f /root/.ssh/vault_terminal_key ]; then
    cat /root/.ssh/vault_terminal_key
    exit 0
fi
ssh-keygen -t ed25519 -C "aspired-vault-terminal" \
    -f /root/.ssh/vault_terminal_key -N "" -q
cat /root/.ssh/vault_terminal_key.pub >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/vault_terminal_key
chmod 600 /root/.ssh/vault_terminal_key.pub
ssh -i /root/.ssh/vault_terminal_key \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o BatchMode=yes \
    -o ConnectTimeout=10 \
    root@127.0.0.1 "true" >/dev/null 2>&1 || true
cat /root/.ssh/vault_terminal_key
"""


class Command(BaseCommand):
    help = ('SSH into each legacy client server, generate or read back a '
            'dedicated vault key, and store it in the vault.')

    def handle(self, *args, **options):
        # NB: `do_droplet_ip` is a GenericIPAddressField, not a CharField.
        # `__isnull=False` alone is the right filter — chaining
        # `.exclude(do_droplet_ip='')` would silently drop every row
        # (see CLAUDE.md Phase 5a note).
        clients = (
            ClientProfile.objects
            .filter(do_droplet_ip__isnull=False,
                    internal_notes__contains='Legacy client')
            .order_by('firm_name')
        )

        if not clients.exists():
            self.stdout.write(self.style.WARNING(
                'No legacy clients found. Run `seed_existing_clients` first.'))
            return

        results = []
        for client in clients:
            results.append(self._process(client))
            time.sleep(1)

        self._print_summary(results)

    # ── per-server work ─────────────────────────────────────────────────────

    def _process(self, client):
        ip = client.do_droplet_ip
        firm = client.firm_name
        self.stdout.write(f'Processing {firm} ({ip})...')

        # Already done?
        vault = ClientVault.objects.filter(client=client).first()
        if vault and VaultCredential.objects.filter(
                vault=vault, is_ssh_credential=True).exists():
            self.stdout.write('  ↷ Skipped — vault key already exists')
            return {'firm': firm, 'ip': ip, 'status': 'SKIPPED',
                    'note': 'Vault key already exists'}

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(
                hostname=ip,
                port=22,
                username='root',
                timeout=20,
                allow_agent=True,
                look_for_keys=True,
            )
            _, stdout, stderr = ssh.exec_command(
                GEN_VAULT_KEY_SCRIPT, timeout=60)
            output = stdout.read().decode('utf-8', errors='replace')
            stderr.read()  # drain, but we don't surface it unless we fail
            ssh.close()

            private_key = _extract_openssh_private_key(output)
            if not private_key:
                raise RuntimeError(
                    f'could not extract private key from script output '
                    f'(first 200 chars: {output[:200]!r})')

            _create_ssh_vault_credential(client, ip, private_key)
            self.stdout.write('  ✓ Vault key created')
            return {'firm': firm, 'ip': ip, 'status': '✓ DONE',
                    'note': 'Vault key created'}

        except Exception as exc:  # noqa: BLE001 — surface in summary
            try:
                ssh.close()
            except Exception:
                pass
            self.stdout.write(f'  ✗ Failed: {str(exc)[:80]}')
            return {'firm': firm, 'ip': ip, 'status': '✗ FAILED',
                    'note': str(exc)[:80]}

    # ── reporting ───────────────────────────────────────────────────────────

    def _print_summary(self, results):
        self.stdout.write('')
        self.stdout.write('=' * 90)
        self.stdout.write(
            f'{"Server":<30} {"IP":<18} {"Status":<12} Notes')
        self.stdout.write('=' * 90)
        for r in results:
            self.stdout.write(
                f"{r['firm']:<30} {r['ip']:<18} "
                f"{r['status']:<12} {r['note']}")
        self.stdout.write('=' * 90)

        done = sum(1 for r in results if '✓' in r['status'])
        failed = sum(1 for r in results if '✗' in r['status'])
        skipped = sum(1 for r in results if 'SKIPPED' in r['status'])
        self.stdout.write('')
        self.stdout.write(
            f'✓ {done} created   ↷ {skipped} skipped   ✗ {failed} failed')
        if failed:
            self.stdout.write(self.style.WARNING(
                '\nFailed servers need manual attention — SSH in and run:\n'
                '  curl -s https://aspiredwebsites.com/static/scripts/'
                'gen_vault_key.sh | bash\n'
                'then paste the printed key into the vault for that client.'))


# ── helpers ─────────────────────────────────────────────────────────────────

def _extract_openssh_private_key(text):
    """
    Pull the FIRST `-----BEGIN OPENSSH PRIVATE KEY-----` block out of
    `text` (raw stdout from the remote script). Returns the key as a
    single string with trailing newline, or None if no key is present.
    """
    lines = text.splitlines()
    collected = []
    inside = False
    for line in lines:
        if line.startswith('-----BEGIN OPENSSH'):
            inside = True
            collected = [line]
            continue
        if inside:
            collected.append(line)
            if line.startswith('-----END OPENSSH'):
                return '\n'.join(collected) + '\n'
    return None
