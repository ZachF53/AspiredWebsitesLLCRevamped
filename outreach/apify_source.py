"""
Apify lead sourcing (COLD_OUTREACH_AGENT.md §3).

Replaces ``outreach/scraper.py``'s job of FINDING leads. Enrichment,
scoring, dedup and persistence are unchanged — this returns dicts shaped
for ``outreach.pipeline.import_leads`` and nothing downstream needs to
know where they came from.

WHAT THE ACTOR ACTUALLY IS
--------------------------
``code_crafter/leads-finder`` is an **Apollo-style B2B contact database**,
not a Google Maps scraper. That difference matters more than it sounds:

  * It returns a PERSON per row — name, job title, seniority — together
    with a real, deliverable **email address**.
  * Google Places never returned an email at all. Email had to be scraped
    off the prospect's homepage afterwards, and leads with no website
    were unreachable by definition. That was the single biggest leak in
    the funnel: the scorer ranked "no website" highest, and those are
    exactly the leads we could never contact.

So this is a source of *addressable* leads rather than merely discovered
ones. Several rows can share one company; ``pipeline.import_leads`` dedups
on firm + city, so extra contacts at the same firm collapse.

COST — READ BEFORE CHANGING ANY DEFAULT
---------------------------------------
Pay-per-event, verified against the live actor 2026-08-22:

    apify-actor-start   $0.02   (one event per GB memory, minimum one)
    lead-fetched        $0.002  per lead, FREE tier

The account is on Apify's **FREE plan with a $5/month ceiling**. So:

    50 leads  = $0.02 + $0.10 = $0.12   <- current default
   100 leads  = $0.02 + $0.20 = $0.22
   3 runs/day of 100 = $0.66/day = the entire monthly plan in ~8 days

``fetch_count`` defaults to **100000** in the actor's own schema. Sending
a run without it would attempt to bill $200 against a $5 plan. It is
therefore always set explicitly, always clamped, and there is a hard
refusal below rather than a comment asking nicely.

Three independent guards, because one is never enough with money:

  1. ``spend.check_apify_allowed()``  — runs per day (OutreachSettings)
  2. ``_month_to_date_cost()``        — dollars this month vs the plan
  3. ``maxTotalChargeUsd`` on the run — Apify's own server-side ceiling,
                                        which holds even if 1 and 2 are
                                        both wrong
"""

import logging
import re
import time
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


APIFY_BASE = 'https://api.apify.com/v2'
RUN_TIMEOUT_SECS = 900
POLL_INTERVAL_SECS = 5


class ApifyError(Exception):
    """User-facing Apify failure — the message is shown in the admin."""


class ApifyQuotaReached(ApifyError):
    """Refused before spending anything. Not an error condition."""


class ApifyActorRefused(ApifyError):
    """The actor ran, billed, and declined to do the work.

    Verified live 2026-08-22: code_crafter/leads-finder returns
    "Users on the free Apify plan can run the actor through the UI and
    not via other methods." No code change fixes this — it needs a paid
    Apify plan, a different actor, or the UI + dataset-import path in
    ``import_from_dataset`` below.
    """


# ── Cost ───────────────────────────────────────────────────────────────

def estimate_cost_usd(n_results):
    """What a run of ``n_results`` leads will cost, in USD."""
    start = Decimal(str(settings.APIFY_COST_PER_RUN_START_USD))
    per_lead = Decimal(str(settings.APIFY_COST_PER_LEAD_USD))
    return start + (per_lead * Decimal(int(n_results or 0)))


def _month_to_date_cost():
    """USD spent on Apify so far this calendar month."""
    from outreach.models import ApifyRun

    today = timezone.localdate()
    start = today.replace(day=1)
    rows = ApifyRun.objects.filter(
        started_at__date__gte=start).exclude(status='refused')
    total = Decimal('0')
    for r in rows:
        total += (r.actual_cost_usd
                  if r.actual_cost_usd is not None
                  else r.estimated_cost_usd) or Decimal('0')
    return total


