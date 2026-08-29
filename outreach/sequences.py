"""
Cold sequence copy - the constant half of every email.

WHAT LIVES HERE vs WHAT CLAUDE WRITES
-------------------------------------
The template is the CONTROL. It is identical for every recipient in a
campaign, which is what makes a reply rate mean something: if the
template never changes, a difference in reply rate is a difference in
the list, the icebreaker, or the offer -- not noise from copy that
drifted.

``{{icebreaker}}`` is the one per-lead sentence, written by
``outreach/icebreaker.py``. The OFFER is the one per-campaign variable.
Everything else is fixed.

THE THREE THINGS THAT DECIDE WHETHER THIS WORKS
-----------------------------------------------
1. **A warm opener, not a critique.** The first version opened with a
   site defect -- "your PageSpeed is 36/100". Specific, and it proves we
   looked, but it tells a stranger their work is bad in sentence one.
   Nobody replies warmly to that. The opener now leads with something
   true about THEM. Research reads as respect; a defect list reads as a
   pitch.

2. **An offer.** Cold outreach without one asks for the prospect's time
   in exchange for nothing. With one it is a trade they can evaluate.
   See OFFERS below.

3. **Testing the offer, not just the copy.** The offer moves reply rate
   far more than wording does, so it is the thing worth A/B testing.
   Six are defined; each becomes its own campaign, and per-campaign
   analytics gives a clean per-offer reply rate.

OTHER CONSTRAINTS
-----------------
* **Plain text, no HTML, no images, no tracking pixel on touch one.**
  CLAUDE.md business rule 7.
* **Under ~190 words.** Anything longer gets skimmed.
* **One ask, and it is small.** "Reply yes" converts; "book a 30-minute
  discovery call" does not, from a stranger.
* **No price, ever.** ``copy_guard`` rejects any price string not in the
  ServiceTier table, and quoting before scope anchors the wrong number.
* **Plain ASCII punctuation only.** No em-dashes, no curly quotes. Both
  read as machine-written to a growing number of people, and cold email
  is exactly where that suspicion costs a reply.

WHAT TOUCH 3 DELIBERATELY DOES NOT SAY
--------------------------------------
An earlier draft asserted "I checked your intake form and it isn't
encrypted." That is a per-lead claim, and it is FALSE for every lead
whose site does have SSL. Template copy cannot make per-lead factual
claims - only the icebreaker can, because only the icebreaker is
generated from that lead's own measurements.

COMPLIANCE
----------
CAN-SPAM requires a working opt-out and a valid physical postal address
in every commercial email. ``build_steps`` refuses to produce copy while
COMPANY_POSTAL_ADDRESS is unset.
"""

import re


# ── CAN-SPAM ───────────────────────────────────────────────────────────
# Lives in .env as COMPANY_POSTAL_ADDRESS, not here, so a home address
# never reaches git and staging can differ from prod. Sending commercial
# email without one is a CAN-SPAM violation at up to $53,088 per message,
# and its absence is read as a spam signal by the major providers.
#
# The FTC accepts a current street address, a USPS-registered PO box, or
# a private mailbox at a Commercial Mail Receiving Agency. The test is
# whether mail sent there actually reaches you.


def _configured_postal_address():
    from django.conf import settings
    return (getattr(settings, 'COMPANY_POSTAL_ADDRESS', '') or '').strip()


FOOTER = (
    '\n\n---\n'
    'Zachery Long, Aspired Websites LLC\n'
    '{postal}\n'
    "Don't want these? Reply \"no\" and I'll take you off my list.\n"
)


