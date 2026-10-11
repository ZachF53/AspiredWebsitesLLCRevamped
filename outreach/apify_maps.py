"""
Apify Google Maps sourcing — the no-website segment.

A SECOND Apify source, deliberately separate from ``apify_source.py``.
They use different actors and answer different questions:

    apify_source.py   code_crafter/leads-finder      a PERSON, with email
    apify_maps.py     compass/crawler-google-places  a BUSINESS, no email

WHY THIS EXISTS
---------------
``LEAD_PIPELINE_PLAYBOOK.md`` argues against Maps as a lead source, and
for law firms it was right: a Maps-sourced list measured ~34% role
addresses against 0/90 from the contact database.

But that argument is about EMAIL. It has nothing to say about the segment
this module targets, which the contact database cannot reach at all:

  * ``scoring.score_lead`` gives "no website" 4 points — the single
    largest signal in the model — because a business with no website is
    the one that most needs the thing we sell.
  * Those businesses are, by construction, absent from an Apollo-style
    contact database. A four-person HVAC shop with no web presence has no
    enriched contact record anywhere.
  * So the funnel's highest-scoring leads were also the only ones it
    could never source. The playbook names this as the biggest leak in
    the whole pipeline.

Google Maps lists exactly these businesses, with a phone number.

THERE IS NO EMAIL ON THIS PATH. NOT "RARELY" — NEVER.
-----------------------------------------------------
This is a structural property, not a gap to be fixed later:

  * The Maps actor returns no email field for a place.
  * Its own ``lead-scraped`` enrichment add-on (work email, name,
    LinkedIn) REQUIRES a website — the actor's own docs tell you to
    enable the "places with a website" filter to use it.
  * ``enricher.py`` finds email by scraping a homepage. There is no
    homepage.

So every lead from this module arrives with ``email=''`` and cannot be
cold-emailed. ``instantly.push_leads`` already refuses a lead with no
icebreaker and no address, so nothing here can leak into an email
campaign by accident — but do not read that as "email will work once
enrichment runs". The contact channel for this segment is the PHONE
number, and the track that uses it is not built yet.

``map_place_to_lead`` deliberately does not invent an email, not even a
guessed ``info@`` pattern. A guessed address is an unverified address,
and the playbook's whole posture is that an ambiguous address fails
closed.

COST — tier BRONZE, read off the live actor 2026-10-10
------------------------------------------------------
    place-scraped      $0.003   per place returned
    filter-applied     $0.001   per place, PER FILTER
    apify-actor-start  $0.00005

Filters are billed per place per filter, so they are not free and the
count matters:

    1,000 places, 1 filter (no-website)        ~= $4.00
    1,000 places, 2 filters (+ skip closed)    ~= $5.00

Measured: a 10-place probe with 2 filters billed $0.0152 against a $0.05
estimate, so the rates above over-estimate. That is the correct direction
for a guard — it refuses early rather than late.

Spend guards are the same three as ``apify_source``, and the dollar
ledger is SHARED with it on purpose: both actors bill the one Apify
account, so two independent budgets would each happily approve a run
while together busting the plan.
"""

import logging
import time
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone

from outreach.apify_source import (
    ApifyError,
    ApifyQuotaReached,
    budget_status,
)

logger = logging.getLogger(__name__)


APIFY_BASE = 'https://api.apify.com/v2'
RUN_TIMEOUT_SECS = 900

# The actor's `website` enum. 'withoutWebsite' is the whole reason this
# module exists; it is a SERVER-SIDE filter, so a place that has a site is
# never returned and never billed.
WEBSITE_ANY = 'allPlaces'
WEBSITE_WITH = 'withWebsite'
WEBSITE_WITHOUT = 'withoutWebsite'


# ── Cost ───────────────────────────────────────────────────────────────

def estimate_cost_usd(n_places, n_filters=1):
    """What a run of ``n_places`` with ``n_filters`` filters will cost."""
    per_place = Decimal(str(settings.APIFY_MAPS_COST_PER_PLACE_USD))
    per_filter = Decimal(str(settings.APIFY_MAPS_COST_PER_FILTER_USD))
    places = Decimal(int(n_places or 0))
    return (per_place * places) + (
        per_filter * places * Decimal(max(0, int(n_filters))))


# ── The one entry point ────────────────────────────────────────────────

