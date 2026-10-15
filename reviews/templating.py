"""
Guard-rails for the review-request wording a client edits in the portal.

WHY THIS EXISTS
---------------
The public service page promises, in these words: *"Asks every customer
the same way: no filtering, no incentives."* Two things break that
promise, and both get a business's Google listing penalised rather than
merely annoying someone:

* **Incentives** — "leave us a review for 10% off". Explicitly against
  Google's review policy.
* **Review gating** — "if you were happy, leave us a review". Screening
  by expected rating is the single most common way agencies violate that
  policy, and it is the thing a contractor will most naturally ask for.

A third class is carrier filtering rather than policy: a 10DLC message
that omits the business name, or that carries a public URL shortener,
gets filtered by carriers and quietly stops being delivered.

So the client gets a textarea, and this module decides what is allowed
in it. ``describe_template_problems`` returns a list of human-readable
problems (empty list = fine) and **never raises** — the caller decides
whether that is a form error or a refusal to render. Shaped on
``outreach/copy_guard.py``, which does the same job for cold email, and
it delegates to that module for the markdown and unfilled-placeholder
checks rather than reimplementing them.

Run it in BOTH places: when the portal form saves, and again in
``reviews.materialise`` when onboarding answers are promoted onto the
program. Defence in depth — wording that somehow reached the database
still cannot be handed to the operator as approved copy.

NOTE ON THE BOUNDARY: GoHighLevel actually sends these messages, from a
workflow the operator builds by hand. This module cannot stop a bad
message that was typed straight into GHL. It stops the portal from ever
*telling* the operator that bad copy is approved, which is the only lever
this app has.
"""

import re

# The review link and the opt-out line are not the client's to remove.
# `normalise_template` force-appends them if they are missing, so a
# client cannot strip them by racing the form validation.
LOCKED_LINK_TOKEN = '{review_link}'
LOCKED_FOOTER = 'Reply STOP to opt out, HELP for help.'

ALLOWED_PLACEHOLDERS = frozenset({
    'customer_first_name',
    'business_name',
    'job_type',
    'technician',
    'review_link',
})

# Two SMS segments. The locked footer eats into this budget, which is
# why the limit is checked against the normalised body and not the raw
# textarea contents.
SMS_SOFT_LIMIT = 320
SMS_HARD_LIMIT = 480

_INCENTIVE_PATTERNS = [
    (r'\bdiscount(s|ed)?\b', 'offers a discount'),
    (r'\bcoupon(s)?\b', 'offers a coupon'),
    (r'\bgift\s*card(s)?\b', 'offers a gift card'),
    (r'\braffle(s)?\b', 'offers a raffle entry'),
    (r'\bdrawing\b', 'offers a prize drawing'),
    (r'\bgiveaway(s)?\b', 'offers a giveaway'),
    (r'\breward(s|ed)?\b', 'offers a reward'),
    (r'\bprize(s)?\b', 'offers a prize'),
    (r'\bincentive(s)?\b', 'offers an incentive'),
    (r'\bwaive(d|s)?\b', 'offers to waive a charge'),
    (r'\bcash\s*(back)?\b', 'offers cash'),
    (r'\bentered?\s+to\s+win\b', 'offers a competition entry'),
    (r'\bfree\s+(service|visit|tune[- ]?up|inspection|month|repair)\b',
     'offers something free'),
    (r'\$\s*\d', 'names a dollar amount'),
    (r'\d+\s*%\s*off\b', 'offers a percentage off'),
    (r'\bcredit\s+(on|toward|towards)\b', 'offers account credit'),
]

# Screening by expected rating. Phrased as the contractor would phrase
# it, because that is what will actually get typed in.
_GATING_PATTERNS = [
    (r'\bif\s+you\s+(were|are)\s+(happy|satisfied|pleased)\b',
     'asks only happy customers'),
    (r'\bif\s+you\s+had\s+a\s+(good|great)\b', 'asks only happy customers'),
    (r'\bonly\s+if\s+you\b', 'conditions the ask on satisfaction'),
    (r'\b5\s*[- ]?\s*star[s]?\s+only\b', 'asks for a specific rating'),
    (r'\brate\s+us\s+5\b', 'asks for a specific rating'),
    (r'\bgive\s+us\s+(a\s+)?(5|five)\s*[- ]?\s*star', 'asks for a specific rating'),
    (r'\bif\s+you\'?d\s+give\s+us\s+(5|five)\b',
     'asks for a specific rating'),
]
# Deliberately NOT flagged: "how did we do?" and "were you satisfied?".
# Those read like gating but are not — Google's policy prohibits
# soliciting *selectively*, and a message that asks "how did we do?" and
# then links every customer to the same review page has screened nobody.
# The violation is a conditional ("if you were happy...") or a demand for
# a specific rating, which is what the patterns above actually match.
# Flagging a friendly opener would just make clients reword it and
# generate support tickets for no compliance gain.

_COMPILED_INCENTIVE = [(re.compile(p, re.IGNORECASE), why)
                       for p, why in _INCENTIVE_PATTERNS]
_COMPILED_GATING = [(re.compile(p, re.IGNORECASE), why)
                    for p, why in _GATING_PATTERNS]

_PLACEHOLDER_RE = re.compile(r'\{([a-z0-9_]+)\}', re.IGNORECASE)
# Any http(s) URL. The only link allowed is the {review_link} token.
_URL_RE = re.compile(r'https?://\S+', re.IGNORECASE)


