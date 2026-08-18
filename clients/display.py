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

    ``row`` may also BE a Website or an Account -- several sweeps iterate
    those and label them directly -- in which case it names itself.
    """
    if _is_instance(row, 'Website') or _is_instance(row, 'Account'):
        return getattr(row, 'name', '') or UNASSIGNED

    for attr in ('website_new', 'website'):
        site = _relation(row, attr)
        if site is not None:
            return getattr(site, 'name', '') or UNASSIGNED

    for attr in ('account_new', 'account'):
        account = _relation(row, attr)
        if account is not None:
            return getattr(account, 'name', '') or UNASSIGNED

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

    ``row`` may also BE an Account or a Website rather than something
    owned by one. That case is not hypothetical: the onboarding reminder
    sweeps iterate Accounts and Websites directly and hand them straight
    to :func:`owner_recipient`. Without this branch every one of those
    resolved to no owner, returned an empty address, and the reminder was
    skipped -- for a client who had paid and was waiting to be let in.
    """
    if _is_instance(row, 'Account'):
        return row
    if _is_instance(row, 'Website'):
        return _safe(row, 'account')

    for attr in ('account_new', 'account'):
        account = _relation(row, attr)
        if account is not None:
            return account

    for attr in ('website_new', 'website'):
        site = _relation(row, attr)
        if site is not None:
            account = _relation(site, 'account')
            if account is not None:
                return account

    client = _safe(row, 'client')
    if client is not None:
        return _safe(client, 'migrated_account')

    return None


def owner_site(row):
    """The Website that owns ``row``, or None.

    Templates need this separately from :func:`owner_label`, because a
    label and a link fail differently. ``{{ row.client.firm_name }}``
    against a null FK renders the empty string and the page still returns
    200 with a blank cell; ``{% url 'x' row.client.id %}`` against the
    same null FK raises NoReverseMatch and takes the whole page down.

    Returns the row itself when it already IS a Website, so a caller can
    hand over either without checking first.
    """
    if _is_instance(row, 'Website'):
        return row

    for attr in ('website_new', 'website', 'pointed_at_website'):
        site = _relation(row, attr)
        if site is not None:
            return site

    # One hop out, through the legacy project, for rows that never had a
    # direct site FK.
    project = _safe(row, 'project')
    if project is not None:
        site = _relation(project, 'migrated_website')
        if site is not None:
            return site

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


def _is_instance(row, model_name):
    """True when ``row`` is an instance of ``clients.<model_name>``.

    Imported lazily and guarded: this module is reached from model
    ``__str__`` methods, which run during app loading and during error
    handling, so it must never be the thing that raises.
    """
    try:
        from clients import account_models

        model = getattr(account_models, model_name, None)
        return model is not None and isinstance(row, model)
    except Exception:  # noqa: BLE001 -- registry not ready
        return False


def _relation(row, attr):
    """``getattr(row, attr)`` only when ``attr`` is genuinely a relation.

    The name alone is not enough to tell. ``ClientProfile.website`` is a
    **URLField** — a plain string holding the client's live URL — while
    ``PaymentRecord.website`` is a ForeignKey to Website. Matching on the
    name meant a legacy profile handed back its URL string and the caller
    then asked a `str` for `.name`.

    Django gives every forward relation a shadow ``<attr>_id`` attribute
    and gives plain fields nothing of the sort, so that is the test.
    """
    if not hasattr(row, f'{attr}_id'):
        return None
    return _safe(row, attr)


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