def run_maps_search(niche, city, state=None, max_results=None,
                    website_filter=WEBSITE_WITHOUT, business_type=None,
                    skip_closed=True, min_stars=None, label='',
                    timeout_secs=RUN_TIMEOUT_SECS):
    """Scrape Google Maps via Apify, return dicts for ``import_leads``.

    Returns ``(leads, apify_run)``. Raises ``ApifyQuotaReached`` when a
    guard refuses — callers should surface that as "quota reached" rather
    than as a failure.

    ``website_filter`` defaults to no-website-only. Passing
    ``WEBSITE_ANY`` widens the run to every place and roughly doubles the
    rows for the same spend, but it also mixes in leads the email
    pipeline should own, so it is an explicit choice rather than a
    default.
    """
    from outreach.models import ApifyRun, OutreachSettings
    from outreach import spend

    token = getattr(settings, 'APIFY_TOKEN', '')
    if not token:
        raise ApifyError(
            'APIFY_TOKEN is not set. Add it to .env and restart.')

    actor = settings.APIFY_MAPS_ACTOR_ID
    cfg = OutreachSettings.load()
    run_label = label or f'{niche} in {city}{f", {state}" if state else ""}'

    # Guard 1 — runs per day. Shared with the contact-DB actor: the cap is
    # on Apify runs, not on one actor's runs.
    allowed, reason = spend.check_apify_allowed()
    if not allowed:
        ApifyRun.objects.create(
            actor_id=actor, status='refused', label=run_label,
            error=reason, finished_at=timezone.now())
        raise ApifyQuotaReached(reason)

    # Clamp BEFORE costing. Never trust a caller's count against a
    # pay-per-place actor.
    ceiling = max(0, int(cfg.apify_max_results_per_run or 0))
    requested = int(max_results or ceiling)
    requested = max(1, min(requested, ceiling)) if ceiling else 0
    if requested <= 0:
        raise ApifyQuotaReached(
            'Apify quota reached: max results per run is 0, so sourcing '
            'is disabled. Work with the leads already in the database.')

    run_input, n_filters = build_actor_input(
        niche=niche, city=city, state=state, max_places=requested,
        website_filter=website_filter, skip_closed=skip_closed,
        min_stars=min_stars)
    estimate = estimate_cost_usd(requested, n_filters)

    # Guard 2 — dollars this month, against the SHARED Apify ledger.
    status = budget_status()
    if status['spent_usd'] + estimate > status['budget_usd']:
        msg = (
            f'Apify quota reached: this run would cost ~${estimate:.2f} and '
            f'${status["spent_usd"]:.2f} of the ${status["budget_usd"]:.2f} '
            f'monthly plan is already used. Work with the leads already in '
            f'the database and wrap up.')
        ApifyRun.objects.create(
            actor_id=actor, status='refused', label=run_label, error=msg,
            results_requested=requested, estimated_cost_usd=estimate,
            finished_at=timezone.now())
        raise ApifyQuotaReached(msg)

    # Cost recorded BEFORE the call — a run that dies mid-flight still
    # consumed compute and still billed.
    ledger = ApifyRun.objects.create(
        actor_id=actor,
        label=run_label,
        search_input=run_input,
        results_requested=requested,
        estimated_cost_usd=estimate,
    )

    try:
        run = _start_and_wait(token, actor, run_input, timeout_secs)
        items = _fetch_dataset(token, run.get('defaultDatasetId', ''))
    except Exception as exc:  # noqa: BLE001
        ledger.status = 'failed'
        ledger.error = str(exc)[:2000]
        ledger.finished_at = timezone.now()
        ledger.save(update_fields=['status', 'error', 'finished_at'])
        logger.exception('Apify Maps run failed for %r', run_label)
        raise ApifyError(f'Apify Maps run failed: {exc}') from exc

    kept, rejected = screen_places(
        items, target_state=state, website_filter=website_filter)
    for row, why in rejected:
        logger.info('Maps screen rejected %s: %s',
                    row.get('title') or '?', why)
    leads = [m for m in (map_place_to_lead(
        i, business_type=business_type,
        fallback_city=city, fallback_state=state) for i in kept) if m]

    ledger.status = 'succeeded'
    ledger.apify_run_id = run.get('id', '')
    ledger.dataset_id = run.get('defaultDatasetId', '')
    ledger.results_returned = len(items)
    ledger.results_rejected = len(rejected)
    ledger.finished_at = timezone.now()
    # PROVISIONAL. `usageTotalUsd` is not final at the instant a run
    # reports SUCCEEDED — charged events settle for a short while after.
    # Measured 2026-10-10: a probe read $0.0152 at finish and settled at
    # $0.0502, a 3x under-report. So this figure is a floor, and
    # `manage.py reconcile_apify_spend` is what trues it up afterwards.
    # Do not treat it as the authoritative cost of the run.
    actual = run.get('usageTotalUsd')
    if isinstance(actual, (int, float)):
        ledger.actual_cost_usd = Decimal(str(round(float(actual), 4)))
    ledger.save()

    logger.info(
        'Apify Maps run %s: %s places, %s screened out, %s mappable, '
        'est $%s actual $%s (provisional)',
        ledger.apify_run_id, len(items), len(rejected), len(leads),
        ledger.estimated_cost_usd, ledger.actual_cost_usd)
    return leads, ledger


