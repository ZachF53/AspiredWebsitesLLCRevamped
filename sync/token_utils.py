"""
HMAC-signed handoff tokens for the maintenance selection flow.

A token encodes a client id + 48-hour expiry, signed with
MOONIEFUL_SYNC_SECRET. It is the credential a Moonieful-referred client
uses to reach /maintenance/start/ without a full portal login.
"""

import base64
import hashlib
import hmac
import time

from django.conf import settings

TOKEN_TTL_SECONDS = 48 * 3600


def generate_handoff_token(client_id):
    """Return a URL-safe signed token for the given client id."""
    secret = settings.MOONIEFUL_SYNC_SECRET
    expiry = int(time.time()) + TOKEN_TTL_SECONDS
    payload = f'{client_id}:{expiry}'
    sig = hmac.new(
        secret.encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()
    token = base64.urlsafe_b64encode(
        f'{payload}:{sig}'.encode()
    ).decode()
    return token


def validate_handoff_token(token):
    """
    Return the client id encoded in a valid, unexpired token, or None.
    Uses a constant-time signature comparison.
    """
    try:
        secret = settings.MOONIEFUL_SYNC_SECRET
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        client_id, expiry, sig = decoded.rsplit(':', 2)
        if int(expiry) < int(time.time()):
            return None  # expired
        expected = hmac.new(
            secret.encode(),
            f'{client_id}:{expiry}'.encode(),
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(sig, expected):
            return client_id
        return None
    except Exception:
        return None
