"""
Custom email backend that auto-appends the legal address footer AND
disables SendGrid's click + open tracking on every outgoing message.

Replaces the stock SMTP backend in settings.EMAIL_BACKEND. Every
Django `send_mail`, `EmailMessage`, and `EmailMultiAlternatives`
call flows through here — both plain-text bodies (`msg.body`) and
HTML alternatives (`msg.alternatives`) get the footer stamped on
before SMTP delivery to SendGrid.

Why tracking is OFF site-wide:
    SendGrid's click tracking rewrites every link as
    `url####.aspiredwebsites.com/ls/click?...` — that looks like
    phishing to anyone reading the URL before clicking, and the
    custom-domain tracking subdomain doesn't have a valid SSL cert
    in the SG UI either. Open tracking ships a 1x1 pixel that
    privacy-conscious clients block. Neither is worth the loss of
    deliverability + trust on transactional mail. We can re-enable
    per-message later by setting `msg.extra_headers['X-SMTPAPI']`
    to a different value BEFORE calling .send() — the backend only
    injects ours if no override is already present.

Direct SendGrid SDK callers (a small set in admin_dashboard + the
scan-runner) bypass this backend entirely; they use
`core.email_signature.append_signature` for the same effect, and
must opt out of tracking by passing a TrackingSettings object on
the Mail object.
"""

import json

from django.core.mail.backends.smtp import EmailBackend as SMTPBackend

from .email_signature import (
    _is_already_signed, html_footer, text_footer,
)


# SendGrid SMTP API header — disable click + open tracking entirely.
# Honoured by SendGrid's SMTP relay; per-message override of the
# account-level Mail Settings.
NO_TRACKING_HEADER_VALUE = json.dumps({
    'filters': {
        'clicktrack': {
            'settings': {'enable': 0, 'enable_text': 0},
        },
        'opentrack': {
            'settings': {'enable': 0},
        },
    },
})


class AspiredEmailBackend(SMTPBackend):
    """SMTP backend wrapper — address footer + tracking-off on send."""

    def send_messages(self, email_messages):
        for msg in email_messages or ():
            try:
                _append_footer_to_message(msg)
            except Exception:
                # Never let a footer-rendering bug block a real send.
                # The address requirement is important but a missing
                # footer is far better than a dropped client email.
                pass
            try:
                _disable_tracking(msg)
            except Exception:
                # Same posture — if anything goes sideways on the
                # header injection, fall back to whatever SG's
                # account-default does rather than dropping the send.
                pass
        return super().send_messages(email_messages)


def _append_footer_to_message(msg):
    """Mutate `msg` in place — add footer to body + HTML alternatives."""
    # Plain-text body. Empty bodies (rare but legal) get the footer
    # only if there's nothing else, so the email isn't completely
    # empty.
    if not _is_already_signed(msg.body or ''):
        msg.body = (msg.body or '') + text_footer()

    # HTML alternatives. In Django 5+ `msg.alternatives` is a list of
    # `EmailAlternative` namedtuples (`.content`, `.mimetype`); in
    # earlier versions it was plain `(content, mimetype)` tuples.
    # Iterating via `alt[0]` / `alt[1]` works for both. We re-assign
    # back using the SAME constructor we received — using a plain
    # tuple on Django 5+ breaks the SMTP serializer with
    # "AttributeError: 'tuple' object has no attribute 'content'".
    alts = list(getattr(msg, 'alternatives', None) or [])
    if not alts:
        return

    try:
        from django.core.mail.message import EmailAlternative
    except ImportError:
        EmailAlternative = None     # noqa: N806 — older Django

    new_alts = []
    for alt in alts:
        content = alt[0]
        mimetype = alt[1]
        if mimetype and 'html' in mimetype.lower():
            if not _is_already_signed(content or ''):
                content = _inject_html_footer(content or '')
        if EmailAlternative is not None and isinstance(
                alt, EmailAlternative):
            new_alts.append(EmailAlternative(content, mimetype))
        else:
            new_alts.append((content, mimetype))
    msg.alternatives = new_alts


def _inject_html_footer(html: str) -> str:
    """Place the HTML footer just inside </body> if present."""
    lower = html.lower()
    idx = lower.rfind('</body>')
    if idx != -1:
        return html[:idx] + html_footer() + html[idx:]
    return html + html_footer()


def _disable_tracking(msg):
    """Inject X-SMTPAPI to switch off SG click + open tracking.

    Only writes the header if the caller hasn't already set one —
    that lets future emails opt back in (e.g. cold outreach if we
    ever decide we want click counts there) without backend code
    changes. Set msg.extra_headers['X-SMTPAPI'] before sending and
    we leave it alone."""
    headers = getattr(msg, 'extra_headers', None)
    if headers is None:
        msg.extra_headers = headers = {}
    if 'X-SMTPAPI' not in headers:
        headers['X-SMTPAPI'] = NO_TRACKING_HEADER_VALUE