def import_from_dataset(dataset_id, label='', target_state=None,
                        business_type=None,
                        website_filter=WEBSITE_WITHOUT,
                        fallback_city='', fallback_state=''):
    """Import Maps places from an existing dataset — free.

    Dataset reads are not billed, so this is the way to re-import a run
    that was triggered in the Apify Console, or to re-process a run whose
    import failed downstream without paying for the scrape twice.
    """
    from outreach.models import ApifyRun

    token = getattr(settings, 'APIFY_TOKEN', '')
    if not token:
        raise ApifyError(
            'APIFY_TOKEN is not set. Add it to .env and restart.')
    dataset_id = (dataset_id or '').strip()
    if not dataset_id:
        raise ApifyError('A dataset ID is required.')

    ledger = ApifyRun.objects.create(
        actor_id=settings.APIFY_MAPS_ACTOR_ID,
        dataset_id=dataset_id,
        label=label or f'Maps dataset import {dataset_id}',
        # The scrape was already billed to whoever ran it. Zero here so
        # the import does not double-count against the monthly budget.
        estimated_cost_usd=Decimal('0'),
        # Starts no actor, so it must not consume a slot in the
        # runs-per-day quota either.
        billable=False,
    )
    try:
        items = _fetch_dataset(token, dataset_id)
    except Exception as exc:  # noqa: BLE001
        ledger.status = 'failed'
        ledger.error = str(exc)[:2000]
        ledger.finished_at = timezone.now()
        ledger.save(update_fields=['status', 'error', 'finished_at'])
        raise

    kept, rejected = screen_places(
        items, target_state=target_state, website_filter=website_filter)
    for row, why in rejected:
        logger.info('Maps screen rejected %s: %s',
                    row.get('title') or '?', why)
    leads = [m for m in (map_place_to_lead(
        i, business_type=business_type,
        fallback_city=fallback_city,
        fallback_state=fallback_state or target_state or '')
        for i in kept) if m]

    ledger.status = 'succeeded'
    ledger.results_returned = len(items)
    ledger.results_rejected = len(rejected)
    ledger.finished_at = timezone.now()
    ledger.save()
    logger.info('Maps dataset %s: %s places, %s screened out, %s mappable',
                dataset_id, len(items), len(rejected), len(leads))
    return leads, ledger


def build_actor_input(niche, city, state=None, max_places=50,
                      website_filter=WEBSITE_WITHOUT, skip_closed=True,
                      min_stars=None):
    """Map our search terms onto the actor's input schema.

    Returns ``(payload, n_filters)`` — the filter count is needed for the
    cost estimate, because ``filter-applied`` bills per place PER FILTER.
    Returning it alongside the payload keeps the two from drifting: the
    thing that adds a filter is the thing that counts it.

    Field names verified against the live build 2026-10-10.
    """
    location = f'{city}, {state}'.strip(', ') if city else (state or '')
    payload = {
        # ALWAYS explicit — this is a pay-per-place actor.
        'maxCrawledPlacesPerSearch': int(max_places),
        'searchStringsArray': [niche] if niche else [],
        'locationQuery': location,
        'language': 'en',
        # 'all' also matches places whose category merely relates to the
        # search term. The tighter modes drop genuine small operators
        # whose Maps category is spelled differently ("Air conditioning
        # contractor" vs "HVAC contractor"), which is exactly the segment
        # being targeted, so the broad match is deliberate and the
        # category screen happens downstream.
        'searchMatching': 'all',
    }

    n_filters = 0
    if website_filter and website_filter != WEBSITE_ANY:
        payload['website'] = website_filter
        n_filters += 1
    if skip_closed:
        payload['skipClosedPlaces'] = True
        n_filters += 1
    if min_stars:
        payload['placeMinimumStars'] = min_stars
        n_filters += 1
    return payload, n_filters


# ── Screen ─────────────────────────────────────────────────────────────

