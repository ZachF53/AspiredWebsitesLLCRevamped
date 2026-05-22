#!/bin/bash
# Aspired Websites — Vault Key Generator
#
# Usage:
#   curl -s https://aspiredwebsites.com/static/scripts/gen_vault_key.sh | bash
#
# Generates an Ed25519 keypair, authorises it for root, verifies a
# loopback SSH login works, and prints the private key for you to paste
# into the vault. Idempotent — if the key already exists it just prints it.

set -e

echo "=== Aspired Websites Vault Key Setup ==="
echo "Server: $(hostname) | IP: $(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
echo ""

mkdir -p /root/.ssh
chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

# If key already exists, print and exit.
if [ -f /root/.ssh/vault_terminal_key ]; then
    echo "Vault key already exists — printing existing key:"
    echo ""
    cat /root/.ssh/vault_terminal_key
    echo ""
    echo "=== KEY ABOVE — paste into vault ==="
    exit 0
fi

# Generate Ed25519 keypair (no passphrase — the vault AES-encrypts it at rest).
ssh-keygen -t ed25519 \
    -C "aspired-vault-terminal" \
    -f /root/.ssh/vault_terminal_key \
    -N "" -q

# Authorise the new public key for root.
cat /root/.ssh/vault_terminal_key.pub >> /root/.ssh/authorized_keys

# Permissions.
chmod 600 /root/.ssh/vault_terminal_key
chmod 600 /root/.ssh/vault_terminal_key.pub

# Loopback test — prove the key actually authenticates as root.
LOOPBACK=$(ssh -i /root/.ssh/vault_terminal_key \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o BatchMode=yes \
    -o ConnectTimeout=10 \
    root@127.0.0.1 \
    "echo loopback_ok" 2>/dev/null || true)

if [ "$LOOPBACK" = "loopback_ok" ]; then
    echo "✓ Loopback SSH test passed"
else
    echo "✗ Loopback SSH test failed — check sshd_config allows root key auth"
fi

echo ""
echo "=== VAULT PRIVATE KEY ==="
cat /root/.ssh/vault_terminal_key
echo "=== END KEY ==="
