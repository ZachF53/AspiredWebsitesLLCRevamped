"""
Niche verification from the company's own homepage.

WHY THIS STAGE EXISTS
---------------------
Two problems with one cause, and one fix.

The provider's industry tag is wrong often enough to burn the domain. A
50-lead batch labelled "Accounting" contained a steel fabricator, an
insulation manufacturer, an insurance agency, a medical-billing company,
a software vendor, and a business whose own homepage says it has
permanently closed. Twelve percent. "Your accounting firm" sent to a
fabricator earns a spam complaint, and complaints follow the sending
domain into every future campaign.

The provider's company description is also useless for personalisation.
It is either absent or filler of the shape "X is an accounting company
based out of <street address>". Openers built from it come out generic,
or quote the street address back at the recipient, which reads like
surveillance rather than research.

The homepage settles both at once: it says what the business does, in
the words the business chose.

WHERE IT RUNS, AND WHY THERE
----------------------------
After import, BEFORE paid verification and before the icebreaker. The
ordering is a cost argument: an off-niche lead is worth zero
verification credits, and an unverified address is worth zero
writer-model calls. Fetching is free; this stage is the cheapest gate
available, so it goes first.

It does its own light fetch rather than waiting for ``enricher``, which
runs later and pulls raw markup for socials, TLS and PageSpeed. Two GETs
per lead a few seconds apart, both free. Not elegant; the alternative is
running the expensive stages against leads we already had the evidence
to drop.

EXTRACTION IS THE SMALL MODEL'S JOB. SELECTION IS THE WRITER'S.
---------------------------------------------------------------
Haiku is NOT asked which fact is most interesting — that is a judgement
call and the thing a small model is worst at. It is asked to extract
everything specific the page states, and the writer model picks. Moving
the judgement out is what makes the cheap model safe here.

FAIL CLOSED
-----------
Only ``confirmed`` proceeds. A wrongly held lead costs one lead; a
wrongly sent one costs the sending reputation of every future campaign.

But a failed fetch is NOT evidence of being off-niche. Real businesses
block scrapers, park domains and render client-side. Treating a timeout
as "not in the niche" silently deletes good leads, so an unreadable page
falls back to a keyword signal in the firm's own name or domain —
"Barrett CPA LLC" with a dead site is still obviously an accounting
firm — and only when the page AND the name say nothing does a lead
become ``unconfirmed``. Held for review, never deleted.
"""

import concurrent.futures
import json
import logging
import re

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


FETCH_TIMEOUT = 12
FETCH_WORKERS = 8
BATCH_SIZE = 6

# Below this much extracted text we cannot tell anything. A
# JavaScript-rendered site serves a nearly empty shell, which is an
# ordinary modern website, not a dead one.
MIN_USEFUL_CHARS = 120

# Plenty for classification, and about 600 tokens — the cost model for
# the whole stage rests on this being small.
MAX_PAGE_CHARS = 1800

SUMMARY_WORD_CAP = 50


# Name/domain keyword fallback, used ONLY when the page could not be
# read. Keyed by the business_type the campaign targets.
_NAME_SIGNALS = {
    'Law Firm': (
        'law', 'legal', 'attorney', 'attorneys', 'lawyer', 'lawyers',
        'counsel', 'advocate', 'esq', 'llp', 'barrister', 'injury',
        'defense', 'litigation',
    ),
    'Dentist': (
        'dental', 'dentist', 'dentistry', 'orthodont', 'perio', 'endodont',
        'smile', 'teeth', 'oral',
    ),
    'Accounting': (
        'cpa', 'accounting', 'accountant', 'bookkeep', 'tax', 'audit',
        'ledger',
    ),
    'Medical Practice': (
        'medical', 'clinic', 'health', 'physician', 'doctor', 'md',
        'care', 'wellness', 'chiropract', 'derma', 'ortho',
    ),
    'Financial Services': (
        'financial', 'wealth', 'advisor', 'advisors', 'capital',
        'investment', 'planning',
    ),
}

# Words that may follow a SHORT signal inside a domain label and still
# leave it meaning what it says: barrettlawgroup, smithlawoffices.
# Without this, "law" matches lawncare and lawson.
_DOMAIN_COMPOUNDS = (
    'firm', 'firms', 'group', 'office', 'offices', 'yer', 'yers',
    'associates', 'assoc', 'partners', 'pllc', 'llp', 'llc', 'pc',
    'center', 'centre', 'practice', 'care', 'clinic',
)

