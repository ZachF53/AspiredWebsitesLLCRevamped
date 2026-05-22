"""
SSH terminal session helpers.

After a TOTP code is verified, an SSH session is valid for SSH_SESSION_MINUTES.
The same logic is mirrored in vault/consumers.py (which reads the raw session
dict from the Channels scope rather than a request).
"""

from datetime import datetime, timedelta

from django.utils import timezone

SSH_SESSION_MINUTES = 15


def _verified_key(cred_id):
    return f'ssh_session_{cred_id}_verified'


def _verified_at_key(cred_id):
    return f'ssh_session_{cred_id}_verified_at'


def mark_ssh_session_verified(request, cred_id):
    """Record a fresh TOTP-verified SSH session for this credential."""
    request.session[_verified_key(cred_id)] = True
    request.session[_verified_at_key(cred_id)] = timezone.now().isoformat()


def _verified_at(request, cred_id):
    raw = request.session.get(_verified_at_key(cred_id))
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def is_ssh_session_valid(request, cred_id):
    """True if TOTP was verified for this credential within the last 15 min."""
    if not request.session.get(_verified_key(cred_id)):
        return False
    verified_at = _verified_at(request, cred_id)
    if verified_at is None:
        return False
    if timezone.now() > verified_at + timedelta(minutes=SSH_SESSION_MINUTES):
        request.session.pop(_verified_key(cred_id), None)
        request.session.pop(_verified_at_key(cred_id), None)
        return False
    return True


def ssh_session_remaining_seconds(request, cred_id):
    """Seconds left on the current TOTP session window (0 if none/expired)."""
    verified_at = _verified_at(request, cred_id)
    if verified_at is None:
        return 0
    remaining = (verified_at + timedelta(minutes=SSH_SESSION_MINUTES)
                 - timezone.now())
    return max(int(remaining.total_seconds()), 0)
