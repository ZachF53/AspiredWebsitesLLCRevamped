"""
Everything that exists only because the legacy tables still exist.

Two things live here, and both disappear with the drop:

- ``delete_legacy_mirror`` / ``delete_mirror_for`` — cleanup. While the
  tables exist, deleting an Account has to delete its legacy mirror too,
  or the row is orphaned and the parity audit reports it forever.
- ``is_legacy_profile`` — recognition. `clients.display` has to answer
  "was I handed a ClientProfile?" so it can name one and find its
  account; that question stops existing when the model does.

Neither is a *source of truth*. Nothing here decides business state or
tells a caller what a client is, which is the property that makes the
readiness-gate exemption honest.

This lives in its own module, listed in `legacy_audit.ALLOWED_PREFIXES`
alongside the parity and backfill machinery, for the same reason those
are: a gate that counts transition machinery as a blocker is
unsatisfiable, because the machinery is what performs the transition.

Two rules keep that exemption honest:

1. Nothing here may be a source of truth about a client. Recognising a
   model class is allowed; answering "what package is this client on" is
   not. If a caller needs to *know* something, it reads the canonical row.
2. This module is deleted whole by the drop change. If something needs
   to survive it, it does not belong here.

`clients.tests_legacy_teardown` asserts both properties.
"""

import logging

logger = logging.getLogger(__name__)


def delete_mirror_for(account):
    """Delete the ClientProfile mirroring ``account``, before it is gone.

    Call this *before* ``account.delete()``. The FK is ``SET_NULL``, so
    once the account row is deleted the link is already NULL in the
    database and only Django's in-memory cache still holds the profile --
    which works, but is not a thing to build a destructive path on.

    Taking the account here rather than at the call site is what keeps
    the last ``legacy_client_profile`` read inside this module, so the
    readiness gate has exactly one allowlisted place to account for
    instead of a line buried in a view.
    """
    if account is None:
        return False
    return delete_legacy_mirror(getattr(account, 'legacy_client_profile', None))


def delete_legacy_mirror(profile):
    """Delete a legacy ClientProfile left behind by an Account delete.

    ``Account.legacy_client_profile`` is declared ``on_delete=SET_NULL``,
    so deleting the Account leaves the profile behind rather than
    cascading to it -- and the profile still owns intake, tickets, scans
    and reports through its own FKs. Left alone it becomes an orphan the
    audit keeps reporting and the drop would take silently.

    Takes the profile rather than the account, because the caller has to
    resolve it *before* the delete anyway: afterwards the column is NULL
    in the database and only Django's in-memory cache still holds it.

    Returns True when a row was deleted. Never raises: it runs inside the
    delete transaction, and failing to tidy a mirror is not a reason to
    abort deleting the account the admin actually asked to delete.
    """
    if profile is None:
        return False
    try:
        profile.delete()
    except Exception:  # noqa: BLE001 -- cleanup is best-effort
        logger.exception(
            'legacy teardown: could not delete legacy profile %s',
            getattr(profile, 'pk', '?'))
        return False
    return True


def is_legacy_profile(row):
    """True when ``row`` IS a ClientProfile.

    `clients.display` needs this to name a legacy profile handed to it
    directly and to reach its Account. Imported lazily and guarded,
    because display is reached from ``__str__`` during app loading and
    during error handling, so it must never be the thing that raises.

    Lives here rather than in display.py so the readiness gate has one
    allowlisted module to account for instead of a legacy import inside
    core runtime code.
    """
    try:
        from clients.models import ClientProfile

        return isinstance(row, ClientProfile)
    except Exception:  # noqa: BLE001 -- registry not ready
        return False
