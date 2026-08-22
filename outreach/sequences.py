"""
Cold sequence copy - the constant half of every email.

WHAT LIVES HERE vs WHAT CLAUDE WRITES
-------------------------------------
These templates are the CONTROL. They are identical for every recipient
in a campaign, which is what makes a reply rate mean something: if the
template never changes, a difference in reply rate is a difference in
the list or the icebreaker, not noise from copy that drifted.

``{{icebreaker}}`` is the one variable sentence, written per lead by
``outreach/icebreaker.py`` from measured facts. Everything else is
constant.

WHY THE COPY IS SHAPED THIS WAY
-------------------------------
* **Plain text, no HTML, no images, no tracking pixel on the first
  touch.** CLAUDE.md business rule 7. HTML in a cold email is a
  deliverability signal and reads as a mailer.
* **60-120 words.** Anything longer gets skimmed and deleted.
* **One question per email**, and it is small. "Worth a look?" converts;
  "book a 30-minute discovery call" does not, from a stranger.
* **No price, ever.** ``copy_guard`` rejects any price string that is
  not in the ServiceTier table, and quoting from a cold email anchors a
  number before the scope is known.
* **The security angle is a credential, not a hook.** Masters in
  Cybersecurity + CISSP is verifiable and genuinely unusual for a web
  designer. It is stated once, plainly, and never oversold.
* **Plain ASCII punctuation only.** No em-dashes, no curly quotes. Both
  read as machine-written to a growing number of people, and cold email
  is exactly where that suspicion costs a reply. ``describe_problems``
  enforces this so a later edit cannot quietly reintroduce it.

WHAT TOUCH 3 DELIBERATELY DOES NOT SAY
--------------------------------------
An earlier draft asserted "I checked your intake form and it isn't
encrypted." That is a per-lead claim, and it is FALSE for every lead
whose site does have SSL. Template copy cannot make per-lead factual
claims - only the icebreaker can, because only the icebreaker is
generated from that lead's own measurements. So touch 3 offers to check
rather than reporting a result.

COMPLIANCE
----------
CAN-SPAM requires a working opt-out and a valid physical postal address
in every commercial email. ``POSTAL_ADDRESS`` below is empty and MUST be
set to the real registered address before a single send. ``build_steps``
refuses to produce copy while it is unset.
"""

# ── CAN-SPAM ───────────────────────────────────────────────────────────
# Lives in .env as COMPANY_POSTAL_ADDRESS, not here, so a home address
# never reaches git and staging can differ from prod. Sending commercial
# email without one is a CAN-SPAM violation at up to $53,088 per message,
# and its absence is read as a spam signal by the major providers.
#
# The FTC accepts a current street address, a USPS-registered PO box, or
# a private mailbox at a Commercial Mail Receiving Agency. The test is
# whether mail sent there actually reaches you -- a registered-agent
# address only qualifies if the agent forwards general business mail
# rather than service of process alone.


def _configured_postal_address():
    from django.conf import settings
    return (getattr(settings, 'COMPANY_POSTAL_ADDRESS', '') or '').strip()

FOOTER = (
    '\n\n---\n'
    'Zachery Long, Aspired Websites LLC\n'
    '{postal}\n'
    "Don't want these? Reply \"no\" and I'll take you off my list.\n"
)


# ── Texas law firms ────────────────────────────────────────────────────
#
# First campaign. Chosen because the existing enrichment data is already
# law-heavy, so the icebreaker has real measurements to work from on day
# one rather than producing generic lines while the data catches up.

