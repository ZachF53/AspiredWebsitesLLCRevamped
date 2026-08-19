"""
Guard-rail for AI-generated outbound copy.

WHY THIS EXISTS
---------------
Between 2026-06-16 and 2026-08-01, ten cold emails went to real
prospects containing the model's *reply to us* rather than an email —
text like:

    I don't have enough specific details about Vilma Sikes's business
    to make a genuine, accurate observation … Once you share one real
    data point, I'll write the email.

Nothing in the pipeline checked that the model's output was actually an
email. ``_split_subject_body`` invented a subject whenever the model
didn't emit a ``Subject:`` line and then used the entire raw response as
the body, so a refusal became a perfectly well-formed outbound message.

Every one of those ten had the fabricated subject ``Quick question,
<name>``; none of the 406 good emails did. A missing ``Subject:`` line
is therefore treated here as a hard failure, not something to paper
over.

USE
---
``describe_copy_problems(subject, body)`` returns a list of
human-readable problems — empty list means the copy is safe to send.
It is deliberately cheap and dependency-free so it can run BOTH at
generation time (outreach/sender.py) and as a last gate immediately
before SMTP hand-off (outreach/dispatcher.py). Defense in depth: a row
that somehow reached ``approved`` with bad copy still cannot go out.

This module never raises. Callers decide what to do with the problems.
"""

import re

# The model addressing US instead of the prospect. Every pattern here
# was observed in copy that actually shipped — this is not speculative.
_META_RESPONSE_PATTERNS = [
    (r"I don'?t have (enough|any|sufficient)", 'asks the operator for more data'),
    (r'I do not have (enough|any|sufficient)', 'asks the operator for more data'),
    (r'(could|can|would) you provide', 'asks the operator a question'),
    (r'once you (share|provide|send)', 'waits on the operator'),
    (r'(your|my) instructions', 'refers to its own instructions'),
    (r"I need more (specific )?(details|info|information)",
     'asks the operator for more data'),
    (r"I (won'?t|will not|cannot|can'?t) (fabricate|make up|invent)",
     'refuses rather than writing copy'),
    (r'making something up would', 'refuses rather than writing copy'),
    (r"I'?ll write the (email|copy)", 'promises to write instead of writing'),
    (r'let me know if you', 'addresses the operator, not the prospect'),
    (r'\bas an AI\b', 'reveals itself as an AI'),
    (r"I'?m unable to|I am unable to", 'refuses rather than writing copy'),
    (r'here (are|is) (a few|some|two|three) (options|versions|drafts)',
     'offers drafts instead of one email'),
]

_COMPILED_META = [(re.compile(p, re.IGNORECASE), why)
                  for p, why in _META_RESPONSE_PATTERNS]

# Markdown has no place in a plain-text cold email (CLAUDE.md: "Plain
# text only"). Its presence means the model wrote a document, not mail.
_MARKDOWN_PATTERNS = [
    (r'\*\*[^*\n]+\*\*', 'contains markdown bold (**)'),
    (r'^\s*[-*]\s+\*\*', 'contains a markdown bullet list'),
    (r'^\s*#{1,6}\s+\S', 'contains a markdown heading'),
    (r'```', 'contains a code fence'),
]

_COMPILED_MARKDOWN = [(re.compile(p, re.IGNORECASE | re.MULTILINE), why)
                      for p, why in _MARKDOWN_PATTERNS]

# Unfilled template slots — a different failure mode with the same
# outcome (garbage to a prospect).
_PLACEHOLDER_PATTERNS = [
    (r'\[(insert|your|name|company|firm|city)[^\]]*\]',
     'contains an unfilled [placeholder]'),
    (r'\{\{.*?\}\}', 'contains an unfilled {{placeholder}}'),
    (r'<the email body>|<one line subject',
     'contains the literal prompt template'),
    (r'\bXXX+\b', 'contains XXX placeholder text'),
    (r'\bLorem ipsum\b', 'contains lorem ipsum'),
]

_COMPILED_PLACEHOLDER = [(re.compile(p, re.IGNORECASE), why)
                         for p, why in _PLACEHOLDER_PATTERNS]

# The system prompt asks for 60-120 words. These bounds are deliberately
# loose — they catch "the model returned one line" or "the model returned
# an essay", not stylistic drift.
_MIN_WORDS = 15
_MAX_WORDS = 400


