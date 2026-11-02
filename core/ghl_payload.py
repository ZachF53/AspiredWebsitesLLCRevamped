"""
Reading GoHighLevel webhook bodies, whichever shape they arrive in.

GHL can send a workflow webhook two ways, and we receive both:

**Custom webhook** (what is wired today). A paid premium action whose
body is a free-text template. Merge values are interpolated as raw text,
so a ``"`` or a newline inside a transcript, summary, SMS reply or review
comment produces invalid JSON -- our endpoint 400s, GHL retries the same
broken body forever, and the event is lost with a retry loop that looks
like it is healing itself. That is why transcript/summary/reply-body/
review-text are currently omitted from those bodies.

**Standard "Webhook" action.** Free, and GHL serialises it properly, so
text fields are safe. But the shape is fixed and different: contact
fields sit at the top level (``contact_id``, ``full_name``, ``phone``,
``email``), location is nested (``location.id``), a reply's text is in
``message.body``, and anything the operator adds as Custom Data arrives
under ``customData``.

So ``first()`` looks in ``customData`` before the top level. Custom Data
is the only place an operator can put an explicit value on a standard
action, so when it is present it is deliberate and wins. Everything the
custom-webhook bodies already send keeps working untouched, because those
have no ``customData`` key at all.

Shared by voice.ghl_webhook and reviews.ghl_webhook rather than copied,
so the two endpoints cannot drift in how they read a payload -- they had
identical private copies of this before.
"""
import json
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

# How many raw bodies to log per endpoint, so the real shape of a live
# GHL payload can be confirmed once rather than guessed at. Counted in
# the cache, so it survives a worker restart but not a cache flush --
# over-logging a few extra bodies is harmless, under-logging is not.
RAW_LOG_LIMIT = 20
RAW_LOG_TTL = 60 * 60 * 24 * 30


def _lookup(container, key):
    """Value at `key`, which may be a dotted path. None if absent."""
    if not isinstance(container, dict):
        return None
    if '.' not in key:
        return container.get(key)
    cur = container
    for part in key.split('.'):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def first(payload, *keys):
    """First present, non-empty value among `keys`.

    Checks ``customData`` first (see module docstring), then the top
    level. Dotted paths work in both, so ``first(p, 'location.id')``
    finds a standard action's nested location and
    ``first(p, 'locationId')`` finds a custom body's flat one.

    Empty string counts as absent: GHL renders an unmatched merge field
    as '', and treating that as a value would overwrite good data with
    nothing.

    Containers also count as absent. Every caller wants a scalar, and
    the standard action nests things the custom bodies send flat -- so
    ``first(p, 'body', 'message', 'message.body')`` must not return the
    whole ``{'body': 'STOP'}`` dict just because ``message`` exists and
    is non-empty. Without this the alias ORDER silently decides whether
    a reply body is readable, which is how a STOP stopped being
    detected. Lists are skipped for the same reason; the two genuinely
    list-valued fields (``extractedData``, ``executedCallActions``) are
    read straight off the payload, not through here.
    """
    if not isinstance(payload, dict):
        return None

    def usable(value):
        return value not in (None, '') and not isinstance(
            value, (dict, list, tuple, set))

    custom = payload.get('customData')
    if isinstance(custom, dict):
        for key in keys:
            value = _lookup(custom, key)
            if usable(value):
                return value
    for key in keys:
        value = _lookup(payload, key)
        if usable(value):
            return value
    return None


