"""
Lead scrapers — three sources.

    - Google Maps  → Google Places API (New) — HTTP, synchronous
    - Texas State Bar attorney directory → Playwright (async)
    - Georgia State Bar attorney directory → Playwright (async)

All scrapers produce dicts shaped for outreach.pipeline.import_leads. None
write to the DB — they're pure functions of (search params) → (raw lead data).
Persisting, scoring, and dedup happen in the pipeline.

Sync entry points for Celery tasks / Django views:
    scrape_google_maps_sync(...)   → (leads, api_call_count)
    scrape_texas_bar_sync(...)     → leads
    scrape_georgia_bar_sync(...)   → leads

──────────────────────────────────────────────────────────────────────────────
NOTES:

1. Google Maps uses the Google Places API (New) at places.googleapis.com.
   Headless Playwright is blocked by Google's bot detection; the Places API
   is the legitimate, reliable, ToS-compliant path. The legacy
   maps.googleapis.com/maps/api/place/* endpoints are deprecated and cannot
   be enabled on new Cloud projects. Requires GOOGLE_PLACES_API_KEY in .env.
   See CLAUDE.md → External APIs & Costs.

2. The two State Bar scrapers still use Playwright. Browser binaries are NOT
   installed by `pip install playwright` — run once: `playwright install chromium`.

3. State Bar selectors are best-effort against the live sites and marked
   `# SELECTOR:`. They need tuning against the live pages.

4. State bar directories are public professional rosters. Light scraping for
   legitimate B2B prospecting is generally tolerated — keep volume modest and
   use the polite delays below.
──────────────────────────────────────────────────────────────────────────────
"""

import asyncio
import logging
import random
import re
import time

import requests
from django.conf import settings
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright


logger = logging.getLogger(__name__)


# ── Google Places API (New) — powers the Google Maps source ────────────────
# The New API is POST-based and returns every requested field in a single
# Text Search call via the X-Goog-FieldMask header — no separate Details call.
PLACES_SEARCH_URL = 'https://places.googleapis.com/v1/places:searchText'
PLACES_PAGE_DELAY = 1.5  # brief pause between paginated requests
PLACES_FIELD_MASK = (
    'places.id,places.displayName,places.formattedAddress,'
    'places.rating,places.userRatingCount,places.businessStatus,'
    'places.nationalPhoneNumber,places.websiteUri,nextPageToken'
)

# State code → full name. We only operate in TX + GA.
_STATE_NAMES = {'TX': 'Texas', 'GA': 'Georgia'}


class ScraperError(Exception):
    """User-facing scraper failure — the message is shown in the admin UI."""


# Realistic desktop Chrome on Windows for the Playwright bar scrapers.
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/126.0.0.0 Safari/537.36'
)

# Practice areas the admin dashboard exposes as a dropdown when triggering
# a scrape. Exported here so the dropdown stays in sync with this module.
PRACTICE_AREAS = [
    'Family Law',
    'Personal Injury',
    'Criminal Defense',
    'Estate Planning',
    'Divorce',
    'DUI',
    'Immigration',
    'Business Law',
    'Real Estate Law',
    'Employment Law',
]


# ────────────────────────────────────────────────────────────────────────────
# Helpers (shared by the Playwright bar scrapers)
# ────────────────────────────────────────────────────────────────────────────

async def _jitter(min_s, max_s):
    """Sleep a random duration between min_s and max_s seconds."""
    await asyncio.sleep(random.uniform(min_s, max_s))


def _clean(text):
    """Whitespace-collapse and strip a string (or None)."""
    if not text:
        return ''
    return re.sub(r'\s+', ' ', text).strip()


async def _new_context(playwright):
    """Spawn a browser + context with realistic defaults."""
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={'width': 1366, 'height': 900},
        locale='en-US',
    )
    return browser, context


