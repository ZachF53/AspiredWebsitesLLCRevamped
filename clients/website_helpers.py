"""
Canonical resolver for the per-website FK stamped on client-scoped rows.

Part of the Phase-D ClientProfile teardown. Every model that carries both
the legacy `client` FK and the new `website_new` FK must set `website_new`
at write time — the admin and portal read paths filter on it, so a row
written without it is invisible in the UI even though it is on disk.

Three near-identical private copies of this had drifted across
`reporting/views.py`, `reporting/tasks.py` and `admin_dashboard/views.py`;
they now all delegate here.

Multi-website accounts: client-level rows can't be split per site, so they
attach to the primary (oldest) website — the same rule
`clients/management/commands/backfill_website_fks.py` uses, so live writes
and the backfill agree.
"""


def primary_website(client):
    """The client's primary (oldest) Website, or None.

    Returns None — never raises — when the client has no migrated Account
    or that Account has no Website yet. Callers store None and the row is
    picked up later by `backfill_website_fks`.
    """
    if client is None:
        return None
    try:
        account = client.migrated_account
    except Exception:  # noqa: BLE001 — reverse o2o raises when absent
        return None
    if account is None:
        return None
    return account.websites.order_by('created_at').first()


def website_for_project(project):
    """Primary Website for a Project, via its client. None if unresolvable."""
    if project is None:
        return None
    return primary_website(getattr(project, 'client', None))
