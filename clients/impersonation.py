"""
Staff "view as client" — read-only portal impersonation.

Single source of truth for the session state, the audit row, and the
read-only flag. Everything else (the guard middleware, the banner context
processor, the handful of suppressed GET writes) reads from here.

── Why the operator is never logged in as the client ──

`django.contrib.auth.login()` is deliberately NOT used. Two reasons, both
disqualifying:

  1. It calls `session.cycle_key()`, which would destroy the operator's
     own session — they would be logged out of the admin dashboard to
     look at a portal.
  2. It fires `user_logged_in`, which writes `user.last_login` on the
     CLIENT's user row. The client is not told a session happened, so a
     mutated `last_login` is exactly the fingerprint this feature must
     not leave.

So the operator stays authenticated as themselves for the whole session
and only account *resolution* changes — see `clients/decorators.py`,
which substitutes the target in `client_required`.

── Fail-closed revalidation ──

`active_target()` re-checks staff status AND that the session's recorded
operator is still the authenticated user, on every single call. Anything
unexpected clears the session keys and closes the audit row rather than
resolving a target. A stale impersonation key degrades to ordinary
browsing, never to someone else's portal.
"""

import logging

from django.utils import timezone

from .portal_resolvers import clear_active_website

logger = logging.getLogger(__name__)


SESSION_ACCOUNT = 'impersonate_account_id'
SESSION_OPERATOR = 'impersonate_operator_id'
SESSION_LOG = 'impersonate_log_id'

_SESSION_KEYS = (SESSION_ACCOUNT, SESSION_OPERATOR, SESSION_LOG)

# Resolution is cached per request: the guard middleware, the decorator,
# the context processor and each suppressed write all ask the same
# question, and without this that is four Account lookups per request.
_CACHE_ATTR = '_impersonation_target_cache'


def _clear_session(request):
    """Drop every impersonation key. Also drops the active-website slug,
    which would otherwise leak the impersonated site into the operator's
    own portal on the way out (resolve_website persists it to session)."""
    for key in _SESSION_KEYS:
        request.session.pop(key, None)
    clear_active_website(request)


def get_log(request):
    """The ImpersonationSession row for the live session, or None."""
    log_id = request.session.get(SESSION_LOG)
    if not log_id:
        return None
    from admin_dashboard.models import ImpersonationSession

    return ImpersonationSession.objects.filter(id=log_id).first()


def _close(log, reason):
    """Stamp a row closed. Idempotent — an already-closed row keeps its
    original reason, because the first close is the true one."""
    if log is None or log.ended_at is not None:
        return
    log.ended_at = timezone.now()
    log.end_reason = reason
    log.save(update_fields=['ended_at', 'end_reason', 'updated_at'])


def _resolve(request):
    """Uncached resolution. Returns the target Account or None."""
    account_id = request.session.get(SESSION_ACCOUNT)
    if not account_id:
        return None

    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated or not user.is_staff:
        # Either the session outlived the login, or a non-staff user
        # somehow holds the key. Neither should resolve a target.
        logger.warning(
            'impersonation: clearing keys — requester is not staff')
        _close(get_log(request), 'operator_mismatch')
        _clear_session(request)
        return None

    if request.session.get(SESSION_OPERATOR) != user.pk:
        logger.warning(
            'impersonation: operator mismatch — session says %s, request '
            'is user %s', request.session.get(SESSION_OPERATOR), user.pk)
        _close(get_log(request), 'operator_mismatch')
        _clear_session(request)
        return None

    from clients.account_models import Account

    account = Account.objects.filter(id=account_id).first()
    if account is None:
        logger.warning(
            'impersonation: target account %s no longer exists', account_id)
        _close(get_log(request), 'operator_mismatch')
        _clear_session(request)
        return None

    return account


def active_target(request):
    """The Account being viewed, or None when not impersonating."""
    if hasattr(request, _CACHE_ATTR):
        return getattr(request, _CACHE_ATTR)
    target = _resolve(request)
    setattr(request, _CACHE_ATTR, target)
    return target


def is_read_only(request):
    """True when this request is a staff view-as session.

    Named for what callers care about rather than for the mechanism: the
    GET-write suppressions read this, and what they need to know is "may
    I write", not "who is this".
    """
    if request is None:
        return False
    return active_target(request) is not None


def begin(request, account):
    """Start a session against `account` and return the audit row.

    Closes any row this operator already has open, so operator → client A
    → client B without exiting leaves A closed as 'superseded' rather
    than open forever.
    """
    from admin_dashboard.models import ImpersonationSession

    stale = ImpersonationSession.objects.filter(
        operator=request.user, ended_at__isnull=True)
    for row in stale:
        _close(row, 'superseded')

    # Cleared rather than preserved: the operator's own active-website
    # slug must not carry into the impersonated view, or the portal would
    # resolve a site the target does not own and 404.
    _clear_session(request)

    log = ImpersonationSession.objects.create(
        operator=request.user,
        account=account,
        website=account.websites.first(),
        ip_address=_client_ip(request),
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:1000],
    )

    request.session[SESSION_ACCOUNT] = str(account.id)
    request.session[SESSION_OPERATOR] = request.user.pk
    request.session[SESSION_LOG] = str(log.id)
    # Invalidate the per-request cache so a resolve later in THIS request
    # sees the new target instead of the None it cached on the way in.
    if hasattr(request, _CACHE_ATTR):
        delattr(request, _CACHE_ATTR)

    logger.info(
        'impersonation: %s began viewing account %s',
        request.user.username, account.pk)
    return log


def end(request, reason='manual_exit'):
    """Close the live session and drop the keys. Safe to call when no
    session is active."""
    log = get_log(request)
    _close(log, reason)
    _clear_session(request)
    if hasattr(request, _CACHE_ATTR):
        delattr(request, _CACHE_ATTR)
    return log


def note_blocked(request):
    """Record that the guard refused a state-changing request.

    Uses an F() expression so concurrent blocked requests both count
    instead of racing on a read-modify-write.
    """
    from django.db.models import F

    from admin_dashboard.models import ImpersonationSession

    log_id = request.session.get(SESSION_LOG)
    if not log_id:
        return
    ImpersonationSession.objects.filter(id=log_id).update(
        blocked_attempts=F('blocked_attempts') + 1,
        updated_at=timezone.now(),
    )


def _client_ip(request):
    """Best-effort client IP. Behind nginx the real address is the first
    hop in X-Forwarded-For; REMOTE_ADDR would be the proxy itself."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip() or None
    return request.META.get('REMOTE_ADDR') or None
