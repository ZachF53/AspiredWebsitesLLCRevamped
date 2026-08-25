"""
Per-lead icebreaker generation — the one specific true sentence.

WHY A SINGLE LINE RATHER THAN A WHOLE EMAIL
-------------------------------------------
Under the SendGrid architecture, Claude wrote the entire email per lead.
That made every send a separate LLM call, made A/B testing meaningless
(no two emails shared a control), and put the guardrails in the position
of policing free-form text on every single send.

With Instantly owning the sequence, the split is cleaner and cheaper:

    Instantly holds the template  — the constant, the thing being tested
    Django writes {{icebreaker}}  — the variable, one sentence per lead

One sentence is also the only part a recipient actually reads as
personal. The rest of a cold email is structurally identical no matter
who receives it, and pretending otherwise costs tokens without buying
anything.

WHAT MAKES A GOOD ICEBREAKER HERE
---------------------------------
``enricher.py`` already computes the signals: PageSpeed performance
score, whether the site answers on https, and the copyright year in the
footer. For a law firm or a medical practice those are not cosmetic
observations — an intake form posted over plain http is a confidentiality
and HIPAA exposure, and saying so is a genuinely useful message from
someone holding a CISSP rather than a compliment about their colour
scheme.

FABRICATION IS THE FAILURE MODE
-------------------------------
An invented detail is worse than a generic line: it is instantly
checkable, and being caught inventing a fact about someone's business
ends the conversation permanently. So the prompt is given only verified
facts, is told explicitly that thin data is normal, and the output is
checked against a fabrication screen before it is stored.
"""

import logging
import re

from django.utils import timezone

logger = logging.getLogger(__name__)

# One or two sentences. Long enough to say something, short enough that
# it reads as an opening line and not a paragraph.
MAX_TOKENS = 300
MAX_CHARS = 320

# Copyright older than this many years reads as an abandoned site.
STALE_COPYRIGHT_YEARS = 3

# PageSpeed performance at or below this is slow enough to be worth
# mentioning; above it, saying "your site is slow" is a lie.
SLOW_PERFORMANCE_SCORE = 50


class IcebreakerError(Exception):
    """Generation failed — the lead keeps its previous icebreaker."""


def warm_facts(lead):
    """Friendly, true things about the BUSINESS - not about its website.

    The opener used to lead with a site defect. "Your PageSpeed is
    36/100" is specific and proves we looked, but it tells a stranger
    their work is bad in sentence one, and people do not reply warmly to
    that. Research reads as respect; a defect list reads as a pitch.

    So the opener now draws from here, and the site findings feed the
    OFFER instead -- which is where a problem belongs, because there it
    comes attached to a free fix rather than to a criticism.

    Everything returned is a stored field the guard can verify.
    """
    from django.utils import timezone

    facts = []
    if lead.founded_year:
        years = timezone.now().year - lead.founded_year
        if 0 < years <= 150:
            facts.append((
                'tenure',
                f"The firm was founded in {lead.founded_year}, so about "
                f"{years} years in practice."))
    if lead.practice_areas:
        facts.append((
            'practice_areas',
            f"Their stated practice areas: {lead.practice_areas}."))
    if lead.city:
        facts.append(('city', f"They are based in {lead.city}."))
    # Google reviews, cited two different ways.
    #
    # The single 4.5-star rule left too much on the table: a firm with 200
    # reviews at 4.2 is demonstrably busy and well regarded, and got
    # nothing -- so its opener fell back to "X caught my attention", which
    # is the generic line this whole module exists to avoid.
    #
    # But volume and rating are not interchangeable. Quoting "3.9 stars"
    # back at someone is a criticism wearing a statistic, and the opener
    # must only ever contain something the firm is pleased to hear. So:
    #
    #   strong rating  -> cite the rating AND the count
    #   high volume    -> cite the COUNT ONLY, never the number of stars
    #   weak rating    -> say nothing, and let another fact carry the line
    #
    # The second branch is the one the Places join unlocks. It never
    # mentions stars, so it cannot become a backhanded compliment.
    count = lead.google_review_count or 0
    rating = float(lead.google_rating) if lead.google_rating else 0.0
    if count >= 10 and rating >= 4.5:
        facts.append((
            'reviews',
            f"{count} Google reviews averaging {rating} stars."))
    elif count >= 40 and rating >= 4.0:
        facts.append((
            'review_volume',
            f"{count} Google reviews - a lot of clients took the time to "
            f"leave one. Mention the NUMBER of reviews only; do NOT "
            f"mention the star rating."))
    return facts


