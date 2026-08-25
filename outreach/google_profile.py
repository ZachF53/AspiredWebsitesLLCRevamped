"""
Give an Apify lead something true to say — the Places join.

THE PROBLEM THIS SOLVES
-----------------------
Measured on 83 real drafts: only 8 openers (10%) contained a hard fact.
The other 75 were a template with the city and firm name substituted in
-- "I've been reaching out to law firms in Dallas, and X caught my
attention." True, harmless, and not a reason for anyone to reply.

The cause was a clean split in the data, not a flaw in the writing:

    Google Maps leads   6 of 6  had a rating + review count
    Apify leads         0 of 75 had any Google data at all

The source that supplies contactable emails supplies nothing to say, and
the source that supplies something to say returns almost no usable
addresses. So this module joins them: take a lead we already know is
worth contacting, ask Places about that one firm, and copy the rating and
review count across.

WHY ONLY QUALIFIED LEADS
------------------------
Each lookup is a paid API call. Spending one on a lead that is unverified,
role-addressed, held for review, or inbound is spending money to decorate
an email that will never be sent. ``qualified_leads()`` is deliberately
stricter than "has an email".

WHY THE MATCHING IS PARANOID
----------------------------
This is the difference between this module and ordinary enrichment. What
it writes ends up in a sentence like:

    "I noticed Gamez Law Firm has built up 1,640 Google reviews at a
     4.8-star average..."

Attach that to the wrong business and we have told a stranger a confident,
checkable falsehood in the first line of a cold email. That is worse than
the generic opener it replaces -- a bland opener gets ignored, a wrong one
gets us reported.

So a match must be POSITIVELY established, never inferred from the absence
of contradiction:

    1. Website domain     same registered domain  -> certain
    2. Phone              same last 10 digits     -> near certain
    3. Name AND place     every distinctive token
                          present, plus the city  -> good
    4. anything else                              -> REJECTED

Test 3 requires the city as well as the name because "Smith Law" matches a
firm of that name in every state. A near-miss returns None and records why
in ``google_profile_note``; it never writes a rating it is unsure of.

Rejecting on uncertainty means some real firms get no rating. That is the
correct trade: the cost of a miss is a generic opener, the cost of a bad
match is a false statement to a prospect.
"""

import datetime
import logging
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.utils import timezone

from outreach.enricher import _name_tokens, _phone_digits

logger = logging.getLogger(__name__)

PLACES_SEARCH_URL = 'https://places.googleapis.com/v1/places:searchText'

# Only what the join needs. Every extra field is billable surface on some
# Places SKUs, and none of the rest is used here.
FIELD_MASK = (
    'places.id,places.displayName,places.formattedAddress,'
    'places.rating,places.userRatingCount,places.businessStatus,'
    'places.nationalPhoneNumber,places.websiteUri'
)

REQUEST_TIMEOUT = 20
NAME_SIMILARITY_FLOOR = 0.80  # stricter than the enricher's 0.65

# A listing with a handful of reviews is not an opener. "4.0 stars across
# 3 reviews" reads as thin rather than impressive, and the whole point is
# to open with something the firm would be pleased to hear.
MIN_REVIEWS_WORTH_CITING = 10


class PlacesError(Exception):
    """Lookup could not be performed — key missing, API down, quota."""


def qualified_leads():
    """Leads worth spending a paid lookup on.

    Stricter than the assignment gate on purpose. Assignment asks "can
    this be sent?"; this asks "is it worth paying to improve?" -- so it
    also skips anything already carrying a usable rating.
    """
    from outreach.models import Lead
    from outreach import verify

    qs = (Lead.objects
          .filter(google_profile_checked_at__isnull=True,
                  unsubscribed=False,
                  needs_review=False)
          .exclude(email='')
          .exclude(source__in=Lead.INBOUND_SOURCES)
          .order_by('-score', '-created_at'))

    return [l for l in qs
            if verify.is_sendable(l.email_verification_status)
            and not (l.google_review_count or 0) >= MIN_REVIEWS_WORTH_CITING]


def lookups_today():
    from outreach.models import Lead
    now = timezone.now().astimezone(timezone.get_current_timezone())
    start = timezone.make_aware(
        datetime.datetime.combine(now.date(), datetime.time.min),
        timezone.get_current_timezone())
    return Lead.objects.filter(
        google_profile_checked_at__gte=start,
        google_profile_checked_at__lt=start + datetime.timedelta(days=1),
    ).count()


def check_allowed():
    """(allowed, reason) for the daily Places budget."""
    from outreach.models import OutreachSettings

    cap = int(OutreachSettings.load().places_max_lookups_per_day or 0)
    if cap <= 0:
        return False, ('Google Places lookups are disabled '
                       '(places_max_lookups_per_day is 0).')
    used = lookups_today()
    if used >= cap:
        return False, (f'Places lookup cap reached: {used} of {cap} used '
                       f'today.')
    return True, ''


# ── Matching ──────────────────────────────────────────────────────────

def _registered_domain(url):
    try:
        host = (urlparse(url).hostname or '').lower()
    except Exception:  # noqa: BLE001
        return ''
    if host.startswith('www.'):
        host = host[4:]
    parts = host.split('.')
    return '.'.join(parts[-2:]) if len(parts) >= 2 else host