def screen_place(item, target_state=None, website_filter=WEBSITE_WITHOUT):
    """Why this place is not wanted. '' means it is."""
    if not (item.get('title') or '').strip():
        return 'No business name.'

    if item.get('permanentlyClosed'):
        return 'Permanently closed.'
    if item.get('temporarilyClosed'):
        return 'Temporarily closed.'

    # Verify the server-side filter actually held. It is Apify's job and
    # it worked on 2026-10-10, but if it ever regresses silently we would
    # pay for has-website places, import them as this segment, and hand
    # them the 4-point "no website" score that drives prioritisation.
    # A filter worth paying for is worth confirming.
    website = (item.get('website') or '').strip()
    if website_filter == WEBSITE_WITHOUT and website:
        return f'Has a website ({website[:60]}) - filter asked for none.'
    if website_filter == WEBSITE_WITH and not website:
        return 'Has no website — filter asked for places with one.'

    # No phone and no website is a lead with no contact channel at all.
    if not (item.get('phone') or item.get('phoneUnformatted')
            or '').strip() and not website:
        return 'No phone and no website — no way to make contact.'

    # State is enforced when KNOWN and left to the other checks when not.
    #
    # Measured 2026-10-10 on a 100-place San Antonio run: 45 rows carried
    # no state, no city and no address at all — they are SERVICE-AREA
    # businesses, HVAC operators working out of a van who never published
    # a storefront on Maps. Every one had a 210 (San Antonio) phone.
    #
    # Rejecting them for "unknown state" discarded 44 leads, and they are
    # the best leads in the batch: no website AND no premises is as small
    # as this ICP gets. Absence of a measurement is not evidence of a
    # problem — the same rule apify_source.screen_contact applies to
    # company size.
    raw = (item.get('state') or '').strip()
    if target_state and raw:
        from outreach.instantly import _STATE_ABBREV
        got = _STATE_ABBREV.get(raw.lower(), raw).upper()
        want = _STATE_ABBREV.get(
            target_state.strip().lower(), target_state).upper()
        if got != want:
            return f'Place is in {raw}; targeting {want}.'
    return ''


def screen_places(items, target_state=None,
                  website_filter=WEBSITE_WITHOUT):
    """Split rows into (kept, [(row, reason), ...]). Never silent."""
    kept, rejected = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        why = screen_place(
            it, target_state=target_state, website_filter=website_filter)
        (rejected.append((it, why)) if why else kept.append(it))
    return kept, rejected


# ── Mapping ────────────────────────────────────────────────────────────

def map_place_to_lead(item, business_type=None, fallback_city='',
                      fallback_state=''):
    """One Maps place -> one ``import_leads`` dict.

    No ``email`` key is produced, ever — see the module docstring. The
    absence is the honest representation of this source, and
    ``score_lead`` reads the blank ``website`` to award the 4 points that
    make these leads rank where they should.

    ``fallback_city``/``fallback_state`` fill in for service-area
    businesses, which Maps returns with no address, city or state (45 of
    100 on a real San Antonio run). The fallback is the city and state we
    SEARCHED, so it says "Google returns this business for San Antonio,
    TX" — which is true and is what a campaign segment needs. It is not a
    claim about a street address, and ``address`` is left blank
    accordingly rather than being invented.

    Same pattern as ``scraper._parse_city_state``, which already takes
    the searched city/state as a fallback for the Places path.
    """
    if not isinstance(item, dict):
        return None
    firm = (item.get('title') or '').strip()
    if not firm:
        return None

    from outreach.instantly import _STATE_ABBREV
    raw_state = (item.get('state') or '').strip() or (fallback_state or '')
    # The actor returns the full name ('Texas'); the Lead model, the
    # campaign segment gate and the ICP screens all compare on 'TX'.
    state = _STATE_ABBREV.get(raw_state.lower(), raw_state).upper()[:2] \
        if raw_state else ''

    phone = ((item.get('phone') or '').strip()
             or (item.get('phoneUnformatted') or '').strip())

    return {
        'firm_name': firm,
        # Maps gives a business, not a person. attorney_name is the
        # contact-name field for every business type (CLAUDE.md -> Data
        # Model Decisions) and is left blank rather than filled with the
        # company name, which would read as a person's name in copy.
        'attorney_name': '',
        'phone': phone[:20],
        # Blank on purpose and load-bearing: scoring.score_lead awards
        # +4 for a falsy website.
        'website': (item.get('website') or '').strip(),
        'address': (item.get('address') or '').strip(),
        'city': ((item.get('city') or '').strip()
                 or (fallback_city or '').strip()),
        'state': state,
        'business_type': (business_type or '').strip(),
        'google_rating': item.get('totalScore'),
        # Key MUST be 'google_review_count' — scoring.py and the Lead
        # model both use it. A 'reviewsCount' key here would silently
        # score every lead as zero reviews.
        'google_review_count': item.get('reviewsCount') or 0,
        # Listed in Maps at all = a Google presence.
        'has_google_business': True,
        'notes': _place_note(item),
    }