def observations(lead):
    """Verified, checkable facts about this lead's web presence.

    Only things measured directly. Nothing inferred, nothing guessed.
    Returns a list of (key, sentence) so the caller can tell which signal
    fired and the prompt can be given plain English.

    SITE-QUALITY CLAIMS REQUIRE A SITE
    ----------------------------------
    A parked or unreachable domain is not a bad website, it is the
    absence of one, and every observation below is meaningless against
    it. PageSpeed scored theascendantgroup.com's Wix parking page 89/100
    -- an excellent score for a page that does not exist. Without this
    guard the generator would compliment a parking page, or criticise
    intake forms that are not there.
    """
    from outreach import enricher

    found = []

    # The domain resolves but there is no site behind it. That IS the
    # observation; nothing else measured about it means anything, so this
    # returns rather than falling through to the site-quality signals.
    if lead.site_status in (enricher.ISSUE_PARKED,
                            enricher.ISSUE_UNREACHABLE):
        return [(
            'no_real_website',
            "Their domain does not currently serve a working website - "
            "it is parked, a builder placeholder, or unreachable.")]

    if lead.has_ssl is False:
        detail = f' ({lead.tls_error})' if lead.tls_error else ''
        found.append((
            'no_ssl',
            f"Their website does not serve a valid certificate over "
            f"https{detail}. Any contact or intake form on it is not "
            f"protected in transit."))

    if lead.website_performance_score is not None:
        if lead.website_performance_score <= SLOW_PERFORMANCE_SCORE:
            found.append((
                'slow',
                f"Google PageSpeed scores their site "
                f"{lead.website_performance_score}/100 on performance."))

    if lead.website_mobile_score is not None and lead.website_mobile_score <= 50:
        found.append((
            'mobile',
            f"Their site scores {lead.website_mobile_score}/100 on mobile."))

    if lead.copyright_year:
        age = timezone.now().year - lead.copyright_year
        if age >= STALE_COPYRIGHT_YEARS:
            found.append((
                'stale_copyright',
                f"The footer copyright still reads {lead.copyright_year}, "
                f"{age} years out of date."))

    if not lead.website:
        found.append((
            'no_website',
            "They have no website listed at all."))

    if lead.has_generic_email:
        found.append((
            'generic_email',
            "Their business email is on a free consumer provider rather "
            "than their own domain."))

    if lead.google_review_count and lead.google_rating:
        if lead.google_review_count >= 10 and float(lead.google_rating) >= 4.5:
            found.append((
                'good_reviews',
                f"They have {lead.google_review_count} Google reviews "
                f"averaging {lead.google_rating} stars."))

    return found


def _facts_block(lead):
    """Everything the model is allowed to know, as plain text."""
    lines = [f'Business name: {lead.firm_name}']
    if lead.attorney_name:
        lines.append(f'Contact person: {lead.attorney_name}')
    if lead.business_type:
        lines.append(f'Business type: {lead.business_type}')
    if lead.practice_area:
        lines.append(f'Practice area: {lead.practice_area}')
    if lead.city or lead.state:
        lines.append(f'Location: {lead.city}, {lead.state}'.strip(', '))
    if lead.website:
        lines.append(f'Website: {lead.website}')
    if lead.notes:
        lines.append(f'Context: {lead.notes}')

    obs = observations(lead)
    if obs:
        lines.append('')
        lines.append('Verified observations about their web presence:')
        lines.extend(f'  - {text}' for _, text in obs)
    else:
        lines.append('')
        lines.append(
            'No web-presence measurements are available for this lead.')
    return '\n'.join(lines)