def describe_template_problems(body, *, channel='sms', subject=''):
    """Human-readable problems with this wording. Empty list = fine.

    ``channel`` is 'sms' or 'email' — only the length rules differ.
    Never raises.
    """
    problems = []
    text = (body or '').strip()
    if not text:
        return ['The message is empty.']

    if LOCKED_LINK_TOKEN not in text:
        problems.append(
            f'The review link {LOCKED_LINK_TOKEN} must appear in the '
            'message — without it the customer has nowhere to go.')

    for name in set(_PLACEHOLDER_RE.findall(text)):
        if name.lower() not in ALLOWED_PLACEHOLDERS:
            allowed = ', '.join(sorted(ALLOWED_PLACEHOLDERS))
            problems.append(
                f'"{{{name}}}" is not a placeholder we can fill. '
                f'Available: {allowed}.')

    for pattern, why in _COMPILED_INCENTIVE:
        if pattern.search(text):
            problems.append(
                f'This {why}. Google forbids offering anything in exchange '
                'for a review, and it can get your listing penalised.')
            break

    for pattern, why in _COMPILED_GATING:
        if pattern.search(text):
            problems.append(
                f'This {why}. Every customer has to be asked the same way — '
                'filtering by who you think will leave a good review is '
                'against Google\'s policy.')
            break

    # A URL other than the locked token. Public shorteners are a top
    # 10DLC filtering trigger, and a second link defeats click tracking.
    for url in _URL_RE.findall(text):
        problems.append(
            f'Remove the link "{url}". Use {LOCKED_LINK_TOKEN} instead — a '
            'raw or shortened URL makes carriers filter the whole message.')
        break

    if channel == 'sms':
        normalised = normalise_template(text, channel='sms')
        # Measure with the longest realistic substitution rather than the
        # raw token, so a message that only fits while the placeholders
        # are short does not surprise anyone in production.
        measured = _measure(normalised)
        if len(measured) > SMS_HARD_LIMIT:
            problems.append(
                f'Too long for a text — about {len(measured)} characters '
                f'once the link and opt-out line are added (limit '
                f'{SMS_HARD_LIMIT}).')
        elif len(measured) > SMS_SOFT_LIMIT:
            problems.append(
                f'This will send as three or more texts (about '
                f'{len(measured)} characters with the link and opt-out '
                'line). Shorten it to keep it to two.')
        if '{business_name}' not in text and not _names_a_business(text):
            problems.append(
                'The message has to say who it is from. Add '
                '{business_name} — carriers require the sender to be '
                'identified in the first text.')

    if channel == 'email':
        if not (subject or '').strip():
            problems.append('The email needs a subject line.')

    # Markdown artefacts and unfilled placeholders — already solved for
    # cold email, same failure mode here.
    try:
        from outreach.copy_guard import describe_copy_problems
        shared = describe_copy_problems(subject or 'Review request', text)
        for problem in shared:
            low = problem.lower()
            # Its length and subject rules are tuned for cold email, and
            # a 2-line SMS trips the minimum-words check every time.
            if any(skip in low for skip in
                   ('word', 'subject', 'length', 'short', 'long')):
                continue
            problems.append(problem)
    except Exception:
        pass

    return problems


def _names_a_business(text):
    """True when the copy appears to hardcode a business name.

    A client who typed "Thanks for choosing Johnson HVAC!" has satisfied
    the carrier identification rule without using the placeholder, and
    refusing that would be wrong. Heuristic on purpose: two or more
    consecutive capitalised words, or a known suffix.
    """
    if re.search(r'\b(LLC|Inc|Co|Heating|Cooling|HVAC|Plumbing|Electric)\b',
                 text):
        return True
    return bool(re.search(r'\b[A-Z][a-z]+\s+[A-Z][a-z]+\b', text))


def _measure(text):
    """The body with placeholders replaced by realistic worst-case
    values, for length checking."""
    sample = {
        'customer_first_name': 'Christopher',
        'business_name': 'Johnson Heating & Cooling',
        'job_type': 'Air conditioning repair',
        'technician': 'Mike',
        # A real Google review short link.
        'review_link': 'https://g.page/r/CQcYnAbcDeFgHi/review',
    }
    out = text
    for key, value in sample.items():
        out = out.replace('{' + key + '}', value)
    return out


def normalise_template(body, *, channel='sms'):
    """The wording as it will actually be used.

    Force-appends the review link and, for SMS, the opt-out line when
    either is missing. ``describe_template_problems`` reports a missing
    link as a problem AND this puts it back, deliberately: validation
    tells the client what is wrong, normalisation guarantees the stored
    value is safe even if validation was bypassed.
    """
    text = (body or '').strip()
    if not text:
        return text
    if LOCKED_LINK_TOKEN not in text:
        text = f'{text}\n\n{LOCKED_LINK_TOKEN}'
    if channel == 'sms' and LOCKED_FOOTER.lower() not in text.lower():
        text = f'{text}\n{LOCKED_FOOTER}'
    return text


DEFAULT_SMS = (
    'Hi {customer_first_name}, thanks for choosing {business_name}! '
    'If you have a moment, we would really appreciate a quick Google '
    'review: {review_link}\n'
    + LOCKED_FOOTER
)

DEFAULT_EMAIL_SUBJECT = 'Thanks from {business_name}'

DEFAULT_EMAIL_BODY = (
    'Hi {customer_first_name},\n\n'
    'Thanks for choosing {business_name} — we appreciate your business.\n\n'
    'If you have a minute, a quick Google review really helps other '
    'homeowners find us:\n\n'
    '{review_link}\n\n'
    'Thanks again,\n'
    '{business_name}'
)