def match_reason(place, lead):
    """Why this Places record is (or is not) this lead. '' means no match.

    Returns (matched: bool, reason: str). The reason is stored so a bad
    match can be diagnosed from the row rather than guessed at.
    """
    place_name = ((place.get('displayName') or {}).get('text') or '').strip()
    address = (place.get('formattedAddress') or '')

    # 1. Website — the strongest signal available, and the one Apify
    #    leads almost always have.
    lead_domain = _registered_domain(lead.website)
    place_domain = _registered_domain(place.get('websiteUri') or '')
    if lead_domain and place_domain:
        if lead_domain == place_domain:
            return True, f'website domain {lead_domain}'
        # Two different real domains is positive evidence AGAINST, not
        # merely missing evidence for.
        return False, (f'website mismatch ({lead_domain} vs '
                       f'{place_domain})')

    # 2. Phone.
    lead_phone = _phone_digits(lead.phone)
    place_phone = _phone_digits(place.get('nationalPhoneNumber') or '')
    if lead_phone and place_phone:
        if lead_phone == place_phone:
            return True, 'phone match'
        return False, 'phone mismatch'

    # 3. Name AND place. The city is required because a firm name alone
    #    matches a same-named practice in another state.
    tokens = _name_tokens(lead.firm_name)
    place_tokens = _name_tokens(place_name)
    if not tokens or not place_tokens:
        return False, 'name too generic to match safely'

    city = (lead.city or '').strip().lower()
    if not city or city not in address.lower():
        return False, f'city "{lead.city}" not in Places address'

    if all(t in place_tokens for t in tokens):
        return True, 'all distinctive name tokens + city'

    ratio = SequenceMatcher(
        None, ' '.join(tokens), ' '.join(place_tokens)).ratio()
    if ratio >= NAME_SIMILARITY_FLOOR:
        return True, f'name similarity {ratio:.2f} + city'

    return False, f'name similarity {ratio:.2f} below {NAME_SIMILARITY_FLOOR}'


# ── The lookup ────────────────────────────────────────────────────────

def _search(query):
    api_key = getattr(settings, 'GOOGLE_PLACES_API_KEY', '')
    if not api_key:
        raise PlacesError('GOOGLE_PLACES_API_KEY is not set.')

    try:
        resp = requests.post(
            PLACES_SEARCH_URL,
            headers={'Content-Type': 'application/json',
                     'X-Goog-Api-Key': api_key,
                     'X-Goog-FieldMask': FIELD_MASK},
            # 5 is enough to survive the real firm not ranking first,
            # and small enough that we are not paying to sift a page.
            json={'textQuery': query, 'pageSize': 5},
            timeout=REQUEST_TIMEOUT)
        data = resp.json()
    except requests.RequestException as exc:
        raise PlacesError(f'Places request failed: {exc}') from exc
    except ValueError as exc:
        raise PlacesError('Places returned malformed JSON.') from exc

    if resp.status_code != 200:
        msg = ((data or {}).get('error', {}).get('message')
               or f'HTTP {resp.status_code}')
        raise PlacesError(f'Places error: {msg}')

    return data.get('places', []) or []


def fetch_profile(lead, save=True):
    """Look this one firm up and copy its rating across if it matches.

    Returns a dict describing what happened. Always stamps
    ``google_profile_checked_at`` -- including on a miss, so a firm with
    no listing is not re-queried and re-billed forever.
    """
    query = ' '.join(p for p in [
        lead.firm_name, lead.city, lead.state] if p).strip()
    if not query:
        return {'matched': False, 'note': 'lead has no name to search'}

    try:
        places = _search(query)
    except PlacesError as exc:
        # NOT stamped. An API outage is not evidence about this firm, and
        # marking it checked would silently skip it forever.
        logger.warning('places lookup failed for %s: %s', lead.pk, exc)
        return {'matched': False, 'error': str(exc), 'note': 'lookup failed'}

    note, matched_place, reason = 'no Places listing found', None, ''
    for place in places:
        if (place.get('businessStatus')
                and place['businessStatus'] != 'OPERATIONAL'):
            continue
        ok, why = match_reason(place, lead)
        if ok:
            matched_place, reason = place, why
            break
        # Keep the first rejection as the explanation — it is the
        # best-ranked candidate and therefore the most informative.
        if note == 'no Places listing found':
            note = f'rejected top hit: {why}'

    now = timezone.now()
    result = {'matched': False, 'note': note, 'query': query}

    if matched_place is not None:
        rating = matched_place.get('rating')
        count = matched_place.get('userRatingCount') or 0
        lead.has_google_business = True
        if rating and count:
            lead.google_rating = rating
            lead.google_review_count = count
            note = f'matched on {reason}'
            result.update({'matched': True, 'rating': rating,
                           'review_count': count, 'reason': reason})
        else:
            note = f'listed ({reason}) but has no reviews yet'
        result['note'] = note

    lead.google_profile_checked_at = now
    lead.google_profile_note = note[:200]

    if save:
        lead.save(update_fields=[
            'google_rating', 'google_review_count', 'has_google_business',
            'google_profile_checked_at', 'google_profile_note',
            'updated_at'])

    return result


def backfill(limit=50):
    """Look up qualified leads until the limit or the daily cap stops us."""
    allowed, why = check_allowed()
    if not allowed:
        return {'looked_up': 0, 'matched': 0, 'reason': why}

    from outreach.models import OutreachSettings
    cap = int(OutreachSettings.load().places_max_lookups_per_day or 0)
    budget = max(0, cap - lookups_today())

    summary = {'looked_up': 0, 'matched': 0, 'no_listing': 0,
               'rejected': 0, 'errors': 0, 'citable': 0}

    for lead in qualified_leads()[:min(limit, budget)]:
        out = fetch_profile(lead)
        if out.get('error'):
            summary['errors'] += 1
            continue
        summary['looked_up'] += 1
        if out.get('matched'):
            summary['matched'] += 1
            if out.get('review_count', 0) >= MIN_REVIEWS_WORTH_CITING:
                summary['citable'] += 1
        elif 'rejected' in out.get('note', ''):
            summary['rejected'] += 1
        else:
            summary['no_listing'] += 1

    logger.info('places backfill: %s', summary)
    return summary
