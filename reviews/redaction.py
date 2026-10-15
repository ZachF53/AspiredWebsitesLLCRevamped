"""
Redact end-customer PII before a payload is rendered, logged or alerted.

This app introduces a PII class the project did not previously hold: the
phone numbers and email addresses of **our clients' customers** — people
who have no relationship with us and never agreed to anything. We store
them because GHL reports them, and the client needs to recognise who a
reply came from.

That changes the breach-notification profile, so the raw payload must
never leak into a log line, a system alert, an admin page, or an export.
Modelled on ``sync/views._redact``, which strips password hashes before
they reach ``SyncLog``.
"""

_SENSITIVE_KEY_PARTS = (
    'phone', 'mobile', 'email', 'address', 'street', 'zip', 'postal',
    'ssn', 'ein', 'tax', 'token', 'secret', 'password', 'authorization',
    'api_key', 'apikey',
)

_KEEP_LAST = 4


def _mask(value):
    text = str(value)
    if not text:
        return ''
    if '@' in text:
        local, _, domain = text.partition('@')
        head = local[:1] if local else ''
        return f'{head}***@{domain}'
    digits = [c for c in text if c.isdigit()]
    if len(digits) >= _KEEP_LAST:
        return f'***{"".join(digits[-_KEEP_LAST:])}'
    return '***'


def _is_sensitive(key):
    low = str(key).lower()
    return any(part in low for part in _SENSITIVE_KEY_PARTS)


def redact_payload(value, _depth=0):
    """A copy of `value` with sensitive leaves masked.

    Recurses dicts and lists. Depth-capped so a hostile or cyclic
    payload cannot blow the stack — GHL payloads are shallow, and
    anything 12 levels deep is not something we display anyway.
    """
    if _depth > 12:
        return '[truncated]'
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            if _is_sensitive(key) and not isinstance(inner, (dict, list)):
                out[key] = _mask(inner)
            else:
                out[key] = redact_payload(inner, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_payload(v, _depth + 1) for v in value]
    return value
