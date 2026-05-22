"""
Vault SSH-session helpers.

Vault-level TOTP means there is no longer a per-credential SSH session
window. The terminal authority is the vault PIN session itself (1 hour)
plus the `vault_totp_verified` flag set when the admin enters their
authenticator code on unlock.

This module wraps that single source of truth in small, request-bound
helpers so tests can exercise the rule without booting the full view
stack. The Channels consumer mirrors the same logic against the raw
session dict in `vault/consumers.py`.
"""

from datetime import datetime, timedelta

from django.utils import timezone

VAULT_SESSION_HOURS = 1


def _unlocked_at(request):
    raw = request.session.get('vault_unlocked_at')
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def is_vault_session_authenticated(request):
    """
    True iff the vault PIN session is still fresh AND vault-level TOTP
    has been verified during it. Both conditions must hold to open an
    SSH terminal.
    """
    unlocked_at = _unlocked_at(request)
    if unlocked_at is None:
        return False
    if timezone.now() > unlocked_at + timedelta(hours=VAULT_SESSION_HOURS):
        return False
    return bool(request.session.get('vault_totp_verified'))


def vault_session_remaining_seconds(request):
    """Seconds left on the current vault PIN session (0 if expired)."""
    unlocked_at = _unlocked_at(request)
    if unlocked_at is None:
        return 0
    remaining = (unlocked_at + timedelta(hours=VAULT_SESSION_HOURS)
                 - timezone.now())
    return max(int(remaining.total_seconds()), 0)
