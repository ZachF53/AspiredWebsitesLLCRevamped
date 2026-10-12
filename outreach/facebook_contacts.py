"""
Facebook -> email, for leads that have no website.

THE PROBLEM THIS SOLVES
-----------------------
``apify_maps`` sources businesses with no website. Those leads arrive
with a phone and nothing else, and the conclusion drawn at first was
that they simply could not be emailed: Maps returns no email, the Maps
actor's own lead enrichment requires a website, and ``enricher`` finds
email by scraping a homepage that does not exist.

That was wrong, and only half the search. A sole operator with no
website very often still has a **Facebook business page**, and that page
carries a contact email. Measured 2026-10-10 on the San Antonio /
Austin HVAC batch:

    facebook page found (Brave, 1 query/lead)   60 / 148
    page carried an email                       41
    survived verification                       38

38 addresses for $0.61 of Apify spend, from a segment written off as
unreachable.

THE CHAIN
---------
    1. find_facebook_pages()  Brave, free, 1 query per lead
    2. fetch_page_contacts()  apify/facebook-page-contact-information,
                              $0.01 per page
    3. apply_contacts()       verify, then store

Step 2 is cheap and reliable (75% of pages yield an email). Step 1 is
the bottleneck at ~40%, and it is also where every accuracy problem
lives, because a search for a small business's name returns a great deal
that is not that business.

WHAT GOES WRONG, AND WHY EACH GUARD EXISTS
------------------------------------------
Every one of these was observed on the real batch:

  * An aggregator's OWN page. Their page quotes the business's name and
    phone, so ``_matches_business`` confirms it. Caught by
    ``_social_matches_name`` -- the HANDLE has to carry the name, and
    ``facebook.com/mykukun`` does not.
  * Not a page at all: ``/public/`` and ``/people/`` are Facebook's
    people-directory, ``/photos/`` and ``/posts/`` are single posts. One
    lead was matched to a personal photo album. See NON_PAGE_MARKERS.
  * Wildly wrong businesses that still share a token: "Texas Heat A/C"
    matched a cheerleading page, "Allstar Air Duct Cleaning" matched
    "Allstar Floor Cleaning".
  * TWO leads assigned the SAME page. Two businesses cannot share one
    page, so at least one is wrong -- both are dropped rather than
    guessing. (The actor also rejects duplicate inputs outright.)
  * A genuine national/international name collision. "Austin Heating &
    Cooling" matched a page for a real business of that name in
    **Michigan**; "Air Force Solutions" matched a US Air Force
    ``@us.af.mil`` address; "Thermacool" matched a UK ``.co.uk``
    company. Name tokens alone cannot separate these.

So storage is tiered rather than boolean:

    phone match   -> store, confident
    name match    -> store, but flag needs_review
    foreign/fed   -> reject outright

An email is a one-shot resource. Sending to the wrong business burns the
address and the sender's reputation, and a recipient who spots that you
have them confused with someone else does not reply. When the evidence
is a name and nothing more, a human looks before it sends.
"""

import json
import logging
import re
import time
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


APIFY_BASE = 'https://api.apify.com/v2'
FB_CONTACT_ACTOR = 'apify~facebook-page-contact-information'
# The actor validates this against a strict locale enum -- plain 'en' is
# rejected with HTTP 400 invalid-input.
FB_ACTOR_LANGUAGE = 'en-US'
RUN_TIMEOUT_SECS = 900
# Tier BRONZE, read off the live actor 2026-10-10.
COST_PER_PAGE_USD = Decimal('0.01')
COST_PER_RUN_START_USD = Decimal('0.001')
# Largest input list the actor handles without silently dropping pages.
# 60 was observed working in full; 194 returned 54 and ignored the rest.
FB_MAX_PAGES_PER_RUN = 50

# URL shapes that are never a business's own page. '/p/' IS the modern
# business-page format (facebook.com/p/Name-123456) and is kept.
NON_PAGE_MARKERS = (
    '/public/', '/people/', '/photos/', '/posts/', '/story.php',
    '/permalink', '/media/', '/videos/', '/groups/', '/events/',
    '/watch', '/marketplace/', '/hashtag/', '/sharer', '/plugins',
    '/login', '/search',
)