TEXAS_LAW = [
    {
        'name': 'Touch 1 - the observation',
        'delay_days': 0,
        'subject': 'quick question about {{companyName}}',
        'body': (
            "Hi {{firstName}},\n"
            "\n"
            "{{icebreaker}}\n"
            "\n"
            "I build websites for law firms around Texas. My background "
            "is security (Masters in Cybersecurity, CISSP), so I tend to "
            "notice things most designers walk past.\n"
            "\n"
            "Not pitching you anything today. Would it be useful if I "
            "sent over the two or three things I'd change?\n"
            "\n"
            "Thanks,\n"
            "Zachery"
        ),
    },
    {
        'name': 'Touch 2 - the offer, made smaller',
        'delay_days': 3,
        # Blank subject threads under touch 1 rather than starting a new
        # conversation. A follow-up that opens a second thread reads as
        # a sequence; one that threads reads as a person.
        'subject': '',
        'body': (
            "Hi {{firstName}},\n"
            "\n"
            "Following up on the note below.\n"
            "\n"
            "To be clear about what I'm offering: a short written list of "
            "what I'd fix on {{companyName}}'s site. No call required, no "
            "obligation, and I won't chase you about it.\n"
            "\n"
            "Just reply \"send it\" and I'll put it together this week.\n"
            "\n"
            "Thanks,\n"
            "Zachery"
        ),
    },
    {
        'name': 'Touch 3 - the security angle',
        'delay_days': 7,
        'subject': '',
        'body': (
            "Hi {{firstName}},\n"
            "\n"
            "One thing I check that most web designers don't: whether a "
            "firm's intake form actually transmits over an encrypted "
            "connection.\n"
            "\n"
            "When it doesn't, everything a prospective client types "
            "(name, phone, what happened to them) crosses the internet in "
            "plain text. For a law firm that's a confidentiality question "
            "before it's a design question.\n"
            "\n"
            "Takes about a minute to check. Want me to run it on "
            "{{companyName}} and send you what I find?\n"
            "\n"
            "Thanks,\n"
            "Zachery"
        ),
    },
    {
        'name': 'Touch 4 - close the loop',
        'delay_days': 14,
        'subject': '',
        'body': (
            "Hi {{firstName}},\n"
            "\n"
            "I'll leave it here, I don't want to clutter your inbox.\n"
            "\n"
            "If a site refresh ever moves up the list, I'm easy to find. "
            "And if you'd rather I didn't reach out again, reply \"no\" "
            "and you're off my list permanently.\n"
            "\n"
            "Either way, good luck with the practice.\n"
            "\n"
            "Thanks,\n"
            "Zachery"
        ),
    },
]


SEQUENCES = {
    'texas-law': TEXAS_LAW,
}


class SequenceError(Exception):
    """The copy is not fit to send."""


def build_steps(slug, postal_address=None):
    """Return the sequence with the CAN-SPAM footer appended to each step.

    Refuses to build without a postal address rather than quietly
    producing non-compliant copy - the whole point of putting the
    requirement in code is that it cannot be forgotten at 11pm.
    """
    steps = SEQUENCES.get(slug)
    if steps is None:
        raise SequenceError(
            f'No sequence named {slug!r}. Known: {sorted(SEQUENCES)}')

    postal = (postal_address or _configured_postal_address()).strip()
    if not postal:
        raise SequenceError(
            'No postal address set. CAN-SPAM requires a valid physical '
            'address in every commercial email. Set '
            'COMPANY_POSTAL_ADDRESS in .env, or pass --postal-address.')

    return [
        {
            'subject': step['subject'],
            'body': step['body'] + FOOTER.format(postal=postal),
            'delay_days': step['delay_days'],
        }
        for step in steps
    ]


# Characters that read as machine-written. Written as chr() calls so the
# check itself cannot be defeated by a source-file normaliser.
_MACHINE_PUNCTUATION = (
    (chr(8212), 'em-dash'),
    (chr(8211), 'en-dash'),
    (chr(8216), 'curly open quote'),
    (chr(8217), 'curly apostrophe'),
    (chr(8220), 'curly open quote'),
    (chr(8221), 'curly close quote'),
)


def describe_problems(steps):
    """Pre-flight the copy. Empty list means it is fit to send.

    Checks the things that are cheap to get wrong and expensive to
    discover after 200 sends.
    """
    from outreach import copy_guard

    problems = []
    for i, step in enumerate(steps, 1):
        body = step.get('body', '')
        subject = step.get('subject', '')

        if not body.strip():
            problems.append(f'Step {i}: empty body.')
            continue

        words = len(body.split())
        if words > 160:
            problems.append(
                f'Step {i}: {words} words - over 160, too long for cold.')

        if i == 1 and not subject.strip():
            problems.append('Step 1 must have a subject line.')

        if '<' in body and '>' in body:
            problems.append(
                f'Step {i}: looks like HTML. Cold email is plain text '
                f'only (business rule 7).')

        if 'Aspired Websites LLC' not in body:
            problems.append(f'Step {i}: missing the CAN-SPAM footer.')

        for char, label in _MACHINE_PUNCTUATION:
            if char in body or char in subject:
                problems.append(
                    f'Step {i}: contains a {label}. Use plain ASCII '
                    f'punctuation in cold copy.')
                break

        problems.extend(
            f'Step {i}: {p}'
            for p in copy_guard.describe_pricing_problems(body, subject))

    return problems