async def _safe_text(scope, selector):
    """Get text content from a selector, returning '' on miss."""
    try:
        el = await scope.query_selector(selector)
        if el is None:
            return ''
        return _clean(await el.text_content() or '')
    except Exception:
        return ''


# ────────────────────────────────────────────────────────────────────────────
# Source 1 — Google Maps (via Google Places API New)
# ────────────────────────────────────────────────────────────────────────────

def scrape_google_maps(niche, city, state, max_results=20):
    """
    Search the Google Places API (New) for businesses matching
    "{niche} in {city} {state}".

    The New Places API returns every field we need — name, address, phone,
    website, rating — in a single Text Search call via a field mask. No
    per-place Place Details call required.

    Returns a tuple: (leads, api_call_count)
        leads          — list of dicts for outreach.pipeline.import_leads
        api_call_count — total Places API requests made (for cost tracking)

    Raises ScraperError on a missing key, an API error, or zero results.

    NOTE: this function is SYNCHRONOUS (plain HTTP). The two bar scrapers
    below are async Playwright — hence the mixed style in this module.
    """
    api_key = settings.GOOGLE_PLACES_API_KEY
    if not api_key:
        raise ScraperError(
            'GOOGLE_PLACES_API_KEY is not set. Add it to .env and restart the server.'
        )

    query = f'{niche} in {city} {state}'.strip()
    logger.info('Places scrape: %s (max=%d)', query, max_results)

    headers = {
        'Content-Type': 'application/json',
        'X-Goog-Api-Key': api_key,
        'X-Goog-FieldMask': PLACES_FIELD_MASK,
    }

    api_calls = 0
    places = []
    page_token = None

    # Text Search (New) — POST, paginated (max pageSize is 20).
    while len(places) < max_results:
        body = {'textQuery': query, 'pageSize': 20}
        if page_token:
            time.sleep(PLACES_PAGE_DELAY)
            body['pageToken'] = page_token

        try:
            resp = requests.post(
                PLACES_SEARCH_URL, headers=headers, json=body, timeout=30
            )
            api_calls += 1
            data = resp.json()
        except requests.RequestException as exc:
            raise ScraperError(f'Places API request failed: {exc}')
        except ValueError:
            raise ScraperError('Places API returned malformed JSON.')

        if resp.status_code != 200:
            err = (data or {}).get('error', {})
            msg = err.get('message') or f'HTTP {resp.status_code}'
            raise ScraperError(f'Places API error: {msg}')

        batch = data.get('places', []) or []
        places.extend(batch)
        page_token = data.get('nextPageToken')
        if not page_token or not batch:
            break

    if not places:
        raise ScraperError(f'No results found for: {query}')

    places = places[:max_results]

    # Map Places API records → pipeline lead dicts.
    leads = []
    for place in places:
        business_status = place.get('businessStatus')
        if business_status and business_status != 'OPERATIONAL':
            continue

        name = ((place.get('displayName') or {}).get('text') or '').strip()
        if not name:
            continue

        address = place.get('formattedAddress', '') or ''
        place_city, place_state = _parse_city_state(
            address, fallback_city=city, fallback_state=state
        )

        leads.append({
            'firm_name': name,
            'address': address,
            'phone': place.get('nationalPhoneNumber', '') or '',
            'website': place.get('websiteUri', '') or '',
            'google_rating': place.get('rating'),
            # Key MUST be 'google_review_count' — that's what scoring.py and
            # the Lead model both use. (A 'review_count' key here silently
            # breaks lead scoring — every lead reads as 0 reviews.)
            'google_review_count': place.get('userRatingCount', 0) or 0,
            'has_google_business': True,  # listed in Places = a Google presence
            'city': place_city,
            'state': place_state,
        })

    logger.info('Places scrape: %d leads, %d API calls', len(leads), api_calls)
    return leads, api_calls


