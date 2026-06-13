"""
Scope filter for reporting helpers.

Reporting models carry both the legacy `client` (ClientProfile) FK and
the `website_new` (Website) FK during the Phase-D teardown. Helpers
accept either object and build the right queryset filter, so the new
per-website admin pages and the legacy per-client callers (monthly
reports, portal, intelligence) both work off the same helpers.
"""


def scope_filter(scope):
    """Return a `.filter(**...)` dict for a Website or a ClientProfile."""
    from clients.account_models import Website
    if isinstance(scope, Website):
        return {'website_new': scope}
    return {'client': scope}
