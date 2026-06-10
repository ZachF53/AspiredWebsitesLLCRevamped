"""
Phase 5a — token-encryption wrappers.

Centralises the key choice for SocialToken so we don't sprinkle
`vault.crypto.derive_server_key()` calls across every publisher.
Server-key encryption (not PIN-key) so background Celery tasks
(publish_due_posts, refresh_expiring_tokens) can decrypt without
an admin PIN session.

Failure modes:
  encrypt_token  raises RuntimeError if VAULT_SERVER_SECRET is unset.
                 We surface this BEFORE the OAuth callback writes a
                 garbage row.
  decrypt_token  returns '' on any failure (no exceptions, no oracle).
                 Matches vault.crypto.decrypt_value semantics.

If we ever rotate VAULT_SERVER_SECRET, EVERY SocialToken row must be
re-encrypted first (same rule as billing/do_helpers SSH credentials).
"""

from vault.crypto import decrypt_value, derive_server_key, encrypt_value


def encrypt_token(plaintext: str) -> str:
    """Server-key-encrypted hex. Raises RuntimeError if
    VAULT_SERVER_SECRET is unset (derive_server_key raises ValueError;
    we re-raise as RuntimeError with a friendlier message so the
    OAuth callback can surface it to the operator)."""
    if not plaintext:
        return ''
    try:
        key = derive_server_key()
    except ValueError as exc:
        raise RuntimeError(
            'VAULT_SERVER_SECRET is not configured on this server. '
            'Add it to .env and restart gunicorn before connecting '
            'social media accounts.') from exc
    return encrypt_value(plaintext, key)


def decrypt_token(ciphertext_hex: str) -> str:
    """Decrypt a hex token. Returns '' on any failure — matches the
    no-oracle behaviour of vault.crypto.decrypt_value."""
    if not ciphertext_hex:
        return ''
    try:
        key = derive_server_key()
    except ValueError:
        return ''
    plain = decrypt_value(ciphertext_hex, key)
    if plain == '[decryption failed]':
        return ''
    return plain