def _parse_city_state(formatted_address, fallback_city='', fallback_state=''):
    """
    Pull (city, state) out of a Google formattedAddress like
    '123 Main St, San Antonio, TX 78205, USA'. Falls back to the
    search params when the address doesn't parse cleanly.
    """
    if not formatted_address:
        return fallback_city, fallback_state
    parts = [p.strip() for p in formatted_address.split(',') if p.strip()]
    city = fallback_city
    state = fallback_state
    # Typical US form: [street, city, 'ST 12345', 'USA']
    if len(parts) >= 3:
        city = parts[-3] or fallback_city
        state_zip = parts[-2].split()
        if state_zip:
            state = _STATE_NAMES.get(state_zip[0].upper(), fallback_state)
    return city, state


# ────────────────────────────────────────────────────────────────────────────
# Source 2 — Texas State Bar attorney directory
# ────────────────────────────────────────────────────────────────────────────

async def scrape_texas_bar(city=None, practice_area=None, max_results=50):
    """
    Search the Texas State Bar attorney directory.

    Public lawyer search lives at:
        https://www.texasbar.com/AM/Template.cfm?Section=Find_A_Lawyer

    Returns a list of dicts with keys:
        attorney_name, firm_name, city, state ('Texas'), phone,
        email, practice_area, bar_number
    """
    logger.info(
        'Texas Bar scrape: city=%s practice=%s (max=%d)',
        city, practice_area, max_results,
    )
    results = []

    async with async_playwright() as p:
        browser, context = await _new_context(p)
        page = await context.new_page()

        try:
            await page.goto(
                'https://www.texasbar.com/AM/Template.cfm?Section=Find_A_Lawyer',
                wait_until='domcontentloaded',
                timeout=30000,
            )
            await _jitter(2, 4)

            # SELECTOR: search form fields. Texas Bar uses ColdFusion forms
            # with NAME attributes (not stable IDs). Verify against the live
            # page if no results come back.
            try:
                if city:
                    await page.fill('input[name="City"]', city)
                if practice_area:
                    # Some sites use a select dropdown for practice area —
                    # fall back to text input if the select isn't present.
                    select = await page.query_selector('select[name="PracticeArea"]')
                    if select:
                        await page.select_option(
                            'select[name="PracticeArea"]', label=practice_area
                        )
                    else:
                        await page.fill('input[name="PracticeArea"]', practice_area)

                # SELECTOR: submit button (form submit may be input[type=submit])
                await page.click('input[type="submit"]')
                await _jitter(3, 5)

                # SELECTOR: results list / table — adjust against live page
                await page.wait_for_selector('table.results, div.searchResults, table[class*="result"]', timeout=15000)
            except PlaywrightTimeout:
                logger.warning('Texas Bar: search form or results didn\'t load')
                return results
            except Exception:
                logger.exception('Texas Bar: form submission failed')
                return results

            # SELECTOR: result rows — VERY likely needs adjustment against live
            rows = await page.query_selector_all(
                'table.results tr, div.searchResults div.result'
            )
            logger.info('Texas Bar: found %d candidate rows', len(rows))

            for row in rows[:max_results]:
                try:
                    name = await _safe_text(row, '.name, td.name, a.attorneyName')
                    firm = await _safe_text(row, '.firm, td.firm')
                    row_city = await _safe_text(row, '.city, td.city')
                    phone = await _safe_text(row, '.phone, td.phone')
                    email = await _safe_text(row, 'a[href^="mailto:"]')
                    practice = await _safe_text(row, '.practice, td.practice')
                    bar_num = await _safe_text(row, '.bar-number, td.barNumber')

                    if not (name or firm):
                        continue

                    results.append({
                        'attorney_name': _clean(name),
                        'firm_name': _clean(firm) or _clean(name),
                        'city': _clean(row_city) or (city or ''),
                        'state': 'Texas',
                        'phone': _clean(phone),
                        'email': _clean(email),
                        'practice_area': _clean(practice) or (practice_area or ''),
                        'bar_number': _clean(bar_num),
                    })
                except Exception:
                    logger.exception('Texas Bar: failed to extract one row')
                    continue

        finally:
            await browser.close()

    logger.info('Texas Bar scrape returned %d records', len(results))
    return results