# A Texas HVAC owner-operator does not run a UK or Australian domain,
# and is not reachable at a federal address. These appear through
# national name collisions, not through bad matching.
FOREIGN_OR_FEDERAL_TLD = re.compile(
    r'\.(co\.uk|com\.au|co\.nz|co\.za|mil|gov|ar|ie|de|fr|es|mx)$', re.I)

# A page title naming a different state is positive evidence against,
# the same logic as the phone-mismatch rule in enricher._matches_business.
OTHER_STATE = re.compile(
    r'\b(michigan|florida|ohio|georgia|california|arizona|colorado|nevada|'
    r'oregon|virginia|carolina|tennessee|alabama|oklahoma|kansas|iowa|'
    r'illinois|indiana|missouri|minnesota|wisconsin|new york|new jersey)\b',
    re.I)


class FacebookContactError(Exception):
    """User-facing failure -- the message is shown in the admin."""


# ── Step 1: find the page ──────────────────────────────────────────────

def is_business_page_url(url):
    """Whether a facebook URL could be a business's own page."""
    from outreach.enricher import _base_domain, _domain_of

    if _base_domain(_domain_of(url or '')) != 'facebook.com':
        return False
    return not any(m in (url or '') for m in NON_PAGE_MARKERS)


def find_facebook_pages(leads, log=None):
    """Brave-search each lead for its Facebook page. Free.

    Only fills leads that have none. Returns ``(found, checked)``.

    A URL already assigned to a different lead is refused for BOTH --
    two businesses cannot share one page, so a collision means at least
    one assignment is wrong and there is no evidence saying which.
    """
    from outreach.enricher import (_brave_search, _matches_business,
                                   _social_matches_name,
                                   BRAVE_INTER_QUERY_DELAY)
    from outreach.models import Lead

    say = log or (lambda m: None)
    api_key = getattr(settings, 'BRAVE_SEARCH_API_KEY', '')
    if not api_key:
        raise FacebookContactError(
            'BRAVE_SEARCH_API_KEY is not set, so pages cannot be found.')

    taken = set(
        Lead.objects.exclude(facebook_url='')
        .values_list('facebook_url', flat=True))

    found = checked = 0
    # URLs assigned during THIS run, so a later collision can undo the
    # earlier assignment's contribution to the count. Without it the
    # command reports finding a page it went on to clear.
    assigned_here = set()
    for lead in leads:
        if lead.facebook_url:
            continue
        checked += 1
        query = f'{lead.firm_name} {lead.city} facebook'.strip()
        try:
            results = _brave_search(query, api_key)
        except Exception:  # noqa: BLE001
            logger.exception('Brave search failed for %s', lead.firm_name)
            continue
        time.sleep(BRAVE_INTER_QUERY_DELAY)

        for hit in results:
            url = hit.get('url', '')
            if not is_business_page_url(url):
                continue
            ok, why = _matches_business(hit, lead)
            if not ok:
                continue
            named, name_why = _social_matches_name(url, lead.firm_name)
            if not named:
                say(f'    rejected {url[:52]}: {name_why}')
                continue
            if url in taken:
                # Collision. Clear the other side too; neither is trusted.
                Lead.objects.filter(facebook_url=url).update(facebook_url='')
                taken.discard(url)
                if url in assigned_here:
                    # We assigned it earlier in this same run, so undo
                    # its contribution rather than reporting a find we
                    # have just thrown away.
                    assigned_here.discard(url)
                    found -= 1
                say(f'    AMBIGUOUS {url[:48]} claimed by 2 leads - '
                    f'cleared both')
                break
            lead.facebook_url = url
            lead.save(update_fields=['facebook_url'])
            taken.add(url)
            assigned_here.add(url)
            found += 1
            say(f'  {lead.firm_name[:30]:32} -> {url[:54]} ({why})')
            break
    return found, checked


# ── Step 2: read the page ──────────────────────────────────────────────

