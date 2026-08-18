"""
Template access to the canonical owner of a row.

Twenty-two templates named the owner of a row as ``{{ x.client.firm_name }}``
and linked to it as ``{% url '...' x.client.id %}``. Both go through the
legacy FK, which is nullable now: writers set ``website_new`` /
``account_new`` and leave ``client`` alone. So those templates already
render wrongly for anything created since the cutover, and would render
wrongly for everything the moment the column is dropped.

The two forms fail differently, which is why there are two filters:

``|owner_label``
    For display. A missing attribute in ``{{ }}`` resolves to the empty
    string -- no exception, no log line, HTTP 200, and a blank cell where
    the client's name should be. That is the failure nobody notices.

``|owner_site`` / ``|owner_account``
    For links. ``{% url %}`` with an empty argument raises
    NoReverseMatch, which is a 500 on the whole page. Guard with
    ``{% if %}`` and these never hand ``{% url %}`` an empty value.

All three delegate to ``clients.display``, so templates and Python resolve
an owner the same way rather than by two similar-looking rules that drift.
"""

from django import template

from clients.display import (
    UNASSIGNED,
    owner_account as _owner_account,
    owner_label as _owner_label,
    owner_recipient as _owner_recipient,
    owner_site as _owner_site,
)

register = template.Library()


@register.filter
def owner_label(row):
    """Human name for whatever owns ``row`` -- site, else account, else
    the legacy profile. Never blank: falls back to a placeholder, because
    a blank cell reads as "no client" rather than "lookup failed"."""
    if row is None:
        return UNASSIGNED
    return _owner_label(row)


@register.filter
def owner_site(row):
    """The Website that owns ``row``, or None. Check before reversing."""
    if row is None:
        return None
    return _owner_site(row)


@register.filter
def owner_account(row):
    """The Account that owns ``row``, or None. Check before reversing."""
    if row is None:
        return None
    return _owner_account(row)


@register.filter
def owner_email(row):
    """Address to contact about ``row``, or ''.

    Templates gate a Send button on this. `{{ row.client.user.email }}`
    read the address through two legacy hops, so the button silently
    disappeared for canonical-only clients -- the send was not broken,
    it was unreachable.
    """
    if row is None:
        return ''
    return _owner_recipient(row)[0]
