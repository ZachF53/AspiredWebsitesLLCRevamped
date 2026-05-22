"""
Client-portal credentials vault — session helpers.

The portal credentials page (/portal/credentials/) is gated by a per-client
4-digit PIN, separate from the admin vault PIN. The PIN only verifies access;
unlike the admin vault it derives no encryption key, so nothing sensitive is
ever placed in the session — only the unlock timestamp.
"""

from datetime import datetime, timedelta

from django.utils import timezone

SESSION_MINUTES = 15
SESSION_KEY = 'client_vault_unlocked_at'


def mark_client_vault_unlocked(request):
    """Record a fresh unlock timestamp in the session."""
    request.session[SESSION_KEY] = timezone.now().isoformat()


def lock_client_vault(request):
    """Drop the unlock marker (explicit re-lock)."""
    request.session.pop(SESSION_KEY, None)


def _unlocked_at(request):
    """Parse the stored unlock timestamp, or None if absent/malformed."""
    raw = request.session.get(SESSION_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def is_client_vault_unlocked(request):
    """
    True if the client unlocked their vault within the last SESSION_MINUTES.
    A stale or malformed marker is cleared from the session as a side effect.
    """
    unlocked_at = _unlocked_at(request)
    if unlocked_at is None:
        request.session.pop(SESSION_KEY, None)
        return False
    if timezone.now() > unlocked_at + timedelta(minutes=SESSION_MINUTES):
        request.session.pop(SESSION_KEY, None)
        return False
    return True


def get_client_vault_remaining_seconds(request):
    """Seconds left on the current unlock window (0 if locked / expired)."""
    unlocked_at = _unlocked_at(request)
    if unlocked_at is None:
        return 0
    remaining = unlocked_at + timedelta(minutes=SESSION_MINUTES) - timezone.now()
    return max(int(remaining.total_seconds()), 0)