# "X is a Y company based out of Z." — the provider's auto-generated
# opening sentence. Stripped rather than discarding the whole field,
# because some records continue into real copy the business wrote.
_BOILERPLATE_RE = re.compile(
    r'^\s*.{0,120}?\bis an?\b.{0,80}?\bbased (?:out of|in)\b[^.]*\.\s*',
    re.IGNORECASE)

_PARKED_MARKERS = (
    'domain is for sale', 'buy this domain', 'domain for sale',
    'coming soon', 'under construction', 'account suspended',
    'site not found', 'default web site page', 'welcome to nginx',
    'apache2 ubuntu default page', 'iis windows server',
    'this site is temporarily unavailable', 'future home of',
    'parked domain', 'godaddy', 'squarespace domain',
)

_TAG_RE = re.compile(r'<(script|style|noscript)[^>]*>.*?</\1>',
                     re.IGNORECASE | re.DOTALL)
_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)
_META_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL)


def strip_provider_boilerplate(text):
    """Drop the provider's generated opening sentence, keep the rest.

    ``enricher``/``apify_source`` previously discarded the entire
    description when it contained "based out of". That threw away the
    records where real copy follows the filler.
    """
    if not text:
        return ''
    return _BOILERPLATE_RE.sub('', text.strip(), count=1).strip()


# ── Fetching ───────────────────────────────────────────────────────────

def _user_agent():
    """The UA the page fetch identifies as.

    Defaults to the project's honest self-identifying bot string. Many
    hosts 403 anything that is not a browser, so a browser UA reads more
    pages — that is a deliberate decision about how we present
    ourselves, not a tuning knob, so it lives in settings and defaults to
    honest.
    """
    return getattr(
        settings, 'OUTREACH_FETCH_USER_AGENT',
        'Mozilla/5.0 (compatible; AspiredWebsitesBot/1.0; '
        '+https://aspiredwebsites.com/bot)')


def extract_page_text(html):
    """Title + meta description + stripped body, truncated.

    Returns '' when there is nothing usable, which the caller must treat
    as "cannot tell" rather than as evidence of anything.
    """
    if not html:
        return ''
    lowered = html[:8000].lower()
    if any(marker in lowered for marker in _PARKED_MARKERS):
        return ''

    parts = []
    title = _TITLE_RE.search(html)
    if title:
        parts.append(re.sub(r'\s+', ' ', title.group(1)).strip())
    meta = _META_RE.search(html)
    if meta:
        parts.append(re.sub(r'\s+', ' ', meta.group(1)).strip())

    body = _TAG_RE.sub(' ', html)
    body = re.sub(r'<[^>]+>', ' ', body)
    body = re.sub(r'&[a-z]+;', ' ', body)
    body = re.sub(r'\s+', ' ', body).strip()
    parts.append(body)

    text = ' | '.join(p for p in parts if p)
    return text[:MAX_PAGE_CHARS]


def fetch_site_text(url, timeout=FETCH_TIMEOUT):
    """(text, note). Never raises — a fetch failure is an outcome."""
    if not url:
        return '', 'no website on record'
    if not url.startswith(('http://', 'https://')):
        url = f'https://{url}'
    try:
        resp = requests.get(
            url, timeout=timeout, allow_redirects=True,
            headers={'User-Agent': _user_agent(),
                     'Accept': 'text/html,application/xhtml+xml'},
        )
    except Exception as exc:  # noqa: BLE001
        return '', f'fetch failed: {type(exc).__name__}'

    if resp.status_code in (401, 403, 429):
        return '', f'blocked by the site (HTTP {resp.status_code})'
    if resp.status_code >= 400:
        return '', f'HTTP {resp.status_code}'

    text = extract_page_text(resp.text or '')
    if len(text) < MIN_USEFUL_CHARS:
        return '', 'page had no readable text (parked or JS-rendered)'
    return text, ''


def fetch_many(leads, workers=FETCH_WORKERS, timeout=FETCH_TIMEOUT):
    """{lead_pk: (text, note)} — fetched in parallel because they are
    independent and each one is mostly waiting."""
    out = {}
    if not leads:
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fetch_site_text, lead.website, timeout): lead.pk
            for lead in leads
        }
        for future in concurrent.futures.as_completed(futures):
            pk = futures[future]
            try:
                out[pk] = future.result()
            except Exception as exc:  # noqa: BLE001
                out[pk] = ('', f'fetch failed: {type(exc).__name__}')
    return out