# ── The offers ─────────────────────────────────────────────────────────
#
# Structure that works, in priority order:
#
#   1. Minimises financial risk   -> free, or money back
#   2. Minimises friction         -> one word starts it
#   3. Cheap for us to produce    -> or it stops scaling the moment it
#                                    starts working
#
# Rule 3 is the one that gets ignored and then hurts. An offer with a
# 10% reply rate that costs four hours to fulfil is a trap: succeed and
# you have sold yourself into unpaid full-time work.
# ``fulfilment_cost`` records that honestly for each one below.
#
# CONSENT IS NOT OPTIONAL
# -----------------------
# Anything that touches the prospect's infrastructure happens AFTER they
# say yes. Never before.
#
# reporting/scan_runner.py drives nmap, nikto and wpscan -- ACTIVE tools
# that send probe and attack-pattern traffic at a host. Running them
# against a stranger's server without authorisation is a Computer Fraud
# and Abuse Act problem and a Texas Penal Code sec. 33 problem, whatever
# the commercial intent, and an indefensible look for a firm whose whole
# pitch is that it takes security seriously.
#
# What happens BEFORE consent is passive and ordinary: fetch a public
# homepage, complete a TLS handshake, ask Google's PageSpeed API for a
# score. That is what any browser does. It is the basis for the opener,
# and it is where pre-consent work stops.

OFFERS = {
    'security_review': {
        'name': 'Free security + performance review',
        'appeals_to': 'compliance risk',
        'fulfilment_cost': (
            'MEDIUM today, not low - assemble the first ones by hand. '
            'scan_runner covers the security half but is CLIENT-scoped: '
            'generate_scan_pdf dereferences scan.client.id, so a scan '
            'raised against a Lead stores its findings and then produces '
            'no report at all (the failure is caught and logged, and the '
            'scan still reads "complete"). The performance half is not in '
            'scan_runner at all - no PageSpeed, no Lighthouse; that number '
            'is already on the lead from enricher.py. Both halves are '
            'deliverable manually, which is the right speed until a real '
            'reply tells you what the report should actually say.'),
        'pitch': (
            "Here's what I'd like to offer: I'll run a full security and "
            "performance review of your site and send you the report "
            "within 48 hours. Free, no strings, and I won't chase you "
            "about it. It covers whether your intake forms are encrypted "
            "end to end, what your site exposes publicly, and where "
            "people give up before the page loads."),
        'restate': (
            "a written security and performance review of "
            "{{companyName}}'s site, free, in your inbox within two days"),
        'ask': "Just reply \"yes\" and I'll get started.",
    },
    'homepage_mockup': {
        'name': 'Free homepage redesign mockup',
        'appeals_to': 'how the firm presents itself',
        'fulfilment_cost': (
            'HIGH - real design time per lead. Run this one at low volume '
            'and watch it, or a good reply rate becomes unpaid work.'),
        'pitch': (
            "Here's what I'd like to offer: I'll design a new homepage "
            "for your firm and send it over within a week. I'll do the "
            "work up front at my own cost. If you like it we can talk "
            "about building it; if you don't, keep the design and we "
            "never speak again."),
        'restate': (
            "a new homepage design for {{companyName}}, done up front at "
            "my cost, yours to keep either way"),
        'ask': "Just reply \"yes\" and I'll get started.",
    },
    'practice_area_page': {
        'name': 'Free practice-area page, written and built',
        'appeals_to': 'growth and search visibility',
        'fulfilment_cost': (
            'Medium - AI drafts the copy, but the build and review are '
            'manual.'),
        'pitch': (
            "Here's what I'd like to offer: pick any practice area you "
            "want to rank for and I'll write and build the page for it, "
            "free. You keep it whether or not we ever work together. "
            "Most firm sites have one thin page covering everything, "
            "which is why they don't rank for anything in particular."),
        'restate': (
            "one practice-area page, written and built for you free, "
            "yours to keep"),
        'ask': "Reply with the practice area and I'll start on it.",
    },
    'speed_guarantee': {
        'name': 'Speed fix, guaranteed or free',
        'appeals_to': 'a measurable number they can check',
        'fulfilment_cost': (
            'Medium - usually images, caching and render-blocking JS. '
            'Bounded work with a clear finish line.'),
        'pitch': (
            "Here's what I'd like to offer: I'll get your site loading "
            "in under two seconds on mobile, or you pay nothing. You can "
            "check the before and after yourself on Google PageSpeed, so "
            "there's nothing to take my word for. Most firm sites I look "
            "at are losing people before the page finishes loading."),
        'restate': (
            "your site under two seconds on mobile, verifiable on Google "
            "PageSpeed, or you pay nothing"),
        'ask': "Just reply \"yes\" and I'll get started.",
    },
    'competitor_teardown': {
        'name': 'Competitor teardown',
        'appeals_to': 'competitive standing',
        'fulfilment_cost': (
            'Low - same passive checks we already run, on two sites '
            'instead of one.'),
        'pitch': (
            "Here's what I'd like to offer: name the firm you most often "
            "lose clients to, and I'll send you a side-by-side of where "
            "they're beating you online - speed, search visibility, how "
            "easy they make it to get in touch. Free, and you'll have it "
            "in 48 hours."),
        'restate': (
            "a side-by-side of you against whichever firm you name, free, "
            "within two days"),
        'ask': "Reply with the firm's name and I'll put it together.",
    },
    'maintenance_month': {
        'name': 'Free month of maintenance',
        'appeals_to': 'the chore they keep postponing',
        'fulfilment_cost': (
            'Low per client, but ONGOING - each yes adds a site you are '
            'now responsible for. Cap the number you accept.'),
        'pitch': (
            "Here's what I'd like to offer: I'll take over maintenance of "
            "your current site for 30 days at no cost - updates, backups, "
            "uptime monitoring, and any small changes you want made. No "
            "contract and nothing to cancel. At the end you either keep "
            "me on or we shake hands and part ways."),
        'restate': (
            "30 days of maintenance on your current site at no cost - "
            "updates, backups, monitoring, small changes"),
        'ask': "Just reply \"yes\" and I'll get set up.",
    },
}