def fetch_page_contacts(urls, timeout_secs=RUN_TIMEOUT_SECS):
    """Run the Apify actor over ``urls``. Returns the dataset items.

    Writes an ``ApifyRun`` ledger row so these pages count against the
    same monthly Apify budget as everything else -- it is one bill.
    """
    from outreach.models import ApifyRun

    token = getattr(settings, 'APIFY_TOKEN', '')
    if not token:
        raise FacebookContactError(
            'APIFY_TOKEN is not set. Add it to .env and restart.')

    # The actor rejects duplicate inputs with HTTP 400, and a duplicate
    # here would mean paying twice for one page regardless.
    urls = list(dict.fromkeys(u for u in urls if u))
    if not urls:
        return [], None

    # The actor SILENTLY TRUNCATES a large input list. Measured
    # 2026-10-12: 194 pages submitted, run finished in 11 seconds, 54
    # rows returned and no error anywhere -- it simply ignored 140
    # pages. A 60-page run the same day returned 60/60.
    #
    # Nothing in the response distinguishes "this page had no contact
    # info" from "this page was never looked at", so a silent truncation
    # reads downstream as a poor hit rate rather than as a failure. Hence
    # batching, and hence the caller comparing rows-returned against
    # pages-submitted.
    if len(urls) > FB_MAX_PAGES_PER_RUN:
        all_items, last = [], None
        for i in range(0, len(urls), FB_MAX_PAGES_PER_RUN):
            chunk = urls[i:i + FB_MAX_PAGES_PER_RUN]
            items, last = fetch_page_contacts(chunk, timeout_secs)
            all_items.extend(items)
            logger.info('facebook contacts batch %s-%s: %s row(s)',
                        i, i + len(chunk), len(items))
        return all_items, last

    estimate = COST_PER_RUN_START_USD + (COST_PER_PAGE_USD * len(urls))
    ledger = ApifyRun.objects.create(
        actor_id=FB_CONTACT_ACTOR,
        label=f'facebook contacts x{len(urls)}',
        search_input={'pages': urls[:50], 'language': FB_ACTOR_LANGUAGE},
        results_requested=len(urls),
        estimated_cost_usd=estimate,
    )

    try:
        run = _start_and_wait(token, urls, timeout_secs)
        items = _fetch_dataset(token, run.get('defaultDatasetId', ''))
    except Exception as exc:  # noqa: BLE001
        ledger.status = 'failed'
        ledger.error = str(exc)[:2000]
        ledger.finished_at = timezone.now()
        ledger.save(update_fields=['status', 'error', 'finished_at'])
        raise FacebookContactError(f'Facebook contact run failed: {exc}')

    ledger.status = 'succeeded'
    ledger.apify_run_id = run.get('id', '')
    ledger.dataset_id = run.get('defaultDatasetId', '')
    ledger.results_returned = len(items)
    ledger.finished_at = timezone.now()
    actual = run.get('usageTotalUsd')
    if isinstance(actual, (int, float)):
        # Provisional -- settles after the run. reconcile_apify_spend
        # trues it up.
        ledger.actual_cost_usd = Decimal(str(round(float(actual), 4)))
    ledger.save()
    return items, ledger


def item_page_url(item):
    """The page URL an actor row refers to.

    The actor emits TWO row shapes and this is the only thing joining a
    result back to a lead:

        success -> {'pageUrl': ..., 'facebookUrl': ..., 'url': None}
        failure -> {'url': ..., 'error': ..., everything else None}

    Reading only ``url`` therefore matches the FAILURES and silently
    drops every success. That cost a full run's results once.
    """
    return (item.get('pageUrl') or item.get('facebookUrl')
            or item.get('url') or '')


def normalise_page_url(url):
    """Canonical form for joining -- the actor rewrites m. and slashes."""
    return (url or '').replace('https://m.', 'https://www.').rstrip(
        '/').lower()


# ── Step 3: verify, then store ─────────────────────────────────────────

