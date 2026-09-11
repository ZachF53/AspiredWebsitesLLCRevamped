"""
Shared HMAC helpers for the Moonieful sync bridge — used by both the inbound
endpoint (sync/views.py) and the outbound sender (run_sync.py).

Mirrors Moonieful's sync/security.py exactly (see docs/sync_contract.md — this
is a locked cross-repo contract, not a local convention). Two properties this
file has to hold:

1. **Fail closed.** With no secret configured, hmac still happily computes a
   signature over an empty key. Every function here refuses when the secret
   is missing or too short, rather than silently verifying against nothing.

2. **Freshness has to be part of what is signed.** The signature covers a
   canonical version + timestamp + body, all three verified together — a
   captured body+signature can't be replayed with a fresh timestamp, because
   the timestamp is inside what's signed, not a separate unsigned field.

Changing the signed message is a CONTRACT CHANGE. The Moonieful side must
compute the same string — update docs/sync_contract.md in both repos in the
same change.
"""
import hashlib
import hmac
import time

from django.conf import settings

# Bumped whenever the canonical signed message changes shape.
SIGNATURE_VERSION = 'v1'

# Shorter than this is not a secret. Guards against a placeholder like 'changeme'
# or a truncated copy-paste being treated as configuration.
MIN_SECRET_LENGTH = 32


class SyncNotConfigured(Exception):
    """No usable shared secret. Refuse rather than sign with an empty key."""


def _secret():
    secret = getattr(settings, 'MOONIEFUL_SYNC_SECRET', '') or ''
    if len(secret) < MIN_SECRET_LENGTH:
        raise SyncNotConfigured(
            'MOONIEFUL_SYNC_SECRET is missing or too short '
            f'(needs at least {MIN_SECRET_LENGTH} characters). Set it in '
            '.env on BOTH servers.'
        )
    return secret.encode()


def secret_configured():
    """True if there is a usable secret. For checks that must not raise."""
    try:
        _secret()
        return True
    except SyncNotConfigured:
        return False


def canonical_message(timestamp, raw_body):
    """Exactly what gets signed, byte for byte.

    Version and timestamp are inside the signature, so neither can be swapped
    without invalidating it. The separator is a newline, which cannot appear in
    the version or the timestamp, so the parts cannot be shifted into one
    another.
    """
    if isinstance(timestamp, int):
        timestamp = str(timestamp)
    prefix = f'{SIGNATURE_VERSION}\n{timestamp}\n'.encode('utf-8')
    return prefix + raw_body


def sign(timestamp, raw_body):
    """HMAC-SHA256 hex signature over the canonical message."""
    return hmac.new(
        _secret(), canonical_message(timestamp, raw_body), hashlib.sha256,
    ).hexdigest()


def verify(timestamp, raw_body, signature):
    """True only if the signature covers THIS body AND THIS timestamp."""
    if not signature:
        return False
    try:
        expected = sign(timestamp, raw_body)
    except SyncNotConfigured:
        # No secret means nothing can be authenticated. Refuse.
        return False
    return hmac.compare_digest(expected, signature)


def timestamp_fresh(ts):
    """True if the X-Sync-Timestamp is within the allowed tolerance window."""
    tolerance = getattr(settings, 'SYNC_TIMESTAMP_TOLERANCE', 300)
    try:
        sent = int(ts)
    except (TypeError, ValueError):
        return False
    return abs(int(time.time()) - sent) <= tolerance
