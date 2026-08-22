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
                    job_titles=None, label='', timeout_secs=RUN_TIMEOUT_SECS):
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
        label=label or f'{niche} in {city}')

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

    leads = [m for m in (map_contact_to_lead(i) for i in items) if m]

    ledger.status = 'succeeded'
    ledger.apify_run_id = run.get('id', '')
    ledger.dataset_id = run.get('defaultDatasetId', '')
    ledger.results_returned = len(items)
    ledger.finished_at = timezone.now()
    actual = (run.get('usageTotalUsd')
              or (run.get('stats') or {}).get('computeUnits'))
    if isinstance(actual, (int, float)):
        ledger.actual_cost_usd = Decimal(str(round(float(actual), 4)))
    ledger.save()

    logger.info(
        'Apify run %s: %s items, %s mappable, est $%s actual $%s',
        ledger.apify_run_id, len(items), len(leads),
        ledger.estimated_cost_usd, ledger.actual_cost_usd)
    return leads, ledger


def import_from_dataset(dataset_id, label=''):
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

    leads = [m for m in (map_contact_to_lead(i) for i in items) if m]
    ledger.status = 'succeeded'
    ledger.results_returned = len(items)
    ledger.finished_at = timezone.now()
    ledger.save()
    logger.info('Apify dataset %s imported: %s items, %s mappable',
                dataset_id, len(items), len(leads))
    return leads, ledger


def build_actor_input(niche, city, state=None, fetch_count=50,
                      job_titles=None, label=''):
    """Map our search terms onto the actor's real input schema.

    Field names verified against the live build 2026-08-22 — this is a
    contact database, so the knobs are person/company filters rather than
    a Maps-style free-text query.
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
    }
    if city:
        payload['contact_city'] = [city]
    if niche:
        payload['company_keywords'] = [niche]
    return payload


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
        'business_type': (item.get('industry') or '').strip(),
        'linkedin_url': (item.get('company_linkedin') or '').strip(),
        # Kept as internal context for the drafter: job title and headline
        # are exactly the "one specific thing" the copy is told to
        # reference instead of inventing.
        'notes': _contact_note(item),
    }


def _contact_note(item):
    bits = []
    if item.get('job_title'):
        bits.append(f"Title: {item['job_title']}")
    if item.get('headline'):
        bits.append(f"Headline: {item['headline']}")
    if item.get('company_size'):
        bits.append(f"Company size: {item['company_size']}")
    if item.get('linkedin'):
        bits.append(f"LinkedIn: {item['linkedin']}")
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