def classify_contact(item, lead):
    """How much to trust this page's email for this lead.

    Returns ``(verdict, email, reason)`` where verdict is one of
    ``'confident'``, ``'review'``, ``'reject'`` or ``'none'``.
    """
    from outreach.enricher import _name_tokens, _phone_digits

    email = (item.get('email') or '').strip().lower()
    if not email:
        return 'none', '', 'page carries no email'

    if FOREIGN_OR_FEDERAL_TLD.search(email):
        return 'reject', email, f'{email} is a foreign or federal domain'

    title = item.get('title') or ''

    page_phone = item.get('phone') or ''
    if lead.phone and page_phone:
        if _phone_digits(lead.phone) == _phone_digits(page_phone):
            return 'confident', email, 'page phone matches the lead phone'
        # Both numbers are known and they differ. That is positive
        # evidence AGAINST, not merely absent evidence -- the same rule
        # enricher._matches_business applies to search hits.
        #
        # It is what separates a trade-word coincidence from a real
        # match: "Texas Heat A/C" scores a name hit against "South TX
        # Heat Football Cheer" purely on the token 'heat', and only the
        # phone says they are different organisations.
        return 'reject', email, (
            f'page phone {page_phone} differs from the lead phone '
            f'{lead.phone}')

    blob = re.sub(r'[^a-z0-9]', '', title.lower())
    matched = [t for t in _name_tokens(lead.firm_name)
               if len(t) >= 4 and t in blob]
    if not matched:
        return 'reject', email, f'page title {title[:40]!r} does not match'

    if OTHER_STATE.search(title):
        return 'reject', email, (
            f'page title {title[:40]!r} names another state')

    return 'review', email, (
        f'name match only on {matched[0]!r}; page is {title[:40]!r}')


def apply_contacts(items, leads):
    """Store verified emails. Returns a counts dict."""
    by_url = {normalise_page_url(l.facebook_url): l
              for l in leads if l.facebook_url}
    stats = {'confident': 0, 'review': 0, 'reject': 0,
             'none': 0, 'error': 0, 'unmatched': 0}

    for item in items:
        if item.get('error'):
            stats['error'] += 1
            continue
        lead = by_url.get(normalise_page_url(item_page_url(item)))
        if lead is None:
            stats['unmatched'] += 1
            continue
        verdict, email, reason = classify_contact(item, lead)
        stats[verdict] += 1
        if verdict == 'confident':
            lead.email = email
            lead.save(update_fields=['email'])
        elif verdict == 'review':
            lead.email = email
            lead.needs_review = True
            lead.review_reason = f'Email from Facebook: {reason}'[:255]
            lead.save(update_fields=['email', 'needs_review',
                                     'review_reason'])
        elif verdict == 'reject':
            logger.info('FB email rejected for %s: %s',
                        lead.firm_name, reason)
    return stats


# ── HTTP ───────────────────────────────────────────────────────────────

def _start_and_wait(token, urls, timeout_secs):
    deadline = time.monotonic() + timeout_secs
    resp = requests.post(
        f'{APIFY_BASE}/acts/{FB_CONTACT_ACTOR}/runs',
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type': 'application/json'},
        params={'timeout': int(timeout_secs),
                'maxTotalChargeUsd': settings.APIFY_MAX_TOTAL_CHARGE_USD,
                'waitForFinish': 60},
        data=json.dumps({'pages': urls, 'language': FB_ACTOR_LANGUAGE}),
        timeout=90,
    )
    if resp.status_code >= 400:
        raise FacebookContactError(
            f'Apify HTTP {resp.status_code}: {resp.text[:300]}')
    data = (resp.json() or {}).get('data', {})
    run_id = data.get('id')
    while data.get('status') in ('READY', 'RUNNING') and run_id:
        if time.monotonic() > deadline:
            raise FacebookContactError(
                f'Apify run {run_id} still {data.get("status")} after '
                f'{timeout_secs}s.')
        poll = requests.get(
            f'{APIFY_BASE}/actor-runs/{run_id}',
            headers={'Authorization': f'Bearer {token}'},
            params={'waitForFinish': 60}, timeout=90)
        if poll.status_code >= 400:
            raise FacebookContactError(
                f'Apify poll HTTP {poll.status_code}: {poll.text[:200]}')
        data = (poll.json() or {}).get('data', {})
    if data.get('status') != 'SUCCEEDED':
        raise FacebookContactError(
            f'Apify run finished as {data.get("status")}')
    return data


def _fetch_dataset(token, dataset_id):
    if not dataset_id:
        return []
    resp = requests.get(
        f'{APIFY_BASE}/datasets/{dataset_id}/items',
        headers={'Authorization': f'Bearer {token}'},
        params={'clean': 'true'}, timeout=180)
    if resp.status_code >= 400:
        raise FacebookContactError(
            f'Apify dataset HTTP {resp.status_code}: {resp.text[:200]}')
    items = resp.json()
    return items if isinstance(items, list) else []
