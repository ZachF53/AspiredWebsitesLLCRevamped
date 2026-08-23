"""
Flag leads that do not look like what the source claims they are.

WHY THIS IS NOT A FILTER
------------------------
Apollo mis-tags. Verified against the live dataset 2026-08-23, every one
of these came back as ``industry='Legal Services'``:

    Bwa Video, Inc.                      title='Owner'
    Kinney Recruiting                    title='Co-owner'
    Patent Designs                       title='Owner'
    National Employment Lawyers Assoc.   title='Owner'

Two of them are not law firms at all, and the fourth is a bar
association rather than a practice. No actor-side filter can catch them:
``company_not_industry: ["staffing & recruiting"]`` cannot exclude a
recruiting company that the source labels "Legal Services". A tightened
run proved it -- 100 rows in, 100 identical rows out, zero excluded.

So this runs on our side, on the one field the source cannot mislabel:
the company's own name.

WHY REVIEW RATHER THAN BLOCK
----------------------------
The signal is a heuristic on a name, and names are ambiguous. "Legal
Solutions PLLC" is a real firm; "Patent Designs" might be a design
studio or a patent practice. Auto-blocking on a guess silently discards
real prospects and nobody ever finds out. A flag costs one glance and
fails in the recoverable direction.

Roughly 4 rows per 100 on the current data -- small enough that manual
review is cheap, large enough that emailing a bar association would be
noticed.
"""

import re

# ── Markers ────────────────────────────────────────────────────────────

# A membership body, school, or public institution. These are never a
# law FIRM even when the name is full of legal words -- "National
# Employment Lawyers Association" is the case that forced the tier split.
STRONG_MARKERS = (
    'association', 'society', 'institute', 'foundation', 'council',
    'chamber', 'coalition', 'academy', 'university', 'college',
    'school', 'nonprofit', 'ministries', 'church', 'alliance',
    'federation', 'union', 'committee',
)

# Another line of business. Only meaningful when the name carries NO
# law-practice marker, because plenty of genuine firms are called
# "... Legal Solutions" or "... Law Consulting".
WEAK_MARKERS = (
    'recruiting', 'recruiter', 'recruiters', 'recruitment', 'staffing',
    'talent', 'headhunter', 'placement',
    'video', 'media', 'marketing', 'advertising', 'studio', 'studios',
    'design', 'designs', 'creative', 'branding', 'photography',
    'software', 'technology', 'technologies', 'systems', 'digital',
    'analytics', 'labs', 'apps',
    'insurance', 'realty', 'real estate', 'mortgage', 'lending',
    'bank', 'banking', 'capital', 'ventures', 'equity', 'holdings',
    'publishing', 'printing', 'logistics', 'staffing', 'janitorial',
    'construction', 'roofing', 'plumbing', 'landscaping', 'restaurant',
    'salon', 'fitness', 'automotive', 'dealership',
)

# Evidence the business really is a law practice.
LAW_MARKERS = (
    'law', 'laws', 'legal', 'attorney', 'attorneys', 'lawyer',
    'lawyers', 'counsel', 'counselors', 'esq', 'llp', 'pllc', 'plc',
    'litigation', 'advocates', 'advocacy', 'barrister', 'solicitor',
    'defense', 'injury', 'justice',
)

_WORD_SPLIT = re.compile(r"[^a-z0-9']+")


def _words(name):
    """Lowercased word set, punctuation stripped.

    Word-level matching, never substring: 'pc' appears inside 'pacific'
    and 'design' inside 'designated', and either would fire constantly
    on a substring rule.
    """
    return {w for w in _WORD_SPLIT.split((name or '').lower()) if w}


def _has(words, markers):
    """Whether any marker matches. Multi-word markers are checked as a
    phrase against the rejoined name."""
    joined = ' '.join(sorted(words))
    for marker in markers:
        if ' ' in marker:
            if marker in joined:
                return marker
        elif marker in words:
            return marker
    return ''


def describe_review_reasons(lead):
    """Why a human should look at this lead. Empty list = it looks fine.

    Only examines the company name. Industry and job title both come
    from the source that got it wrong in the first place, so neither is
    evidence here.
    """
    reasons = []
    name = (lead.firm_name or '').strip()
    if not name:
        return ['No company name.']

    words = _words(name)
    # Multi-word markers need the raw name, not the sorted word set.
    raw = ' '.join(_WORD_SPLIT.split(name.lower())).strip()

    strong = ''
    for marker in STRONG_MARKERS:
        if marker in words:
            strong = marker
            break
    if strong:
        reasons.append(
            f'Name contains "{strong}" - looks like a membership body or '
            f'institution rather than a practice.')
        return reasons

    law = _has(words, LAW_MARKERS)
    if law:
        # Carries real law-practice signal; a weak marker alongside it is
        # almost always a practice area or a stylistic name.
        return reasons

    weak = ''
    for marker in WEAK_MARKERS:
        if (' ' in marker and marker in raw) or marker in words:
            weak = marker
            break
    if weak:
        reasons.append(
            f'Name contains "{weak}" and no law-practice wording - may be '
            f'a different line of business.')

    return reasons


def flag_lead(lead, save=True):
    """Set or clear the review flag on one lead. Returns True if flagged."""
    reasons = describe_review_reasons(lead)
    lead.needs_review = bool(reasons)
    lead.review_reason = ' '.join(reasons)[:255]
    if save:
        lead.save(update_fields=['needs_review', 'review_reason',
                                 'updated_at'])
    return lead.needs_review
