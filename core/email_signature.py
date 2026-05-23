"""
Single source of truth for the legal address footer that every
outgoing email gets. One constant here keeps the address consistent
across:

    1. Django's mail framework — handled automatically by
       `core.email_backend.AspiredEmailBackend`, which appends the
       footer to every EmailMessage on its way out.

    2. Direct SendGrid SDK callers (PDF-attachment paths in admin
       and reporting) — they call `append_signature(text, html)`
       just before building the Mail object.

Idempotent: a magic marker (`_SIG_MARKER`) is embedded in the
footer so repeat invocations don't double-stamp. Anyone passing
content through the helper twice (or anyone running an email
through the backend after the helper already touched it) is safe.
"""

from typing import Optional, Tuple


COMPANY_NAME = 'Aspired Websites LLC'
ADDRESS_LINES = (
    '8735 Dunwoody Place',
    'Ste R',
    'Atlanta, GA 30350, USA',
)

# Marker hidden in the HTML footer (and as a plain-text sentinel in
# the text footer) so we can detect "already signed" content and
# avoid double-appending. Hidden in HTML via a comment; for plain
# text we just look for the company name + ZIP pair.
_SIG_MARKER_HTML = '<!-- aspired-email-signature -->'
_SIG_MARKER_TEXT_HINT = '8735 Dunwoody Place'


def _is_already_signed(content: str) -> bool:
    """Return True if `content` already contains our footer marker."""
    if not content:
        return False
    return (_SIG_MARKER_HTML in content
            or _SIG_MARKER_TEXT_HINT in content)


def text_footer() -> str:
    """Plain-text footer separated by the standard sig-dash convention."""
    addr = '\n'.join(ADDRESS_LINES)
    return (
        '\n\n'
        '-- \n'
        f'{COMPANY_NAME}\n'
        f'{addr}\n'
    )


def html_footer() -> str:
    """HTML footer — minimal, muted, no styling that needs tokens."""
    addr_html = '<br>'.join(ADDRESS_LINES)
    return (
        f'{_SIG_MARKER_HTML}'
        '<div style="margin-top:24px;padding-top:14px;'
        'border-top:1px solid #e5e7eb;color:#6b7280;'
        'font-family:Arial,Helvetica,sans-serif;font-size:12px;'
        'line-height:1.5;">'
        f'<strong style="color:#374151;">{COMPANY_NAME}</strong><br>'
        f'{addr_html}'
        '</div>'
    )


def append_signature(
    text: Optional[str] = None,
    html: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Append the address footer to both text and HTML versions of an
    email body. Pass either or both. Returns the same pair back so
    callers can use it inline:

        text_body, html_body = append_signature(text_body, html_body)

    Idempotent — already-signed content is returned unchanged.
    """
    out_text = text
    if text is not None and not _is_already_signed(text):
        out_text = text + text_footer()

    out_html = html
    if html is not None and not _is_already_signed(html):
        # Try to inject the footer just before </body> for valid HTML
        # documents; otherwise just concatenate.
        lower = html.lower()
        idx = lower.rfind('</body>')
        if idx != -1:
            out_html = html[:idx] + html_footer() + html[idx:]
        else:
            out_html = html + html_footer()

    return out_text, out_html