def budget_status():
    """Month-to-date spend vs the plan allowance, for the admin UI."""
    budget = Decimal(str(settings.APIFY_MONTHLY_BUDGET_USD))
    spent = _month_to_date_cost()
    return {
        'spent_usd': spent,
        'budget_usd': budget,
        'remaining_usd': max(Decimal('0'), budget - spent),
        'exhausted': spent >= budget,
    }


# ── The one entry point ────────────────────────────────────────────────

def run_lead_search(niche, city, state=None, max_results=None,
                    job_titles=None, label='', timeout_secs=RUN_TIMEOUT_SECS,
                    business_type=None):
    """Start an Apify run, wait for it, return dicts for ``import_leads``.

    Returns ``(leads, apify_run)``. Raises ``ApifyQuotaReached`` when a
    guard refuses — callers should surface that as "quota reached" rather
    than as a failure, so the agent wraps up cleanly instead of retrying.
    """
    from outreach.models import ApifyRun, OutreachSettings
    from outreach import spend

    token = getattr(settings, 'APIFY_TOKEN', '')
    if not token:
        raise ApifyError(
            'APIFY_TOKEN is not set. Add it to .env and restart.')

    cfg = OutreachSettings.load()

    # Guard 1 — runs per day.
    allowed, reason = spend.check_apify_allowed()
    if not allowed:
        ApifyRun.objects.create(
            actor_id=settings.APIFY_LEADS_ACTOR_ID, status='refused',
            label=label or f'{niche} in {city}', error=reason,
            finished_at=timezone.now())
        raise ApifyQuotaReached(reason)

    # Clamp BEFORE costing. The actor's own default is 100000; never let
    # a caller's value through unchecked.
    ceiling = max(0, int(cfg.apify_max_results_per_run or 0))
    requested = int(max_results or ceiling)
    requested = max(1, min(requested, ceiling)) if ceiling else 0
    if requested <= 0:
        raise ApifyQuotaReached(
            'Apify quota reached: max results per run is 0, so sourcing '
            'is disabled. Work with the leads already in the database.')

    estimate = estimate_cost_usd(requested)

    # Guard 2 — dollars this month.
    status = budget_status()
    if status['spent_usd'] + estimate > status['budget_usd']:
        msg = (
            f'Apify quota reached: this run would cost ~${estimate:.2f} and '
            f'${status["spent_usd"]:.2f} of the ${status["budget_usd"]:.2f} '
            f'monthly plan is already used. Work with the leads already in '
            f'the database and wrap up.')
        ApifyRun.objects.create(
            actor_id=settings.APIFY_LEADS_ACTOR_ID, status='refused',
            label=label or f'{niche} in {city}', error=msg,
            results_requested=requested, estimated_cost_usd=estimate,
            finished_at=timezone.now())
        raise ApifyQuotaReached(msg)

    run_input = build_actor_input(
        niche=niche, city=city, state=state,
        fetch_count=requested, job_titles=job_titles,
        label=label or f'{niche} in {city}',
        business_type=business_type)

    # Cost is recorded BEFORE the call. A run that dies mid-flight still
    # consumed compute; costing it only on success under-reports exactly
    # when it matters most.
    ledger = ApifyRun.objects.create(
        actor_id=settings.APIFY_LEADS_ACTOR_ID,
        label=run_input.get('file_name', ''),
        search_input=run_input,
        results_requested=requested,
        estimated_cost_usd=estimate,
    )

    try:
        run = _start_and_wait(token, run_input, timeout_secs)
        items = _fetch_dataset(token, run.get('defaultDatasetId', ''))
    except Exception as exc:  # noqa: BLE001
        ledger.status = 'failed'
        ledger.error = str(exc)[:2000]
        ledger.finished_at = timezone.now()
        ledger.save(update_fields=['status', 'error', 'finished_at'])
        logger.exception('Apify run failed for %r', run_input.get('file_name'))
        raise ApifyError(f'Apify run failed: {exc}') from exc

    try:
        _raise_if_actor_refused(items)
    except ApifyActorRefused as exc:
        # The run billed even though it did nothing. Record the real cost
        # so the monthly budget reflects money actually spent, and fail
        # loudly — a silent empty result would repeat every single night.
        ledger.status = 'failed'
        ledger.error = str(exc)[:2000]
        ledger.dataset_id = run.get('defaultDatasetId', '')
        ledger.apify_run_id = run.get('id', '')
        ledger.actual_cost_usd = Decimal(
            str(settings.APIFY_COST_PER_RUN_START_USD))
        ledger.finished_at = timezone.now()
        ledger.save()
        logger.error('Apify actor refused the run: %s', exc)
        raise

    kept, rejected = screen_contacts(
        items, target_state=state, target_business_type=business_type)
    for row, reason in rejected:
        logger.info('Apify ICP screen rejected %s (%s): %s',
                    row.get('full_name') or '?',
                    row.get('company_name') or '?', reason)
    leads = [m for m in (map_contact_to_lead(i) for i in kept) if m]

    ledger.status = 'succeeded'
    ledger.apify_run_id = run.get('id', '')
    ledger.dataset_id = run.get('defaultDatasetId', '')
    ledger.results_returned = len(items)
    ledger.results_rejected = len(rejected)
    ledger.finished_at = timezone.now()
    actual = (run.get('usageTotalUsd')
              or (run.get('stats') or {}).get('computeUnits'))
    if isinstance(actual, (int, float)):
        ledger.actual_cost_usd = Decimal(str(round(float(actual), 4)))
    ledger.save()

    logger.info(
        'Apify run %s: %s items, %s screened out, %s mappable, '
        'est $%s actual $%s',
        ledger.apify_run_id, len(items), len(rejected), len(leads),
        ledger.estimated_cost_usd, ledger.actual_cost_usd)
    return leads, ledger


