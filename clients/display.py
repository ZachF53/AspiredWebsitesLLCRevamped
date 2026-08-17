"""
One way to name whatever owns a row.

Roughly thirty-five ``__str__`` methods across six apps were written as
``f'{self.client.firm_name} - ...'``. That was safe only while `client`
was NOT NULL. It is nullable now, and new writes leave it NULL, so every
one of those would raise ``AttributeError: 'NoneType' object has no
attribute 'firm_name'``.

``__str__`` raising is a nasty failure mode precisely because it is not
where anyone looks: it breaks the Django admin changelist, the `%s` in a
log line, the repr in an exception message -- including the exception
message for the *original* problem someone was trying to debug. So the
resolution lives in one function rather than being re-derived in thirty-
five f-strings.

Order is canonical-first, legacy-last, and it never raises.
"""

UNASSIGNED = '(unassigned)'


def owner_label(row):
    """Best available human name for the owner of ``row``.

    Prefers the site (the most specific true answer), then the account,
    then the legacy profile. Returns a placeholder rather than raising or
    returning None, because every caller is building a display string and
    a crash there is worse than a vague label.
    """
    for attr in ('website_new', 'website'):
        site = _safe(row, attr)
        if site is not None:
            return site.name or UNASSIGNED

    for attr in ('account_new', 'account'):
        account = _safe(row, attr)
        if account is not None:
            return account.name or UNASSIGNED

    client = _safe(row, 'client')
    if client is not None:
        return getattr(client, 'firm_name', '') or UNASSIGNED

    # Some rows reach the client one hop out, through the legacy project.
    project = _safe(row, 'project')
    if project is not None:
        client = _safe(project, 'client')
        if client is not None:
            return getattr(client, 'firm_name', '') or UNASSIGNED

    return UNASSIGNED


def owner_account(row):
    """The Account that owns ``row``, or None.

    Resolves through the site when the row is site-scoped, because most
    rows carry only ``website_new`` -- and the person to email, the
    billing relationship and the contact name are all account-level.
    """
    for attr in ('account_new', 'account'):
        account = _safe(row, attr)
        if account is not None:
            return account

    for attr in ('website_new', 'website'):
        site = _safe(row, attr)
        if site is not None:
            account = _safe(site, 'account')
            if account is not None:
                return account

    client = _safe(row, 'client')
    if client is not None:
        return _safe(client, 'migrated_account')

    return None


def owner_recipient(row):
    """``(email, name)`` for the person to contact about ``row``.

    Both may be empty strings; callers are expected to check the email and
    skip the send rather than deliver to nobody.

    This exists for the same reason as :func:`owner_label`, but the
    consequence is worse. Display code that read ``row.client.firm_name``
    rendered wrongly; *sending* code that read ``row.client.user.email``
    raised AttributeError the moment `client` went null, inside a Celery
    task, after the report it was attached to had already been generated.
    The work was done and thrown away, once a month, per client.
    """
    account = owner_account(row)
    if account is not None:
        user = _safe(account, 'user')
        email = (_safe(user, 'email') or '') if user is not None else ''
        email = email or (_safe(account, 'email_alt') or '')
        name = (_safe(account, 'contact_name') or ''
                or _safe(account, 'name') or '')
        return email, name

    client = _safe(row, 'client')
    if client is not None:
        user = _safe(client, 'user')
        email = (_safe(user, 'email') or '') if user is not None else ''
        name = (_safe(client, 'contact_name') or ''
                or _safe(client, 'firm_name') or '')
        return email, name

    return '', ''


def _safe(row, attr):
    """getattr that survives a broken or absent relation.

    A deleted related row, a null FK, or a model that simply has no such
    field all mean the same thing here: nothing to read. Since the only
    consumer is a display string, none of them is worth an exception.
    """
    try:
        return getattr(row, attr, None)
    except Exception:  # noqa: BLE001 -- relation missing or unloadable
        return None