SYSTEM_PROMPT_OLD_CRITIQUE_STYLE = (
    "You are Zachery Long, founder of Aspired Websites LLC — a custom web "
    "design agency serving law firms and small businesses in Texas and "
    "Georgia. You hold a Masters in Cybersecurity and a CISSP; security "
    "is the firm's primary differentiator."
    "\n\n"
    "Your job is to write ONE opening line for a cold email. It will be "
    "inserted into a template as the first sentence, so it must read as "
    "though you looked at this specific business before writing."
    "\n\n"
    "RULES — all of them matter:\n"
    "  * One or two sentences. Under 40 words. Never a paragraph.\n"
    "  * Reference ONLY the verified observations you are given. If you "
    "are given none, write a short honest line about why you are "
    "reaching out to businesses of this type in this city. A slightly "
    "generic line is CORRECT when the data is thin.\n"
    "  * NEVER invent a fact: no made-up client names, no invented "
    "statistics, no 'I noticed you recently...' unless it is in the "
    "observations.\n"
    "  * Never mention price, packages, or a discount.\n"
    "  * No greeting, no 'Hi <name>', no sign-off. The template supplies "
    "those. Start directly with the observation.\n"
    "  * Plain text. No markdown, no bullet points, no quotation marks "
    "around the line.\n"
    "  * Do not ask for more information and do not describe what you "
    "are doing. Output the line and nothing else."
    "\n\n"
    "Tone: direct, warm, specific. You are a working professional "
    "pointing something out, not a salesperson opening a pitch."
)


SYSTEM_PROMPT = (
    "You are Zachery Long, founder of Aspired Websites LLC - a custom web "
    "design agency serving law firms in Texas and Georgia.\n\n"
    "Your job is to write ONE warm opening line for a cold email to a law "
    "firm. It is the first thing they read, and its only job is to make "
    "them feel this was written for them rather than blasted at them.\n\n"
    "RULES:\n"
    "  * One or two sentences. Under 35 words.\n"
    "  * Reference ONLY the facts you are given. If you are given very "
    "little, write a short honest line about reaching out to firms of "
    "this kind in this city. A slightly generic line is CORRECT when the "
    "data is thin.\n"
    "  * NEVER criticise their website, their marketing, or their "
    "business. Not even gently. The email makes an offer later; the "
    "opening line is not the place to point out a problem.\n"
    "  * NEVER invent a fact: no made-up cases, awards, podcasts, "
    "rankings, client counts or years in practice.\n"
    "  * Never mention price or packages.\n"
    "  * No greeting and no sign-off - the template supplies those.\n"
    "  * Plain text. No markdown, no quotes around the line.\n"
    "  * Use plain ASCII punctuation. No em-dashes, no curly quotes.\n"
    "  * Output the line and nothing else. Never explain yourself.\n\n"
    "Good shape: note how long they have been practising, or what they "
    "practise, and why that made you reach out. Specific and respectful, "
    "the way one professional writes to another. Not flattery, and not a "
    "compliment you cannot back up."
)


# Phrases that mean the model editorialised instead of writing the line.
_META_PATTERNS = (
    r'\bas an ai\b', r"\bi can'?t\b", r'\bi cannot\b', r'\bi don\'?t have\b',
    r'\bcould you (please )?(provide|share|tell)\b',
    r'\bhere(\'s| is) (a|the|an)\b', r'\bopening line\b', r'\bicebreaker\b',
    r'\blet me know\b', r'\bi need more\b', r'\bplease provide\b',
)

# Claims we can never substantiate from lead data. Catching these is the
# difference between a checkable observation and a lie the prospect can
# disprove in one click.
_FABRICATION_PATTERNS = (
    r'\bi (just |recently )?(saw|read|watched|listened to)\b',
    r'\byour recent\b', r'\bcongratulations on\b', r'\byour award\b',
    r'\byour (new )?(podcast|webinar|book|article|blog post)\b',
    r'\bwe worked with\b', r'\bour client\b', r'\b\d+% (more|increase|boost)\b',
    r'\blast (week|month)\b',
)


