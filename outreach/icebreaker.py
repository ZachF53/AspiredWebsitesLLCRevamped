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


SYSTEM_PROMPT = (
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

    # If the copy names a year, it has to be the one we measured.
    years = re.findall(r'\b(?:19|20)\d{2}\b', text)
    if years and 'stale_copyright' not in observed_keys:
        problems.append(
            'Cites a year but no copyright year was measured for this lead.')
    elif years and lead.copyright_year:
        if str(lead.copyright_year) not in years:
            problems.append(
                f'Cites {years} but the measured copyright year is '
                f'{lead.copyright_year}.')

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
