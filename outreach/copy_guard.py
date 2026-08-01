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