def describe_problems(line, lead):
    """Why this generated line must not be used. Empty list = usable.

    Runs before the line is ever stored, because the storage IS the
    approval in this design — anything saved here goes to Instantly as a
    custom variable and out to a real person.
    """
    problems = []
    text = (line or '').strip()

    if not text:
        return ['Empty output.']
    if len(text) > MAX_CHARS:
        problems.append(
            f'{len(text)} chars — over the {MAX_CHARS} limit for one line.')
    if '\n\n' in text:
        problems.append('Contains a paragraph break; this is one line.')

    low = text.lower()
    for pattern in _META_PATTERNS:
        if re.search(pattern, low):
            problems.append(f'Reads as commentary, not copy (matched {pattern!r}).')
            break

    # A fabrication check is only meaningful against what we actually
    # know, so a claim is allowed if the matching observation fired.
    observed_keys = {key for key, _ in observations(lead)}
    for pattern in _FABRICATION_PATTERNS:
        if re.search(pattern, low):
            problems.append(
                f'Contains an unverifiable claim (matched {pattern!r}).')
            break

    # A year is only sayable if we actually hold it. "Practising since
    # 1998" is exactly the flattering detail a model invents, and the
    # recipient can disprove it in one second.
    years = re.findall(r'\b(?:19|20)\d{2}\b', text)
    if years:
        known = {str(y) for y in (lead.founded_year, lead.copyright_year)
                 if y}
        unknown = [y for y in years if y not in known]
        if unknown:
            problems.append(
                f'Cites year(s) {unknown} never recorded for this lead '
                f'(known: {sorted(known) or "none"}).')

    # The opener must not criticise. The email makes its offer further
    # down, where a problem arrives attached to a free fix; leading with
    # a defect just puts a stranger on the defensive. This is the whole
    # point of the rewrite, so it is enforced rather than requested.
    critique_markers = (
        'slow', 'outdated', 'out of date', 'dated', 'broken', 'stale',
        'not secure', 'insecure', 'unencrypted', 'not encrypted',
        'vulnerable', 'missing', 'lacks', 'lacking', 'poorly', 'weak',
        'struggling', 'losing', 'falling behind', 'bouncing',
        "isn't working", 'not working', 'problem with your',
        'issue with your', "doesn't have", 'no https', 'plain http',
    )
    for marker in critique_markers:
        if marker in low:
            problems.append(
                f'Opener criticises the prospect (matched {marker!r}). '
                f'Problems belong in the offer, not the first line.')
            break

    # Any number presented as a score must match a real measurement.
    scores = {lead.website_performance_score, lead.website_mobile_score,
              lead.website_seo_score, lead.google_review_count}
    for match in re.findall(r'\b(\d{1,3})\s*/\s*100\b', text):
        if int(match) not in {s for s in scores if s is not None}:
            problems.append(
                f'Cites a score of {match}/100 that was never measured.')
            break

    from outreach import copy_guard
    problems.extend(copy_guard.describe_pricing_problems(text))

    return problems


def generate(lead, save=True):
    """Write and store one icebreaker for this lead.

    Raises IcebreakerError when the model produces something unusable,
    leaving any previous value in place. A lead with no icebreaker is
    refused by ``instantly.push_leads``, so a failure here means the lead
    waits rather than going out as a mail merge.
    """
    from reporting import ai

    prompt = (
        f'{_facts_block(lead)}\n\n'
        'Write the opening line for a cold email to this business.'
    )

    try:
        raw = ai.claude_complete(
            messages=[{'role': 'user', 'content': prompt}],
            system=SYSTEM_PROMPT,
            model=ai.MODEL_CONTENT,
            max_tokens=MAX_TOKENS,
            thinking=ai.THINKING_OFF,
        )
    except Exception as exc:
        raise IcebreakerError(f'Claude call failed: {exc}') from exc

    line = (raw or '').strip().strip('"').strip()
    # Models occasionally prefix the line despite the instruction.
    line = re.sub(r'^(opening line|icebreaker)\s*:\s*', '', line,
                  flags=re.IGNORECASE).strip()

    problems = describe_problems(line, lead)
    if problems:
        raise IcebreakerError(
            'Generated line rejected: ' + ' '.join(problems))

    lead.icebreaker = line
    lead.icebreaker_generated_at = timezone.now()
    if save:
        lead.save(update_fields=[
            'icebreaker', 'icebreaker_generated_at', 'updated_at'])
    return line