# ── Name fallback ──────────────────────────────────────────────────────

def name_signal(lead, business_type):
    """Does the firm's own name or domain say what it is?

    Only consulted when the page could not be read. "Barrett CPA LLC"
    with a dead site is still obviously an accounting firm, and dropping
    it because a server timed out would be throwing away a good lead for
    an infrastructure reason.
    """
    words = _NAME_SIGNALS.get((business_type or '').strip())
    if not words:
        return ''
    haystack = f"{lead.firm_name or ''} {lead.website or ''}".lower()
    tokens = set(re.split(r'[^a-z0-9]+', haystack))
    for word in words:
        if word in tokens:
            return word

    # Domains have no word boundaries, so "smithlaw.com" needs a
    # substring match. A plain substring is unsafe for the SHORT signals
    # though, and "law" is both the shortest and the most important one:
    # lawncare.com, lawson.com and flawless.com would all confirm as law
    # firms, and a wrong confirmation is the direction that costs sending
    # reputation.
    #
    # So a short signal must sit at the end of the domain label
    # (smithlaw.com) or be followed by a business word
    # (barrettlawgroup.com). Longer signals — "dental", "attorney" — are
    # distinctive enough to match plainly.
    domain = re.sub(r'^https?://', '', (lead.website or '').lower())
    label = domain.split('/')[0].split('.')[0]
    for word in words:
        if len(word) >= 4:
            if word in domain:
                return word
            continue
        for match in re.finditer(re.escape(word), label):
            tail = label[match.end():]
            if not tail or tail.startswith(_DOMAIN_COMPOUNDS):
                return word
    return ''


# ── Classification ─────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You read business homepages and answer two questions per business.

For each one return:

  verdict — "confirmed", "rejected", or "unconfirmed"
  summary — what the page says this business does

VERDICT RULES
  confirmed    the page shows this IS the target kind of business.
               Adjacent counts: a bookkeeping-only shop is an
               accounting firm; a solo practitioner is a law firm.
  rejected     the page clearly describes a DIFFERENT kind of business,
               or says the business has closed. Be sure. Rejecting is
               how a real lead gets thrown away.
  unconfirmed  the page text is empty, generic, or says nothing about
               what they do.

NEVER guess from the company name. You are given the name only so you
know who the page belongs to. If the page text says nothing, the answer
is "unconfirmed" — not a guess in either direction.

SUMMARY RULES
Extract EVERYTHING specific the page states, up to 50 words, as plain
semicolon-separated clauses. Services, specialties, who they serve,
years in business, locations, team size, credentials, unusual
combinations of the above.

Do NOT pick the single most interesting fact. Do not write a sentence,
an opening line, or any commentary. Someone else chooses what to use.

A BLANK summary is correct and useful when the page says nothing
specific. An invented detail is far worse than a missing one — it is
instantly checkable, and being caught inventing ends the conversation
permanently.