DEFAULT_OFFER = 'security_review'


# ── The sequence ───────────────────────────────────────────────────────
#
# One template, six offers. The offer text is substituted at build time
# so every campaign shares identical structure and differs only in the
# variable being tested.

_TOUCHES = [
    {
        'name': 'Touch 1 - warm opener + the offer',
        'delay_days': 0,
        'subject': 'quick question about {{companyName}}',
        'body': (
            "Hi {{firstName}},\n"
            "\n"
            "{{icebreaker}}\n"
            "\n"
            "I build websites for law firms in Texas, and my background "
            "is security - Masters in Cybersecurity and a CISSP.\n"
            "\n"
            "{offer_pitch}\n"
            "\n"
            "{offer_ask}\n"
            "\n"
            "Thanks,\n"
            "Zachery"
        ),
    },
    {
        'name': 'Touch 2 - restate the offer, smaller',
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
            "To be clear about what I'm offering: {offer_restate}. No "
            "call required, nothing to sign, and I won't chase you about "
            "it afterwards.\n"
            "\n"
            "{offer_ask}\n"
            "\n"
            "Thanks,\n"
            "Zachery"
        ),
    },
    {
        'name': 'Touch 3 - why this matters for a law firm',
        'delay_days': 7,
        'subject': '',
        'body': (
            "Hi {{firstName}},\n"
            "\n"
            "The reason I focus on law firms specifically: an intake form "
            "is usually the first thing a prospective client touches, and "
            "it often carries the most sensitive thing they will ever "
            "tell you - what happened to them, and when.\n"
            "\n"
            "Most firm sites treat that form as a design detail. It "
            "isn't, and the gap tends not to surface until somebody "
            "asks.\n"
            "\n"
            "The offer stands either way, and it's still free. "
            "{offer_ask}\n"
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
            "The offer doesn't expire. If you ever want it, reply to this "
            "email and I'll pick it up, whether that's next week or next "
            "year.\n"
            "\n"
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
    'texas-law': _TOUCHES,
}


class SequenceError(Exception):
    """The copy is not fit to send."""


def resolve_offer(offer):
    """Turn a key, an Offer row, or None into the dict we substitute from.

    The DATABASE is the source of truth. OFFERS above is the seed and the
    fallback -- it keeps the module importable and testable before the
    migration has run, and stops a fresh checkout being unable to build
    copy at all. If a key exists in both, the row wins, because the row
    is the one a human can edit without a deploy.
    """
    from outreach.models import Offer

    if offer is None:
        offer = DEFAULT_OFFER
    if isinstance(offer, Offer):
        return offer.as_dict()

    key = str(offer)
    try:
        row = Offer.objects.filter(key=key).first()
    except Exception:
        # No table yet (fresh checkout, or a test DB mid-migration).
        row = None
    if row is not None:
        return row.as_dict()

    spec = OFFERS.get(key)
    if spec is None:
        raise SequenceError(
            f'No offer named {key!r}. Known in code: {sorted(OFFERS)}. '
            f'Add it at /admin/outreach/offer/ or seed with '
            f'`manage.py seed_offers`.')
    return spec


def build_steps(slug, postal_address=None, offer=DEFAULT_OFFER):
    """Compose one sequence: template + chosen offer + CAN-SPAM footer.

    ``offer`` accepts an Offer row, its key, or None for the default.

    Refuses to build without a postal address rather than quietly
    producing non-compliant copy - the point of putting the requirement
    in code is that it cannot be forgotten at 11pm.
    """
    touches = SEQUENCES.get(slug)
    if touches is None:
        raise SequenceError(
            f'No sequence named {slug!r}. Known: {sorted(SEQUENCES)}')

    spec = resolve_offer(offer)

    postal = (postal_address or _configured_postal_address()).strip()
    if not postal:
        raise SequenceError(
            'No postal address set. CAN-SPAM requires a valid physical '
            'address in every commercial email. Set '
            'COMPANY_POSTAL_ADDRESS in .env, or pass --postal-address.')

    built = []
    for touch in touches:
        # NOT str.format(). The bodies contain Instantly's {{firstName}}
        # syntax, and format() reads "{{" as an escape for a literal "{",
        # so it silently rewrites every variable to {firstName} -- which
        # then ships to the prospect verbatim as "Hi {firstName},".
        # Caught by the render tests; it would have been catastrophic and
        # completely invisible in the funnel counts.
        body = touch['body']
        for slot, value in (
            ('{offer_pitch}', spec['pitch']),
            ('{offer_restate}', spec['restate']),
            ('{offer_ask}', spec['ask']),
        ):
            body = body.replace(slot, value)
        built.append({
            'subject': touch['subject'],
            'body': body + FOOTER.replace('{postal}', postal),
            'delay_days': touch['delay_days'],
            'offer': offer,
        })
    return built


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

# Every sequence must make an offer somewhere. A sequence that only
# describes what we do, without saying what the prospect gets and how to
# start, is the shape that produced a 0% reply rate across 416 sends.
_OFFER_MARKERS = ('free', 'no strings', 'no cost', 'pay nothing',
                  'at my cost', 'yours to keep')


def describe_problems(steps):
    """Pre-flight the copy. Empty list means it is fit to send."""
    from outreach import copy_guard

    problems = []
    for i, step in enumerate(steps, 1):
        body = step.get('body', '')
        subject = step.get('subject', '')

        if not body.strip():
            problems.append(f'Step {i}: empty body.')
            continue

        words = len(body.split())
        if words > 190:
            problems.append(f'Step {i}: {words} words - too long for cold.')

        if i == 1 and not subject.strip():
            problems.append('Step 1 must have a subject line.')

        if '<' in body and '>' in body:
            problems.append(
                f'Step {i}: looks like HTML. Cold email is plain text '
                f'only (business rule 7).')

        if 'Aspired Websites LLC' not in body:
            problems.append(f'Step {i}: missing the CAN-SPAM footer.')

        if '{offer_' in body or '{postal}' in body:
            problems.append(
                f'Step {i}: an unsubstituted template slot survived the '
                f'build and would ship literally.')

        for char, label in _MACHINE_PUNCTUATION:
            if char in body or char in subject:
                problems.append(
                    f'Step {i}: contains a {label}. Use plain ASCII '
                    f'punctuation in cold copy.')
                break

        problems.extend(
            f'Step {i}: {p}'
            for p in copy_guard.describe_pricing_problems(body, subject))

    # Checked across the sequence rather than per step - touch 4 is a
    # sign-off and does not need to re-pitch.
    joined = ' '.join(s.get('body', '') for s in steps).lower()
    if not any(marker in joined for marker in _OFFER_MARKERS):
        problems.append(
            'No offer anywhere in the sequence. Cold outreach without one '
            "asks for the prospect's time in exchange for nothing.")

    return problems


# ── Preview ────────────────────────────────────────────────────────────

def render_for_lead(step, lead):
    """Substitute Instantly's variables so a human can read the real email.

    Instantly does this substitution on its own side at send time, which
    means the exact text a prospect receives is never visible from Django
    -- you approve a template and hope. This renders it locally with one
    lead's data so the thing being approved is the thing being sent.

    Preview only. Nothing here is transmitted.
    """
    contact = (lead.attorney_name or '').strip()
    first = contact.split(' ')[0] if contact else ''

    values = {
        'firstName': first,
        'first_name': first,
        'lastName': ' '.join(contact.split(' ')[1:]) if contact else '',
        'companyName': lead.firm_name or '',
        'company_name': lead.firm_name or '',
        'icebreaker': lead.icebreaker or '',
        'personalization': lead.icebreaker or '',
        'city': lead.city or '',
        'state': lead.state or '',
        'website': lead.website or '',
        'business_type': lead.business_type or '',
    }

    def _sub(text):
        for key, value in values.items():
            text = text.replace('{{' + key + '}}', value)
        return text

    return {
        'subject': _sub(step.get('subject', '')),
        'body': _sub(step.get('body', '')),
        'delay_days': step.get('delay_days', 0),
        'offer': step.get('offer', ''),
    }


def compose_email(lead, campaign=None, touch=1, offer=None,
                  postal_address=None, sequence='texas-law'):
    """The finished email for one lead: template + offer + icebreaker.

    THIS IS THE DRAFT FUNCTION. Anything that needs to produce a real
    email -- the preview command, an approval queue, and eventually
    Prospect -- should call this rather than assembling the pieces
    itself, because the pieces are easy to assemble WRONG.

    The two per-thing variables come from two different places and both
    are required:

        offer       <- the CAMPAIGN (an Offer row; the A/B arm)
        icebreaker  <- the LEAD     (written from that lead's own facts)

    Precedence for the offer: explicit ``offer`` argument, else the
    campaign's, else the default. That order lets a caller preview an
    alternative offer against a live campaign without touching it.

    Returns a dict with subject, body, offer_key and any unresolved
    variables. A non-empty ``unresolved`` is a blocker, never a warning:
    a surviving placeholder ships literally, and "Hi {{firstName}}," is
    worse than sending nothing at all.
    """
    chosen = offer or (campaign.offer if campaign and campaign.offer_id
                       else DEFAULT_OFFER)
    steps = build_steps(sequence, postal_address, offer=chosen)

    index = max(1, min(int(touch), len(steps))) - 1
    rendered = render_for_lead(steps[index], lead)

    spec_key = (chosen.key if hasattr(chosen, 'key')
                else (chosen if isinstance(chosen, str) else DEFAULT_OFFER))
    unresolved = (unresolved_variables(rendered['body'])
                  + unresolved_variables(rendered['subject']))

    return {
        'subject': rendered['subject'],
        'body': rendered['body'],
        'touch': index + 1,
        'delay_days': rendered['delay_days'],
        'offer_key': spec_key,
        'has_icebreaker': bool((lead.icebreaker or '').strip()),
        'unresolved': unresolved,
    }


def unresolved_variables(text):
    """Any {{placeholder}} the render did not fill.

    A leftover placeholder ships literally to the prospect -- "Hi
    {{firstName}}," is worse than no email at all, and it is the classic
    mail-merge failure. Treat a non-empty result as a blocker.
    """
    return sorted(set(re.findall(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}',
                                 text or '')))
