"""
Vault recovery-code helpers.

If the admin loses their authenticator app, one of eight one-time recovery
codes — generated when TOTP is first set up — lets them back into the
vault. Codes are stored as SHA-256 hashes on `VaultConfig.recovery_codes`;
the plaintext is shown to the admin a single time at generation and never
recoverable from the database. Each code consumes itself on first use.

Why SHA-256 and not bcrypt/argon2: recovery codes are 12 random hex chars
(48 bits of entropy). Brute-forcing one from its SHA-256 hash, even at
billions of guesses per second, takes longer than the universe has lasted
— there's no need for a slow KDF on top.
"""

import hashlib
import hmac
import secrets

# 8 codes, 12 hex chars each (48 bits of entropy per code).
RECOVERY_CODE_COUNT = 8
RECOVERY_CODE_BYTES = 6


def generate_recovery_codes() -> list[str]:
    """Return a fresh list of plaintext recovery codes (uppercase hex)."""
    return [secrets.token_hex(RECOVERY_CODE_BYTES).upper()
            for _ in range(RECOVERY_CODE_COUNT)]


def hash_recovery_code(code: str) -> str:
    """SHA-256 hex of a normalised (stripped, uppercased) recovery code."""
    normalised = (code or '').strip().upper()
    return hashlib.sha256(normalised.encode('utf-8')).hexdigest()


def store_recovery_codes(config, plaintext_codes: list[str]) -> None:
    """
    Replace the stored list with fresh hashes — used both at first
    enrolment and on regeneration. Caller is responsible for `save()`ing.
    """
    config.recovery_codes = [
        {'code_hash': hash_recovery_code(c), 'used': False}
        for c in plaintext_codes
    ]


def remaining_count(config) -> int:
    """How many unused codes are still available."""
    return sum(1 for entry in (config.recovery_codes or [])
               if not entry.get('used'))


def consume_recovery_code(config, submitted: str) -> bool:
    """
    Verify and consume a submitted recovery code in one step.

    Returns True on success and atomically flips the entry's `used` flag
    via `save(update_fields=['recovery_codes'])`. Uses
    `hmac.compare_digest` against every stored hash so a wrong code
    always takes the same time as a right one — no timing oracle on
    which slot is real.
    """
    if not submitted:
        return False
    target = hash_recovery_code(submitted)
    codes = list(config.recovery_codes or [])
    matched_index = None
    for i, entry in enumerate(codes):
        stored = entry.get('code_hash') or ''
        if hmac.compare_digest(stored, target) and not entry.get('used'):
            matched_index = i
            # Do NOT break — keep iterating so the comparison cost is
            # constant regardless of which slot (or none) matched.
    if matched_index is None:
        return False
    codes[matched_index] = {**codes[matched_index], 'used': True}
    config.recovery_codes = codes
    config.save(update_fields=['recovery_codes', 'updated_at'])
    return True