# ────────────────────────────────────────────────────────────────────────────
# Source 3 — Georgia State Bar attorney directory
# ────────────────────────────────────────────────────────────────────────────

async def scrape_georgia_bar(city=None, practice_area=None, max_results=50):
    """
    Search the Georgia State Bar attorney directory.

    Member search lives at:
        https://www.gabar.org/MemberSearchForm.cfm

    Returns a list of dicts with keys:
        attorney_name, firm_name, city, state ('Georgia'), phone,
        email, practice_area, bar_number
    """
    logger.info(
        'Georgia Bar scrape: city=%s practice=%s (max=%d)',
        city, practice_area, max_results,
    )
    results = []

    async with async_playwright() as p:
        browser, context = await _new_context(p)
        page = await context.new_page()

        try:
            await page.goto(
                'https://www.gabar.org/MemberSearchForm.cfm',
                wait_until='domcontentloaded',
                timeout=30000,
            )
            await _jitter(2, 4)

            # SELECTOR: gabar.org form field names — verify against live page
            try:
                if city:
                    await page.fill('input[name="City"]', city)
                if practice_area:
                    select = await page.query_selector('select[name="PracticeArea"]')
                    if select:
                        await page.select_option(
                            'select[name="PracticeArea"]', label=practice_area
                        )
                await page.click('input[type="submit"]')
                await _jitter(3, 5)
                await page.wait_for_selector('table.results, div.searchResults, table[class*="member"]', timeout=15000)
            except PlaywrightTimeout:
                logger.warning('Georgia Bar: search form or results didn\'t load')
                return results
            except Exception:
                logger.exception('Georgia Bar: form submission failed')
                return results

            rows = await page.query_selector_all(
                'table.results tr, div.searchResults div.result, table.member-list tr'
            )
            logger.info('Georgia Bar: found %d candidate rows', len(rows))

            for row in rows[:max_results]:
                try:
                    name = await _safe_text(row, '.name, td.name, a.memberName')
                    firm = await _safe_text(row, '.firm, td.firm')
                    row_city = await _safe_text(row, '.city, td.city')
                    phone = await _safe_text(row, '.phone, td.phone')
                    email = await _safe_text(row, 'a[href^="mailto:"]')
                    practice = await _safe_text(row, '.practice, td.practice')
                    bar_num = await _safe_text(row, '.bar-number, td.barNumber')

                    if not (name or firm):
                        continue

                    results.append({
                        'attorney_name': _clean(name),
                        'firm_name': _clean(firm) or _clean(name),
                        'city': _clean(row_city) or (city or ''),
                        'state': 'Georgia',
                        'phone': _clean(phone),
                        'email': _clean(email),
                        'practice_area': _clean(practice) or (practice_area or ''),
                        'bar_number': _clean(bar_num),
                    })
                except Exception:
                    logger.exception('Georgia Bar: failed to extract one row')
                    continue

        finally:
            await browser.close()

    logger.info('Georgia Bar scrape returned %d records', len(results))
    return results


# ────────────────────────────────────────────────────────────────────────────
# Sync entry points — for Celery tasks / Django views
# ────────────────────────────────────────────────────────────────────────────

def scrape_google_maps_sync(niche, city, state, max_results=20):
    # scrape_google_maps is already synchronous (Places API HTTP calls).
    # Kept as a *_sync name so callers have a consistent interface.
    return scrape_google_maps(niche, city, state, max_results)


def scrape_texas_bar_sync(city=None, practice_area=None, max_results=50):
    return asyncio.run(scrape_texas_bar(city, practice_area, max_results))


def scrape_georgia_bar_sync(city=None, practice_area=None, max_results=50):
    return asyncio.run(scrape_georgia_bar(city, practice_area, max_results))
