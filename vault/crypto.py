"""
Vault cryptography — AES-256-GCM credential encryption, PBKDF2 key
derivation from the PIN, and PIN verification hashing.

The PIN is never stored. Only the verification hash and the encryption
salt are persisted. A wrong PIN yields garbage (or '[decryption failed]'),
never a distinguishable error — no decryption oracle.
"""

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from django.conf import settings

PBKDF2_ITERATIONS = 600_000


def derive_key(pin: str, salt: bytes) -> bytes:
    """
    Derive a 32-byte AES key from the PIN + salt via PBKDF2-HMAC-SHA256.
    The PIN itself is never stored — only the salt is.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(pin.encode('utf-8'))


def hash_pin(pin: str, salt: bytes) -> str:
    """
    Hash the PIN for verification storage. Uses a separate derivation
    (salt + b'verify') so the verification hash and the encryption key
    are independent — the stored hash never reveals the encryption key.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt + b'verify',
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.b64encode(kdf.derive(pin.encode('utf-8'))).decode('utf-8')


def verify_pin(pin: str, stored_hash: str, salt: bytes) -> bool:
    """Constant-time PIN verification (no timing oracle)."""
    expected = hash_pin(pin, salt)
    return hmac.compare_digest(expected, stored_hash)


def encrypt_value(value: str, key: bytes) -> str:
    """
    Encrypt a string with AES-256-GCM. Returns hex: nonce(12B) + ciphertext.
    Empty input returns an empty string.
    """
    if not value:
        return ''
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, value.encode('utf-8'), None)
    return (nonce + ciphertext).hex()


def decrypt_value(encrypted_hex: str, key: bytes) -> str:
    """
    Decrypt an AES-256-GCM hex string. Empty input returns ''.
    A wrong key (or any failure) returns '[decryption failed]' — it never
    raises and never reveals why, so there is no oracle to attack.
    """
    if not encrypted_hex:
        return ''
    try:
        data = bytes.fromhex(encrypted_hex)
        nonce, ciphertext = data[:12], data[12:]
        aesgcm = AESGCM(key)
        return aesgcm.decrypt(nonce, ciphertext, None).decode('utf-8')
    except Exception:
        return '[decryption failed]'


def make_hint(value: str) -> str:
    """A non-sensitive hint: first 3 chars + '***'. Never the full value."""
    if not value or len(value) < 3:
        return '***'
    return value[:3] + '***'


def generate_salt() -> bytes:
    """A cryptographically random 32-byte salt."""
    return os.urandom(32)


# ── Session key protection ───────────────────────────────────────────────────
# SECURITY: the unlocked vault key must NOT sit in the database-backed session
# in usable form. We wrap it with a key derived from SECRET_KEY (held in .env,
# never in the DB) before storing it in the session, and unwrap it per request.
# A database-only compromise therefore never yields a usable vault key.

def _server_key() -> bytes:
    """A stable 32-byte key derived from Django's SECRET_KEY."""
    return hashlib.sha256(settings.SECRET_KEY.encode('utf-8')).digest()


def wrap_key(key: bytes) -> str:
    """Encrypt the vault key with the server key for safe session storage."""
    return encrypt_value(base64.b64encode(key).decode('utf-8'), _server_key())


def unwrap_key(wrapped_hex: str):
    """Recover the vault key from its wrapped session form, or None."""
    if not wrapped_hex:
        return None
    inner = decrypt_value(wrapped_hex, _server_key())
    if not inner or inner == '[decryption failed]':
        return None
    try:
        return base64.b64decode(inner.encode('utf-8'))
    except Exception:
        return None
