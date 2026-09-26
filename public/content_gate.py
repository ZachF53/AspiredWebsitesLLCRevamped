"""
The grep gate from the Sept 2026 implementation plan (§12.1).

Renders every public page and fails on wording that must never ship
again: discontinued tiers and services, legacy URLs, internal notes,
Texas-office residue, the old timeline, unqualified ownership claims,
proof overclaims, first-person voice outside the About page, and
placeholders. It checks RENDERED HTML, not template source, because most
of the risky copy lives in the database (case studies, articles, city
pages, pricing bullets), where a template grep never looks.

Used by `manage.py content_gate` (local render or a live --base-url) and
by public.tests_content_gate, so a regression fails the test suite.
"""

import html as html_lib
import re
from dataclasses import dataclass

# (label, regex, flags). Plain phrases are escaped; entries marked RAW
# are real regexes.
_PHRASES = {
    'A pricing/tiers': [
        'Essential Build', 'Premium Build', '$2,500', '$4,500',
        '2,500–4,500', '$299', '1,199', '50% deposit', '50% final',
        'billed annually', 'Annual Hosting', 'annual renewal',
        'post schedules', 'work tiers',
    ],
    'B discontinued services': [
        'Social Media', 'Social media', 'social media', 'Digital Marketing',
        'Digital marketing', 'Search Engine Optimization', 'Free SEO Audit',
        'Web Design & SEO', '(SEO)', 'local SEO', 'Local SEO', 'local-seo',
        'SEO work', 'separate ongoing discipline',
    ],
    'C legacy/internal': [
        '/services/seo', '/services/digital-marketing',
        'custom-web-development', '/for-law-firms', '/portfolio/other',
        'brand_fact_matrix', 'itemised', 'measurement window', 'FindLaw',
        'practice areas', 'Law Firm', 'law firm',
    ],
    'D Texas residue': [
        'San Antonio, Texas', 'San Antonio, TX · Atlanta',
        'Hand-coded in San Antonio', 'Based in Georgia',
    ],
    'E timeline/duration': [
        'Three To Four Weeks', 'Three to four weeks', 'three to four weeks',
        '3–4 weeks', 'takes 60 seconds', 'about a minute',
    ],
    'F ownership overclaims': [
        'Leave whenever and take every file', 'No monthly lock-in',
    ],
    'G proof overclaims': [
        'HVAC project write-ups are publishing shortly', 'Recent HVAC Builds',
        'Real HVAC Websites', 'publishing shortly',
        'Real projects, real screenshots', 'That’s Ok', "That's Ok",
        'limited number of projects', 'who we build for now',
        'willing to help out any small business',
    ],
    'I consistency': [
        'AI Agents', 'not stored unless', 'Included in every plan.',
    ],
    'J placeholders': ['TODO', 'Lorem', 'OWNER-INPUT', '[ZACH]'],
}

_RAW = {
    'G proof overclaims': [r'(?i)\bcoming soon\b'],
    'J placeholders': [r'\[[A-Z][A-Z _-]{1,38}\]'],
}

# First-person voice (plan §12.1 H). Checked on <main> text only, after
# removing quotes, testimonials, form labels/options and bylines.
# "I(?!-\d)" so an interstate ("I-75") isn't read as first person.
_VOICE = re.compile(r"\b(I(?!-\d)|I've|I’ve|I'm|I’m|I'll|I’ll|I'd|I’d|my|me)\b")

# path prefix -> set of labels (or specific phrases) allowed there.
_WHITELIST = {
    '/portfolio/denis-law-group/': {'law firm', 'Law Firm'},
    '/insights/how-much-does-law-firm-web-design-cost/': {
        'law firm', 'Law Firm', 'practice areas', 'VOICE', 'A pricing/tiers'},
    '/locations/san-antonio/': {'San Antonio, Texas'},
    '/about/': {'VOICE'},
}
# Allowed anywhere: the Denis Law Group card pill ("Law Firm · San
# Antonio, TX") is a true business-type label on real client work.
_GLOBAL_ALLOW = [re.compile(r'Law Firm\s*(?:&middot;|·)')]


