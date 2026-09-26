"""Template helpers for public-site forms."""

from django import template

register = template.Library()


@register.simple_tag
def signed_form_timestamp():
    """A fresh signed render time for the spam-timing check.

    Lets the call-back form (core/_callback_form.html) be included on any
    page, including the booking page, without every view having to put a
    timestamp into its context.
    """
    from public.views import _signed_form_timestamp
    return _signed_form_timestamp()