def as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def coerce_duration_seconds(raw):
    """Call duration in whole seconds, from whatever GHL sent.

    ``{{voice_ai.duration}}`` arrives as a STRING and its format is not
    documented. Before this, the parse was ``int(float(raw))``, which
    turns "1:35" into a ValueError, caught, and billed as **zero
    seconds** -- silently, with the 300-minute cap never tripping. If
    GHL sends m:ss then every call metered zero.

    Accepted:
        "95" / 95 / "95.4"        -> 95
        "1:35"                    -> 95
        "1:02:03"                 -> 3723
        "PT1M35S" / "1m35s"       -> 95
        "" / None / junk          -> 0

    Returns (seconds, recognised). `recognised` is False when the value
    was non-empty but unparseable, so the caller can log the raw string
    rather than quietly bill nothing.
    """
    if raw is None:
        return 0, True
    if isinstance(raw, bool):
        return 0, False
    if isinstance(raw, (int, float)):
        return max(0, int(round(float(raw)))), True

    text = str(raw).strip()
    if not text:
        return 0, True

    # Plain number, possibly fractional.
    try:
        return max(0, int(round(float(text)))), True
    except ValueError:
        pass

    # Colon form: m:ss, h:mm:ss. Each part after the first must be a
    # valid 0-59 unit, or this is not a duration.
    if ':' in text:
        parts = text.split(':')
        if len(parts) <= 3:
            try:
                nums = [float(p) for p in parts]
            except ValueError:
                nums = None
            if nums is not None and all(
                    0 <= n < 60 for n in nums[1:]):
                total = 0.0
                for n in nums:
                    total = total * 60 + n
                return max(0, int(round(total))), True

    # ISO-8601 duration or a compact 1m35s / 2h3m4s form.
    import re
    match = re.fullmatch(
        r'(?:PT)?\s*(?:(\d+(?:\.\d+)?)\s*h)?\s*'
        r'(?:(\d+(?:\.\d+)?)\s*m)?\s*'
        r'(?:(\d+(?:\.\d+)?)\s*s)?',
        text, re.IGNORECASE)
    if match and any(match.groups()):
        h, m, s = (float(g or 0) for g in match.groups())
        return max(0, int(round(h * 3600 + m * 60 + s))), True

    return 0, False


def log_raw_body(endpoint, body_bytes):
    """Log the first RAW_LOG_LIMIT raw bodies seen by `endpoint`.

    The shape of a live GHL payload is currently inferred from docs and
    the workflow builder, not observed. This records the real thing for
    the first few deliveries so the field mapping can be confirmed, then
    goes quiet -- transcripts and reply bodies are customer content and
    do not belong in logs indefinitely.

    Never raises: a logging failure must not cost a webhook.
    """
    key = f'ghl_raw_log_count:{endpoint}'
    try:
        count = cache.get(key) or 0
        if count >= RAW_LOG_LIMIT:
            return
        cache.set(key, count + 1, RAW_LOG_TTL)
        try:
            text = body_bytes.decode('utf-8', 'replace')
        except Exception:  # noqa: BLE001
            text = repr(body_bytes)
        logger.info(
            'GHL raw body [%s] %s/%s: %s',
            endpoint, count + 1, RAW_LOG_LIMIT, text[:4000])
    except Exception:  # noqa: BLE001
        logger.exception('GHL raw-body logging failed for %s', endpoint)


def parse_body(request, endpoint):
    """``(payload_dict_or_None, raw_bytes)``. Logs the first N bodies."""
    raw = request.body or b''
    log_raw_body(endpoint, raw)
    try:
        payload = json.loads(raw.decode('utf-8') or '{}')
    except Exception:  # noqa: BLE001
        return None, raw
    if not isinstance(payload, dict):
        return None, raw
    return payload, raw


# Carrier/GHL stop keywords. An inbound reply whose entire body is one
# of these is an opt-out, not a conversation. GHL and the carrier own
# the actual suppression; we classify it so the portal does not show it
# as a reply needing a human answer, and so consent state is visible.
STOP_KEYWORDS = {
    'stop', 'stopall', 'unsubscribe', 'cancel', 'end', 'quit', 'revoke',
    'optout', 'opt out', 'opt-out',
}


def is_stop_keyword(body):
    """Is this reply body a bare opt-out keyword?

    Whole-body match only, case-insensitive, trimmed, punctuation
    stripped. "STOP" opts out; "stop by tomorrow with the quote" is a
    customer talking and must not be mistaken for one.
    """
    text = (body or '').strip().lower().strip('.!?,;:')
    if not text:
        return False
    return text in STOP_KEYWORDS