def describe_copy_problems(subject, body):
    """Return a list of reasons this copy must not be sent.

    An empty list means the copy passed every check. Order is roughly
    most- to least-severe so the first item reads well in a log line or
    an admin ``rejected_reason``.
    """
    problems = []
    subject = (subject or '').strip()
    body = (body or '').strip()

    if not subject:
        problems.append('subject is empty')
    if not body:
        problems.append('body is empty')
        # Nothing further is meaningful without a body.
        return problems

    seen = set()

    def _add(reason):
        if reason not in seen:
            seen.add(reason)
            problems.append(reason)

    haystack = f'{subject}\n{body}'

    for rx, why in _COMPILED_META:
        if rx.search(haystack):
            _add(why)
    for rx, why in _COMPILED_MARKDOWN:
        if rx.search(body):
            _add(why)
    for rx, why in _COMPILED_PLACEHOLDER:
        if rx.search(haystack):
            _add(why)

    words = len(body.split())
    if words < _MIN_WORDS:
        _add(f'body is only {words} words (min {_MIN_WORDS})')
    elif words > _MAX_WORDS:
        _add(f'body is {words} words (max {_MAX_WORDS})')

    return problems


def is_sendable(subject, body):
    """Convenience boolean wrapper around describe_copy_problems()."""
    return not describe_copy_problems(subject, body)


# ── Pricing guardrail ──────────────────────────────────────────────────
#
# The agent may not invent, discount, or approximate a price. Real pricing
# lives in the database (billing.pricing_models.ServiceTier) and is the
# only pricing the business has agreed to honour — a number the model made
# up is a quote we are on the hook for.
#
# Cold outreach should almost never quote a price anyway: the ask is a
# call, not a sale. So the posture here is deny-by-default — ANY
# price-shaped text is a rejection unless it matches an active
# ServiceTier.price_display exactly.
#
# Unlike describe_copy_problems, this one reads the DB. It is a separate
# function precisely so the cheap dependency-free checks stay cheap and
# dependency-free.

# Money-shaped text: $2,500 / $2500.00 / $299/month / USD 299.
_PRICE_PATTERNS = [
    (r'\$\s?\d[\d,]*(?:\.\d{1,2})?', 'quotes a dollar figure'),
    (r'\b\d[\d,]*(?:\.\d{1,2})?\s?(?:USD|usd|dollars)\b',
     'quotes a dollar figure'),
    (r'\b\d+\s?%\s?(?:off|discount)', 'offers a percentage discount'),
    (r'\b(?:discount|% off|half[- ]price|free month|no charge|'
     r'waive[ds]?\s+(?:the\s+)?fee)\b', 'offers a discount or fee waiver'),
    (r'\b(?:starting at|starts at|as low as|from only|priced at)\b',
     'implies a price point'),
    (r'\b\d[\d,]*\s?(?:/|per\s)\s?(?:mo|month|yr|year|hour|hr)\b',
     'quotes a recurring rate'),
]

_COMPILED_PRICE = [(re.compile(p, re.IGNORECASE), why)
                   for p, why in _PRICE_PATTERNS]


def _approved_price_strings():
    """Every price string the business has actually agreed to publish.

    Returns a set of normalised (lowercased, whitespace-collapsed)
    strings drawn from active ServiceTier rows — both the human
    ``price_display`` and the raw decimal rendered a few common ways, so
    "$299" matches a tier stored as ``299.00``.

    Returns an empty set on any failure. That is deliberate: an empty
    allow-list means every price-shaped string is rejected, which fails
    CLOSED. A DB hiccup must never turn into permission to quote.
    """
    approved = set()
    try:
        from billing.pricing_models import ServiceTier
        rows = ServiceTier.objects.filter(is_active=True).values_list(
            'price', 'price_display')
    except Exception:  # noqa: BLE001 — see docstring: fail closed.
        return approved

    for price, display in rows:
        if display:
            approved.add(re.sub(r'\s+', ' ', display).strip().lower())
        if price is None:
            continue
        whole = int(price)
        if price == whole:
            approved.update({
                f'${whole}', f'${whole:,}', f'${whole}.00', f'${whole:,}.00',
            })
        else:
            approved.update({f'${price}', f'${price:,}'})
    return approved


def describe_pricing_problems(body, subject=''):
    """Return a list of reasons this copy must not be sent, on price grounds.

    Empty list means no price-shaped text, or every price found matches an
    active ServiceTier exactly.

    Never raises — callers decide what to do with the problems.
    """
    haystack = f'{(subject or "").strip()}\n{(body or "").strip()}'.strip()
    if not haystack:
        return []

    hits = []
    for rx, why in _COMPILED_PRICE:
        for match in rx.finditer(haystack):
            hits.append((match.group(0).strip(), why))
    if not hits:
        return []

    approved = _approved_price_strings()
    problems = []
    seen = set()
    for raw, why in hits:
        normalised = re.sub(r'\s+', ' ', raw).strip().lower()
        if normalised in approved:
            continue
        reason = f'{why}: {raw!r} is not an active ServiceTier price'
        if reason not in seen:
            seen.add(reason)
            problems.append(reason)
    return problems
