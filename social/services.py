"""
Cross-app service helpers — kept here so reporting/ can ask for a
Google access token without importing OAuth internals (mirrors the
brief's Phase 6.0 decoupling rule).

Phase 5a-pivot architecture: there is ONE GbpOperatorToken per agency
operator (manager-invite model), not per-client. Every client's GBP/
GSC work uses the same operator token. So `google_access_token(client)`
ignores the client argument and just returns the first operator token
on file — convenient for reporting tasks that loop over clients.

When 5b lands real Meta OAuth, this module also exposes meta_access_token
+ linkedin_access_token following the same pattern (per-channel for
those because each client has their own Facebook page / LinkedIn org).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def google_access_token(client=None) -> Optional[str]:
    """Return a usable Google access token for the agency operator,
    refreshing if needed. Returns None when no operator has connected
    yet, or when the token can't be decrypted/refreshed.

    The `client` argument is accepted for API symmetry with the brief
    and the eventual meta_access_token/linkedin_access_token helpers,
    but is unused — the GBP token is operator-scoped, not client-scoped.

    NEVER raises — Celery tasks rely on this for the "skip if not
    connected" guard.
    """
    try:
        from reporting.google_gbp import decrypt_token, refresh_if_needed
        from reporting.models import GbpOperatorToken
    except Exception:
        logger.exception('google_access_token: import failed')
        return None

    try:
        token = (GbpOperatorToken.objects
                 .order_by('created_at')
                 .first())
    except Exception:
        logger.exception('google_access_token: query failed')
        return None
    if token is None:
        return None

    try:
        refresh_if_needed(token)
    except Exception:
        # No refresh path or refresh failed — the operator must
        # re-connect. We return None instead of raising so Celery
        # tasks degrade cleanly.
        logger.warning(
            'google_access_token: refresh_if_needed failed for token %s',
            token.id)
        return None

    try:
        plain = decrypt_token(token.access_token_encrypted)
    except Exception:
        logger.exception('google_access_token: decrypt failed')
        return None
    return plain or None