Return ONLY a JSON array, one object per business, in the order given:
[{"id": <id>, "verdict": "...", "summary": "..."}]\
"""


def _build_user_message(items, business_type):
    lines = [f'Target kind of business: {business_type or "unspecified"}.',
             '', 'Businesses:']
    for item in items:
        lines.append('')
        lines.append(f"id: {item['id']}")
        lines.append(f"name: {item['name']}")
        lines.append(f"page text: {item['text']}")
    return '\n'.join(lines)


def classify_batch(items, business_type):
    """One model call for up to BATCH_SIZE pages.

    Batching is the cost lever: it amortises the system prompt across
    several leads. Prompt caching is not relied on — small models carry a
    high minimum cacheable prompt and this system prompt is well under
    it.

    Returns {id: {'verdict', 'summary'}}. On any failure returns {} and
    the caller holds those leads rather than assuming anything.
    """
    from reporting import ai

    if not items:
        return {}
    try:
        raw = ai.claude_complete(
            messages=[{'role': 'user',
                       'content': _build_user_message(items, business_type)}],
            system=SYSTEM_PROMPT,
            model=ai.MODEL_CHAT,
            max_tokens=1500,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('site_check: classification call failed')
        return {}

    text = (raw or '').strip()
    start, end = text.find('['), text.rfind(']')
    if start == -1 or end == -1:
        logger.warning('site_check: no JSON array in response: %r', text[:200])
        return {}
    try:
        rows = json.loads(text[start:end + 1])
    except ValueError:
        logger.warning('site_check: unparseable JSON: %r', text[:200])
        return {}

    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        verdict = str(row.get('verdict', '')).strip().lower()
        if verdict not in ('confirmed', 'rejected', 'unconfirmed'):
            # An unrecognised verdict is not a confirmation.
            verdict = 'unconfirmed'
        summary = ' '.join(str(row.get('summary') or '').split())
        words = summary.split()
        if len(words) > SUMMARY_WORD_CAP:
            summary = ' '.join(words[:SUMMARY_WORD_CAP])
        out[str(row.get('id'))] = {'verdict': verdict, 'summary': summary}
    return out


# ── The stage ──────────────────────────────────────────────────────────

def check_leads(leads, business_type=None, report=None):
    """Verify a batch of leads against their own homepages.

    Skips any lead that already carries a verdict, so a re-run costs
    nothing and a crashed run resumes instead of re-fetching and
    re-paying.
    """
    from outreach.models import Lead

    say = report or (lambda _line: None)
    leads = [lead for lead in leads
             if lead.niche_verdict == Lead.NICHE_PENDING]
    if not leads:
        return {'checked': 0, 'confirmed': 0, 'rejected': 0,
                'unconfirmed': 0}

    say(f'fetching {len(leads)} homepages')
    pages = fetch_many(leads)
    readable = sum(1 for text, _ in pages.values() if text)
    say(f'{readable}/{len(leads)} homepages readable')

    counts = {'checked': 0, 'confirmed': 0, 'rejected': 0, 'unconfirmed': 0}
    batch = []

    def flush(pending):
        if not pending:
            return
        wanted = business_type or (pending[0]['lead'].business_type or '')
        results = classify_batch(
            [{'id': str(p['lead'].pk), 'name': p['lead'].firm_name,
              'text': p['text']} for p in pending],
            wanted)
        for entry in pending:
            lead = entry['lead']
            got = results.get(str(lead.pk))
            if got is None:
                # The call failed or omitted this row. Hold it; do not
                # invent a verdict.
                _record(lead, Lead.NICHE_UNCONFIRMED, '',
                        'classification did not return a verdict')
                counts['unconfirmed'] += 1
            else:
                _record(lead, got['verdict'], got['summary'],
                        'read from the homepage')
                counts[got['verdict']] += 1
            counts['checked'] += 1

    for lead in leads:
        text, note = pages.get(lead.pk, ('', 'not fetched'))
        if text:
            batch.append({'lead': lead, 'text': text})
            if len(batch) >= BATCH_SIZE:
                flush(batch)
                batch = []
            continue

        # Unreadable page. Fall back to the name; only if that says
        # nothing too does the lead become unconfirmed.
        wanted = business_type or (lead.business_type or '')
        hit = name_signal(lead, wanted)
        if hit:
            _record(lead, Lead.NICHE_CONFIRMED, '',
                    f'{note}; name/domain says "{hit}"')
            counts['confirmed'] += 1
        else:
            _record(lead, Lead.NICHE_UNCONFIRMED, '',
                    f'{note}; name says nothing either')
            counts['unconfirmed'] += 1
        counts['checked'] += 1

    flush(batch)

    say(f"confirmed {counts['confirmed']}, rejected {counts['rejected']}, "
        f"unconfirmed {counts['unconfirmed']}")
    return counts


def _record(lead, verdict, summary, evidence):
    """Write the verdict, and hold anything not confirmed for review.

    Held rather than deleted, and flagged through the SAME
    needs_review queue the rest of the system already uses — push_leads
    already refuses a lead carrying it, so nothing new has to learn
    about this stage in order to be safe.
    """
    from outreach.models import Lead

    lead.niche_verdict = verdict
    lead.niche_evidence = evidence[:255]
    lead.niche_checked_at = timezone.now()
    fields = ['niche_verdict', 'niche_evidence', 'niche_checked_at',
              'updated_at']
    if summary:
        lead.site_summary = summary
        fields.append('site_summary')
    if verdict != Lead.NICHE_CONFIRMED:
        lead.needs_review = True
        lead.review_reason = f'niche {verdict}: {evidence}'[:255]
        fields += ['needs_review', 'review_reason']
    lead.save(update_fields=fields)