@dataclass
class Hit:
    path: str
    label: str
    phrase: str
    context: str

    def __str__(self):
        return f'{self.path}  [{self.label}]  {self.phrase!r}  …{self.context}…'


def _strip_comments(markup):
    return re.sub(r'<!--.*?-->', '', markup, flags=re.S)


def _visible_text(markup):
    markup = re.sub(r'<(script|style)\b.*?</\1>', ' ', markup, flags=re.S | re.I)
    markup = re.sub(r'<[^>]+>', ' ', markup)
    return html_lib.unescape(re.sub(r'\s+', ' ', markup))


def _main_prose(markup):
    m = re.search(r'<main\b.*?</main>', markup, flags=re.S | re.I)
    body = m.group(0) if m else markup
    # Also questions (FAQ summaries/h3s are asked in the customer's voice:
    # "When am I charged?") and button text ("Call me back").
    for tag in ('blockquote', 'figcaption', 'label', 'option', 'select',
                'script', 'style', 'noscript', 'summary', 'h3', 'button'):
        body = re.sub(rf'<{tag}\b.*?</{tag}>', ' ', body, flags=re.S | re.I)
    # Testimonial and byline blocks are someone else's words, or the
    # author's name, not site voice.
    body = re.sub(r'<(p|div)[^>]*class="[^"]*(testimonial__quote|hero__meta|'
                  r'trust-block__quote|article-body)[^"]*"[^>]*>.*?</\1>',
                  ' ', body, flags=re.S | re.I)
    text = _visible_text(body)
    # Quoted speech is the customer's words, not the site's voice.
    text = re.sub(r'"[^"]{0,120}"|“[^”]{0,120}”', ' ', text)
    return text.replace('Call me back', ' ')


def _allowed(path, label, phrase):
    for prefix, allowed in _WHITELIST.items():
        if path.startswith(prefix) and (label in allowed or phrase in allowed):
            return True
    return False


def scan(path, markup):
    """Return the list of Hits for one rendered page."""
    hits = []
    raw = _strip_comments(markup)
    text = _visible_text(raw)
    haystacks = (raw, html_lib.unescape(raw))

    def ctx(src, start, end):
        return re.sub(r'\s+', ' ', src[max(0, start - 50):end + 50])

    for label, phrases in _PHRASES.items():
        for phrase in phrases:
            for hay in haystacks:
                idx = hay.find(phrase)
                if idx == -1:
                    continue
                window = hay[max(0, idx - 20):idx + len(phrase) + 20]
                if any(rx.search(window) for rx in _GLOBAL_ALLOW):
                    continue
                if not _allowed(path, label, phrase):
                    hits.append(Hit(path, label, phrase, ctx(hay, idx, idx + len(phrase))))
                break
    for label, patterns in _RAW.items():
        for pattern in patterns:
            m = re.search(pattern, text)
            if m and not _allowed(path, label, m.group(0)):
                hits.append(Hit(path, label, m.group(0), ctx(text, m.start(), m.end())))

    if not _allowed(path, 'VOICE', 'VOICE'):
        prose = _main_prose(raw)
        m = _VOICE.search(prose)
        if m:
            hits.append(Hit(path, 'H voice', m.group(0), ctx(prose, m.start(), m.end())))
    return hits


# Pages that are public but not in the sitemap (noindexed or secondary).
EXTRA_PATHS = [
    '/design/schedule/', '/contact/', '/login/', '/password-reset/',
    '/insights/how-much-does-law-firm-web-design-cost/',
    '/services/hosting-maintenance/sample-report/',
]


def sitemap_paths():
    """Every URL the sitemap would list, as paths."""
    from urllib.parse import urlparse
    from public.sitemaps import SITEMAPS
    paths = []
    for sitemap_cls in SITEMAPS.values():
        sm = sitemap_cls()
        for item in sm.items():
            loc = sm.location(item)
            paths.append(urlparse(loc).path or loc)
    return paths


def all_paths():
    seen, out = set(), []
    for p in sitemap_paths() + EXTRA_PATHS:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out