def import_from_dataset(dataset_id, label='', target_state=None,
                        target_business_type=None):
    """Import leads from a dataset produced by a UI-triggered run.

    THE FREE-PLAN PATH. ``code_crafter/leads-finder`` refuses
    API-triggered runs on Apify's free plan but runs fine from the Apify
    Console. So: trigger the run yourself in the UI, copy the dataset ID
    off the run, and hand it here. Everything downstream — the mapper,
    dedup, scoring, enrichment — is identical to the API path.

    Reading a dataset is free, so this costs nothing beyond whatever the
    UI run already charged.

    Returns ``(leads, apify_run)``.
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
        actor_id=settings.APIFY_LEADS_ACTOR_ID,
        dataset_id=dataset_id,
        label=label or f'UI import {dataset_id}',
        # Cost was incurred by the UI run, not by us; leave the estimate
        # at zero so this import does not double-count against the
        # monthly budget.
        estimated_cost_usd=Decimal('0'),
    )
    try:
        items = _fetch_dataset(token, dataset_id)
        _raise_if_actor_refused(items)
    except Exception as exc:  # noqa: BLE001
        ledger.status = 'failed'
        ledger.error = str(exc)[:2000]
        ledger.finished_at = timezone.now()
        ledger.save(update_fields=['status', 'error', 'finished_at'])
        raise

    # The same screen as the API path. A UI-triggered run is sourced from
    # the identical actor and is no more trustworthy for being pasted in
    # by hand — a filter that guards only one door is not a filter.
    kept, rejected = screen_contacts(
        items, target_state=target_state,
        target_business_type=target_business_type)
    for row, reason in rejected:
        logger.info('Apify ICP screen rejected %s (%s): %s',
                    row.get('full_name') or '?',
                    row.get('company_name') or '?', reason)
    leads = [m for m in (map_contact_to_lead(i) for i in kept) if m]
    ledger.status = 'succeeded'
    ledger.results_returned = len(items)
    ledger.results_rejected = len(rejected)
    ledger.finished_at = timezone.now()
    ledger.save()
    logger.info('Apify dataset %s imported: %s items, %s screened out, '
                '%s mappable', dataset_id, len(items), len(rejected),
                len(leads))
    return leads, ledger


def build_actor_input(niche, city, state=None, fetch_count=50,
                      job_titles=None, label='', business_type=None):
    """Map our search terms onto the actor's real input schema.

    Field names and enum values verified against the live build
    2026-08-29 — this is a contact database, so the knobs are
    person/company filters rather than a Maps-style free-text query.

    ``state`` is deliberately NOT sent. The actor's ``contact_location``
    enum is countries only ('united states', 'germany', …); there is no
    US-state input. State is enforced post-fetch in ``screen_contact``
    instead. The parameter stays in the signature because callers pass
    it and the screen needs it.
    """
    # Decision-makers only. Emailing a junior employee about a website
    # rebuild wastes a send and a lead.
    default_titles = [
        'owner', 'founder', 'president', 'partner', 'principal',
        'managing partner', 'ceo', 'attorney',
    ]
    payload = {
        # ALWAYS explicit. The actor's own default is 100000.
        'fetch_count': int(fetch_count),
        'file_name': (label or f'{niche} {city}')[:200],
        'contact_job_title': list(job_titles or default_titles),
        # The single highest-value filter. A 2026-08-29 run without it
        # returned Norton Rose Fulbright (3000 staff) and DJC Law (64)
        # against an ICP of 1-20. Filtering at the actor means an
        # oversized firm is never fetched and never billed.
        'size': list(ICP_SIZE_BUCKETS),
        # Structured seniority rather than relying on title keywords
        # alone. Note this does NOT replace the title screen: Apollo
        # classifies "AI Product Owner" as seniority=owner, so this
        # filter would have let that row through by itself.
        'seniority_level': ['founder', 'owner', 'partner', 'c_suite'],
        # Vendor-side email validation. Cheaper than paying
        # MillionVerifier to discover the same thing downstream.
        'email_status': ['validated'],
        'contact_location': ['united states'],
    }
    if city:
        payload['contact_city'] = [city]
    if niche:
        payload['company_keywords'] = [niche]
    # `company_keywords` is free text and matches companies that SERVE
    # the niche as readily as the niche itself. A 2026-08-29 run for
    # 'law firm' returned a legal-staffing agency and a legal-marketing
    # consultancy — both correctly blocked at push, both after paying to
    # verify and personalise them. The industry enum is exact.
    industries = industries_for_business_type(business_type)
    if industries:
        payload['company_industry'] = industries
    return payload


# ── ICP screen ─────────────────────────────────────────────────────────
#
# Runs BEFORE verification and icebreaker generation. Both of those cost
# real money per lead (MillionVerifier per address, a Claude call per
# icebreaker), so a row rejected here saves more than a send.
#
# `segment_mismatch` in instantly.py already blocks an out-of-state lead
# at push time. That gate stays — it is the last line — but by then the
# money is spent. This is the same rule applied where it is still cheap.

# Buckets the actor accepts. 1-20 employees is the ICP.
ICP_SIZE_BUCKETS = ['1-10', '11-20']

# Our own ceiling, re-checked per row. The bucket filter is the actor's
# job; this is ours. `company_size` comes back as a raw headcount and a
# row can carry a headcount that disagrees with its bucket.
ICP_MAX_COMPANY_SIZE = 20

# Titles that contain a decision-maker word but do not belong to the
# decision maker. "Product Owner" is the one that actually got through:
# Apollo scores it seniority=owner, and `contact_job_title: ['owner']`
# matches it on a substring.
_TITLE_DISQUALIFIERS = (
    'product owner', 'process owner', 'product manager',
    'program manager', 'project manager', 'scrum master',
    'business analyst', 'account manager', 'account executive',
    'office manager', 'marketing manager', 'operations manager',
    'legal assistant', 'legal secretary', 'paralegal',
    'executive assistant', 'recruiter', 'intern',
)

# Whole tokens that identify someone who can say yes to a website
# rebuild. Matched as TOKENS, never as substrings — the substring match
# is the bug this exists to prevent.
_DECISION_MAKER_TOKENS = frozenset({
    'owner', 'founder', 'cofounder', 'president', 'principal',
    'partner', 'ceo', 'proprietor', 'shareholder', 'member',
    'attorney', 'lawyer', 'esq', 'esquire', 'counsel',
    'dentist', 'dds', 'dmd', 'physician', 'cpa',
})


def _headcount_or_none(value):
    """Employee count as a non-negative int, or None when unusable.

    Apollo sends this as an int on some rows and a string on others.
    """
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _title_tokens(title):
    """Lowercase alphanumeric tokens of a job title."""
    return {t for t in re.split(r'[^a-z0-9]+', (title or '').lower()) if t}


def screen_contact(item, target_state=None, max_size=None,
                   target_business_type=None):
    """Why this row is not an ICP match. '' means it is.

    Fails closed on the size check only when a size is actually
    reported: a missing `company_size` is missing data, not evidence of
    a small firm, but rejecting every row without one would discard
    good small firms that Apollo simply has no headcount for. Size is
    therefore enforced when known and left to the other checks when not.
    """
    max_size = ICP_MAX_COMPANY_SIZE if max_size is None else max_size

    # NOT _int_or_none — that one clamps to 1700..2100 because it parses
    # founding years, so every real headcount comes back None and the
    # ceiling silently never fires.
    size = _headcount_or_none(item.get('company_size'))
    if size is not None and size > max_size:
        return (f'{item.get("company_name") or "Company"} has ~{size} '
                f'staff; ICP is {max_size} or fewer.')

    title = (item.get('job_title') or '').strip()
    lowered = title.lower()
    for phrase in _TITLE_DISQUALIFIERS:
        if phrase in lowered:
            return f'Job title {title!r} is not a decision maker.'

    if title and not (_title_tokens(title) & _DECISION_MAKER_TOKENS):
        return f'Job title {title!r} carries no decision-maker term.'

    if target_state:
        from outreach.instantly import _STATE_ABBREV
        raw = (item.get('company_state') or item.get('state') or '').strip()
        got = _STATE_ABBREV.get(raw.lower(), raw).upper()
        want = _STATE_ABBREV.get(
            target_state.strip().lower(), target_state).upper()
        if got != want:
            return (f'Company is in {raw or "an unknown state"}; '
                    f'targeting {want}.')

    if target_business_type:
        # Compared through normalise_business_type, the same function the
        # mapper uses, so this screen and the push-time segment gate
        # always reach the same verdict about the same row.
        got_type = normalise_business_type(item.get('industry'))
        if got_type.strip().lower() != target_business_type.strip().lower():
            return (f'{item.get("company_name") or "Company"} is '
                    f'{got_type or "an unknown industry"}; '
                    f'targeting {target_business_type}.')

    return ''


def screen_contacts(items, target_state=None, target_business_type=None):
    """Split rows into (kept, rejections). Rejections are [(row, reason)].

    Never silent — the caller logs the count and writes it to the run
    ledger. A filter that drops rows without saying so is how a bad list
    looks like a good one.
    """
    kept, rejected = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        reason = screen_contact(
            it, target_state=target_state,
            target_business_type=target_business_type)
        (rejected.append((it, reason)) if reason else kept.append(it))
    return kept, rejected


# Apollo-style industry strings -> the business_type values campaigns
# target. Without this the segment gate in instantly.push_leads rejects
# every lead: the actor returns "law practice" or "legal services", the
# TX campaign asks for "Law Firm", and a straight string compare fails on
# a list that is entirely correct.
#
# Mapped rather than loosened deliberately. Making the gate fuzzy would
# also let a "Legal Staffing" company through, which is the exact
# category the campaign is trying to exclude.
_BUSINESS_TYPE_MAP = {
    'law practice': 'Law Firm',
    'legal services': 'Law Firm',
    'law firm': 'Law Firm',
    'attorney': 'Law Firm',
    'attorneys': 'Law Firm',
    'legal': 'Law Firm',
    'dentist': 'Dentist',
    'dental': 'Dentist',
    'dentistry': 'Dentist',
    'dental practice': 'Dentist',
    'medical practice': 'Medical Practice',
    'health, wellness & fitness': 'Medical Practice',
    'hospital & health care': 'Medical Practice',
    'accounting': 'Accounting',
    'financial services': 'Financial Services',
}


def normalise_business_type(industry):
    """Apollo's industry label -> our business_type.

    Unknown industries pass through title-cased rather than being
    blanked: an unmapped value is still information, and a blank
    business_type would sail through a campaign's segment check instead
    of being caught by it.
    """
    raw = (industry or '').strip()
    if not raw:
        return ''
    return _BUSINESS_TYPE_MAP.get(raw.lower(), raw.title())


# Values the actor's `company_industry` enum actually accepts, verified
# against the live build 2026-08-29. _BUSINESS_TYPE_MAP also carries our
# own normalisation spellings ('attorney', 'legal', 'dental') which are
# NOT Apollo values — sending one would be an invalid enum — so the two
# sets are intersected below rather than used interchangeably.
_ACTOR_INDUSTRY_ENUM = frozenset({
    'law practice', 'legal services', 'accounting', 'financial services',
    'medical practice', 'hospital & health care', 'mental health care',
    'health, wellness & fitness', 'alternative medicine',
    'marketing & advertising', 'staffing & recruiting',
})


# A search niche is not a business type. "family law", "personal injury"
# and "estate planning attorney" are all searches for LAW FIRMS, and the
# campaign that will receive them targets business_type "Law Firm".
#
# Longest needle first so 'dental law' cannot match 'law' before
# 'dental' — order here is the tie-break, not a preference.
_NICHE_TYPE_HINTS = (
    ('orthodont', 'Dentist'),
    ('dentist', 'Dentist'),
    ('dental', 'Dentist'),
    ('chiropract', 'Medical Practice'),
    ('medical', 'Medical Practice'),
    ('clinic', 'Medical Practice'),
    ('bookkeep', 'Accounting'),
    ('accounting', 'Accounting'),
    ('cpa', 'Accounting'),
    ('financial', 'Financial Services'),
    ('wealth', 'Financial Services'),
    ('attorney', 'Law Firm'),
    ('lawyer', 'Law Firm'),
    ('legal', 'Law Firm'),
    ('law', 'Law Firm'),
)


def business_type_for_niche(niche):
    """Free-text search niche -> the segment's business_type, or ''.

    WHY THIS IS NOT ``niche.title()``
    --------------------------------
    It used to be. A scrape for "family law" therefore ran with
    business_type "Family Law", and:

      * ``industries_for_business_type('Family Law')`` is empty, so no
        industry filter reached the actor;
      * the post-fetch screen compared Apollo's "Law Practice" — which
        normalises to "Law Firm" — against "Family Law" and rejected
        EVERY row;
      * any survivor would carry business_type "Family Law" and be
        blocked again by the campaign segment gate at push.

    So the run billed in full and imported nothing, and the reason was
    three layers down. Returns '' for an unrecognised niche, which means
    "do not constrain on type": the size, title and state screens still
    apply, and importing leads that need a segment correction beats
    importing none.
    """
    text = (niche or '').lower()
    for needle, business_type in _NICHE_TYPE_HINTS:
        if needle in text:
            return business_type
    return ''


def industries_for_business_type(business_type):
    """Apollo industry values that normalise to ``business_type``.

    DERIVED from _BUSINESS_TYPE_MAP, never written out separately, so
    the actor filter and the segment gate cannot disagree. Every value
    sent to the actor is one `normalise_business_type` maps back to the
    type the campaign wants; if it did not, the lead would be fetched,
    verified, personalised, and then blocked at push having cost money
    at every stage.

    Returns [] when nothing maps, which means "do not constrain" — a
    niche we have no mapping for is better sourced broadly and screened
    than silently sourced as nothing.
    """
    want = (business_type or '').strip().lower()
    if not want:
        return []
    derived = {k for k, v in _BUSINESS_TYPE_MAP.items()
               if v.strip().lower() == want}
    return sorted(derived & _ACTOR_INDUSTRY_ENUM)


def map_contact_to_lead(item):
    """One Apify contact row -> one ``import_leads`` dict.

    Returns None for rows with no company name — ``firm_name`` is
    required on the model and a nameless row cannot be deduped.
    """
    if not isinstance(item, dict):
        return None
    firm = (item.get('company_name') or '').strip()
    if not firm:
        return None

    email = (item.get('email') or item.get('personal_email') or '').strip()
    phone = (item.get('company_phone') or item.get('mobile_number')
             or '').strip()

    return {
        'firm_name': firm,
        # attorney_name is the contact-name field for every business type
        # (CLAUDE.md -> Data Model Decisions); it was not renamed.
        'attorney_name': (item.get('full_name') or '').strip(),
        'email': email.lower(),
        'phone': phone,
        'website': (item.get('company_website') or '').strip(),
        'address': (item.get('company_full_address') or '').strip(),
        'city': (item.get('company_city') or item.get('city') or '').strip(),
        'state': (item.get('company_state') or item.get('state') or '').strip(),
        'business_type': normalise_business_type(item.get('industry')),
        # Warm-opener material. Kept as structured fields so the
        # icebreaker's fabrication guard can verify what it claims.
        'founded_year': _int_or_none(item.get('company_founded_year')),
        'practice_areas': (item.get('keywords') or '').strip()[:500],
        'linkedin_url': (item.get('company_linkedin') or '').strip(),
        # Kept as internal context for the drafter: job title and headline
        # are exactly the "one specific thing" the copy is told to
        # reference instead of inventing.
        'notes': _contact_note(item),
    }


def _int_or_none(value):
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    # A founding year outside this range is bad data, not a fact.
    return year if 1700 <= year <= 2100 else None


def _contact_note(item):
    """Context the icebreaker writes from.

    Field population measured across 100 live rows 2026-08-23:

        job_title             100%
        headline               98%   (usually just "Owner, <firm>")
        company_technologies   96%
        company_description    87%   (often boilerplate, sometimes not English)
        keywords               74%   <- practice areas, the useful one
        company_founded_year   65%   <- tenure, the other useful one

    keywords and founded_year were not captured before. They are the two
    fields that support a WARM opener -- "20 years in estate planning"
    reads as research; "your PageSpeed is 36/100" reads as a critique,
    and a critique in sentence one puts a stranger on the defensive.
    """
    bits = []
    if item.get('job_title'):
        bits.append(f"Title: {item['job_title']}")
    if item.get('keywords'):
        bits.append(f"Practice areas: {item['keywords']}")
    if item.get('company_founded_year'):
        bits.append(f"Founded: {item['company_founded_year']}")
    if item.get('company_size'):
        bits.append(f"Company size: {item['company_size']}")
    # Skipped when it is the generic Apollo filler, which says nothing
    # and would tempt the model into inventing something around it.
    desc = (item.get('company_description') or '').strip()
    if desc and 'based out of' not in desc.lower() and len(desc) > 60:
        bits.append(f"About: {desc[:400]}")
    if item.get('company_linkedin'):
        bits.append(f"LinkedIn: {item['company_linkedin']}")
    return ' | '.join(bits)


# ── HTTP ───────────────────────────────────────────────────────────────

def _start_and_wait(token, run_input, timeout_secs):
    """Start the run and block until it reaches a terminal status.

    Uses the async runs endpoint plus ``waitForFinish`` rather than
    ``/run-sync``: run-sync returns the actor's OUTPUT key-value record,
    which this actor leaves empty, so the response body was not JSON at
    all and parsing it raised "Expecting value: line 1 column 1". We need
    the run OBJECT anyway — its id, dataset id and billed cost.

    ``maxTotalChargeUsd`` is guard 3 — Apify enforces it server-side, so
    it holds even if our own arithmetic is wrong.
    """
    actor = settings.APIFY_LEADS_ACTOR_ID
    deadline = time.monotonic() + timeout_secs

    resp = requests.post(
        f'{APIFY_BASE}/acts/{actor}/runs',
        headers={'Authorization': f'Bearer {token}'},
        params={
            'timeout': int(timeout_secs),
            'maxTotalChargeUsd': settings.APIFY_MAX_TOTAL_CHARGE_USD,
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


def _raise_if_actor_refused(items):
    """Detect an actor that ran but refused to do the work.

    ``code_crafter/leads-finder`` blocks API-triggered runs on Apify's
    FREE plan and writes a single ``{"error": ...}`` row to the dataset
    instead of leads. The run still reports SUCCEEDED and still bills the
    $0.02 start event, so without this check every scheduled scrape would
    look like "ran fine, found nothing" while quietly costing money.

    Raised as ApifyError so the caller records a FAILED run rather than a
    successful empty one.
    """
    if len(items) == 1 and isinstance(items[0], dict):
        err = items[0].get('error')
        if err and not items[0].get('company_name'):
            raise ApifyActorRefused(str(err))


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