def _place_note(item):
    """Context for the operator and, later, the call script.

    Everything here is a measured field off the Maps record, so anything
    written from it is checkable — which is the standard the icebreaker
    screen enforces and a call script should meet too.
    """
    bits = []
    if item.get('categoryName'):
        bits.append(f"Category: {item['categoryName']}")
    if item.get('neighborhood'):
        bits.append(f"Neighborhood: {item['neighborhood']}")
    if item.get('totalScore') and item.get('reviewsCount'):
        bits.append(f"Google: {item['totalScore']} stars, "
                    f"{item['reviewsCount']} reviews")
    # An unclaimed listing is a real, checkable opening: the owner has
    # never taken control of their own Google presence.
    if item.get('claimThisBusiness') is True:
        bits.append('Google Business Profile is UNCLAIMED')
    hours = item.get('openingHours')
    if isinstance(hours, list) and hours:
        open_24 = any('24 hours' in str(h.get('hours', '')) for h in hours
                      if isinstance(h, dict))
        if open_24:
            bits.append('Advertises 24-hour availability')
    if item.get('placeId'):
        bits.append(f"placeId: {item['placeId']}")
    if item.get('url'):
        bits.append(f"Maps: {item['url']}")
    return ' | '.join(bits)[:2000]


# ── HTTP ───────────────────────────────────────────────────────────────

def _start_and_wait(token, actor, run_input, timeout_secs):
    """Start the run and block until it reaches a terminal status.

    ``maxTotalChargeUsd`` is guard 3 — Apify enforces it server-side, so
    it holds even when our own arithmetic is wrong. The actor rejects a
    value below its own minimum with HTTP 400
    ``max-total-charge-usd-below-minimum``, so the configured ceiling is
    floored to that minimum rather than sent as-is. It is a cap, not a
    charge: a 10-place run bills ~$0.015 under a $0.50 cap.
    """
    ceiling = max(
        float(settings.APIFY_MAX_TOTAL_CHARGE_USD),
        float(settings.APIFY_MAPS_MIN_MAX_CHARGE_USD),
    )
    deadline = time.monotonic() + timeout_secs

    resp = requests.post(
        f'{APIFY_BASE}/acts/{actor}/runs',
        headers={'Authorization': f'Bearer {token}'},
        params={
            'timeout': int(timeout_secs),
            'maxTotalChargeUsd': ceiling,
            # Apify caps this at 60s per call; we loop below.
            'waitForFinish': 60,
        },
        json=run_input,
        timeout=90,
    )
    if resp.status_code >= 400:
        raise ApifyError(f'Apify HTTP {resp.status_code}: {resp.text[:300]}')
    try:
        data = (resp.json() or {}).get('data', {})
    except ValueError:
        raise ApifyError(
            f'Apify returned a non-JSON response: {resp.text[:200]}')

    run_id = data.get('id')
    while data.get('status') in ('READY', 'RUNNING') and run_id:
        if time.monotonic() > deadline:
            raise ApifyError(
                f'Apify run {run_id} still {data.get("status")} after '
                f'{timeout_secs}s — giving up waiting.')
        poll = requests.get(
            f'{APIFY_BASE}/actor-runs/{run_id}',
            headers={'Authorization': f'Bearer {token}'},
            params={'waitForFinish': 60},
            timeout=90,
        )
        if poll.status_code >= 400:
            raise ApifyError(
                f'Apify poll HTTP {poll.status_code}: {poll.text[:200]}')
        data = (poll.json() or {}).get('data', {})

    if data.get('status') != 'SUCCEEDED':
        raise ApifyError(
            f'Apify run finished as {data.get("status")}: '
            f'{data.get("statusMessage", "")}')
    return data


def _fetch_dataset(token, dataset_id):
    """Pull the run's items. Free — dataset reads are not billed."""
    if not dataset_id:
        return []
    resp = requests.get(
        f'{APIFY_BASE}/datasets/{dataset_id}/items',
        headers={'Authorization': f'Bearer {token}'},
        params={'clean': 'true'},
        timeout=120,
    )
    if resp.status_code >= 400:
        raise ApifyError(
            f'Apify dataset HTTP {resp.status_code}: {resp.text[:200]}')
    items = resp.json()
    return items if isinstance(items, list) else []
