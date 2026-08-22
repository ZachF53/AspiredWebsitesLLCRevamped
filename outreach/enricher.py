"""
Lead enrichment — runs AFTER a lead is scraped + imported, fills in the
fields Google Places API doesn't surface.

Two passes:

  1. Homepage scrape (free, runs whenever ``lead.website`` is set).
     Fetches the HTML, parses out:
       - Email addresses (mailto: hrefs + a name@domain.tld regex on
         visible text; first non-noreply, non-no-reply hit wins).
       - Social URLs (facebook.com, instagram.com, linkedin.com,
         twitter|x.com, tiktok.com, youtube.com).
       - SSL flag (resolved by whether https:// returned 200).
       - Copyright year (parsed from a footer "© YYYY" pattern).
       - Generic-email flag (gmail/yahoo/hotmail/aol/outlook).

  2. Google PageSpeed Insights run (free, requires
     ``GOOGLE_PAGESPEED_API_KEY``). Writes
     ``website_performance_score``, ``website_seo_score``,
     ``website_mobile_score`` (PageSpeed reports four categories;
     mobile-strategy is what real users see, so we always request
     mobile).

  3. Brave Search API fallback (free 2000/mo, then $3/1000).
     Triggered only when ``lead.website`` is blank after the scrape.
     Reads ``settings.BRAVE_SEARCH_API_KEY``; quietly skipped when
     unset so the pipeline keeps working in pre-config environments.
     Makes up to 3 queries per lead:
       a. "{firm_name} {city} {state}" → first organic hit becomes
          the website (if it's not a directory like yelp.com /
          facebook.com / etc.).
       b. "{firm_name} {city} facebook" → first facebook.com/...
          hit becomes facebook_url.
       c. "{firm_name} {city} instagram" → first instagram.com/...
          hit becomes instagram_url.
     If query (a) finds a website we recurse into pass 1 + 2 against
     it so the lead ends up with the same data as it would have if
     Google Places had returned a website.

     History: this slot has been through Google Custom Search
     (deprecated 'search the entire web' in 2025) and a brief stop
     at DuckDuckGo / Bing HTML scraping (both walled off behind JS
     anti-bot in 2025-2026). Brave's API is a clean JSON endpoint
     and they actively want this use case.

Every step is best-effort and isolated — a failed PageSpeed call
must not skip the email extraction etc. Each step appends a short
status line to ``lead.enrichment_log`` so the admin can see why a
field is blank.
"""

import html as _html
import logging
import re
import time
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Tuning constants ────────────────────────────────────────────────────────

HTTP_TIMEOUT = 12
USER_AGENT = (
    'Mozilla/5.0 (compatible; AspiredWebsitesBot/1.0; '
    '+https://aspiredwebsites.com/bot)')

PAGESPEED_URL = 'https://www.googleapis.com/pagespeedonline/v5/runPagespeed'
PAGESPEED_TIMEOUT = 60

# Brave Search API — JSON endpoint, returns up to 20 results per
# query. Free tier is 2000 queries/month at 1 req/sec.
BRAVE_SEARCH_URL = 'https://api.search.brave.com/res/v1/web/search'
BRAVE_TIMEOUT = 15
# Cap queries per lead so a 100-lead batch never blows through the
# 2000/mo free quota in one burst. Three (website / facebook /
# instagram) is the realistic ceiling.
WEB_SEARCH_MAX_PER_LEAD = 3
# Brave's free tier rate-limits to 1 req/sec — sleep between
# queries for the same lead to stay under it.
BRAVE_INTER_QUERY_DELAY = 1.1

# Domains we treat as "not a real website" when picking the org's
# homepage from Custom Search results — they're directories or
# social profiles, not the firm's own site.
DIRECTORY_DOMAINS = frozenset({
    'yelp.com', 'facebook.com', 'instagram.com', 'linkedin.com',
    'twitter.com', 'x.com', 'tiktok.com', 'youtube.com',
    'pinterest.com', 'maps.google.com', 'business.google.com',
    'google.com', 'bing.com', 'duckduckgo.com', 'yellowpages.com',
    'angi.com', 'angieslist.com', 'bbb.org', 'wikipedia.org',
    'manta.com', 'whitepages.com', 'thumbtack.com', 'foursquare.com',
    'tripadvisor.com', 'opentable.com', 'grubhub.com', 'doordash.com',
    'healthgrades.com', 'zocdoc.com', 'webmd.com', 'vitals.com',
    'avvo.com', 'justia.com', 'findlaw.com', 'lawyers.com',
    'martindale.com', 'super-lawyers.com',
})

# Free email providers — finding info@gmail.com on a business site
# signals "no real IT investment", which is a scoring + pitch hook.
GENERIC_EMAIL_DOMAINS = frozenset({
    'gmail.com', 'yahoo.com', 'hotmail.com', 'aol.com', 'outlook.com',
    'icloud.com', 'mail.com', 'protonmail.com', 'live.com', 'msn.com',
})

# Skip these "fake" emails — placeholders on website templates.
EMAIL_NOISE_PATTERNS = (
    'noreply@', 'no-reply@', 'donotreply@', 'example@', 'your@',
    'wixsite.com', '@sentry.io', 'wordpress.com', '@2x.', '.png', '.jpg',
)

# Regex pre-compiled at import time — these hit hot during enrichment.
EMAIL_RE = re.compile(
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
COPYRIGHT_RE = re.compile(
    r'(?:©|&copy;|copyright)\s*(?:&#\d+;)?\s*(\d{4})',
    re.IGNORECASE)
SOCIAL_PATTERNS = {
    'facebook_url': re.compile(
        r'https?://(?:www\.)?facebook\.com/[A-Za-z0-9_.\-/?#=&]+',
        re.IGNORECASE),
    'instagram_url': re.compile(
        r'https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.\-/?#=&]+',
        re.IGNORECASE),
    'linkedin_url': re.compile(
        r'https?://(?:www\.)?linkedin\.com/(?:company|in|pub)/'
        r'[A-Za-z0-9_.\-/?#=&%]+',
        re.IGNORECASE),
}
OTHER_SOCIAL_PATTERNS = {
    'twitter': re.compile(
        r'https?://(?:www\.)?(?:twitter\.com|x\.com)/[A-Za-z0-9_]+',
        re.IGNORECASE),
    'tiktok': re.compile(
        r'https?://(?:www\.)?tiktok\.com/@[A-Za-z0-9_.\-]+',
        re.IGNORECASE),
    'youtube': re.compile(
        r'https?://(?:www\.)?youtube\.com/(?:channel|c|user|@)/?'
        r'[A-Za-z0-9_.\-]+',
        re.IGNORECASE),
}


# ── Public entry point ─────────────────────────────────────────────────────

def enrich_lead(lead):
    """
    Synchronous orchestrator. Updates ``lead`` in-place and saves it.

    Idempotent — re-running on a lead that's already been enriched
    overwrites the fields with fresh data. Safe to call from a Celery
    task OR directly from the shell during debugging.

    Returns the lead row for chaining / testability.
    """
    log_lines = []
    log_lines.append(f'[{timezone.now():%Y-%m-%d %H:%M:%S}] enrichment started')
    lead.enrichment_attempted_at = timezone.now()
    # Save the attempt marker first so a crash mid-enrichment is visible.
    lead.save(update_fields=['enrichment_attempted_at', 'updated_at'])

    # Pass 1: homepage scrape (only if we have a website)
    if lead.website:
        log_lines.append(
            f'Scraping homepage: {lead.website}')
        try:
            _scrape_homepage(lead)
            log_lines.append('  homepage scrape: OK')
        except Exception as exc:  # noqa: BLE001
            log_lines.append(f'  homepage scrape: FAILED ({exc})')
            logger.exception('enrich_lead homepage scrape failed for %s',
                             lead.pk)

        # Pass 2: PageSpeed (still gated on website being set)
        log_lines.append(f'Running PageSpeed: {lead.website}')
        try:
            _run_pagespeed(lead)
            log_lines.append(
                f'  PageSpeed: perf={lead.website_performance_score} '
                f'seo={lead.website_seo_score} '
                f'mobile={lead.website_mobile_score}')
        except Exception as exc:  # noqa: BLE001
            log_lines.append(f'  PageSpeed: FAILED ({exc})')
            logger.exception('enrich_lead PageSpeed failed for %s', lead.pk)

    else:
        # Pass 3: Brave Search API fallback for no-website leads.
        log_lines.append(
            'No website on Places result → searching Brave')
        try:
            found_website = _web_search_fallback(lead, log_lines)
            if found_website:
                # Recurse: now that we have a website, do homepage scrape
                # + PageSpeed against it. Refresh lead from DB so the
                # recursion sees the website we just wrote.
                lead.refresh_from_db()
                log_lines.append(
                    f'  → found website, re-running homepage + PageSpeed')
                try:
                    _scrape_homepage(lead)
                    log_lines.append('  homepage scrape: OK')
                except Exception as exc:  # noqa: BLE001
                    log_lines.append(
                        f'  homepage scrape: FAILED ({exc})')
                try:
                    _run_pagespeed(lead)
                    log_lines.append(
                        f'  PageSpeed: perf={lead.website_performance_score}')
                except Exception as exc:  # noqa: BLE001
                    log_lines.append(f'  PageSpeed: FAILED ({exc})')
        except Exception as exc:  # noqa: BLE001
            log_lines.append(f'  Brave search: FAILED ({exc})')
            logger.exception('enrich_lead Brave search failed for %s',
                             lead.pk)

    # Re-score now that enrichment has filled in PageSpeed, SSL,
    # socials, generic-email + copyright-year signals. The initial
    # score set at import-time only saw what the scraper returned
    # (firm name + Google rating + website URL) — re-running scoring
    # here is what makes "5 — warm" become "8 — hot" once PageSpeed
    # shows the site is dog-slow.
    old_score = lead.score
    new_score, new_temp = _rescore_from_model(lead)
    if new_score != old_score:
        log_lines.append(
            f'  score: {old_score} → {new_score} '
            f'({lead.temperature} → {new_temp})')
    lead.score = new_score
    lead.temperature = new_temp

    lead.enrichment_completed_at = timezone.now()
    log_lines.append(
        f'[{timezone.now():%Y-%m-%d %H:%M:%S}] enrichment completed')
    lead.enrichment_log = '\n'.join(log_lines)
    lead.save()  # full save — every enrichment path may have touched any field
    return lead


def _rescore_from_model(lead):
    """
    Recompute (score, temperature) from a Lead model instance.

    ``score_lead`` was written to take the scraper dict shape, so this
    shim copies the relevant fields off the model into a dict and
    delegates. Keep the field list in sync with score_lead's reads.
    """
    from outreach.scoring import score_lead

    return score_lead({
        'website_performance_score': lead.website_performance_score,
        'website':                   lead.website,
        'has_google_business':       lead.has_google_business,
        'google_review_count':       lead.google_review_count,
        'facebook_url':              lead.facebook_url,
        'instagram_url':             lead.instagram_url,
        'linkedin_url':              lead.linkedin_url,
        'has_ssl':                   lead.has_ssl,
        'has_generic_email':         lead.has_generic_email,
        'copyright_year':            lead.copyright_year,
    })


# ── Pass 1: homepage scrape ────────────────────────────────────────────────

def _scrape_homepage(lead):
    """Fetch the lead's website + a couple obvious contact pages and
    pull out email + social URLs + the misc free signals.

    Tries https:// first, falls back to http:// — `has_ssl` records
    whether the https attempt succeeded.

    Mutates `lead` in place; caller is responsible for save().
    """
    base_url = (lead.website or '').strip()
    if not base_url:
        return

    # SSL probe — try https first.
    https_ok = False
    if base_url.startswith('https://'):
        html, final_url, ok = _http_get(base_url)
        https_ok = ok
    elif base_url.startswith('http://'):
        # Try https on the same host first; if it works, prefer it +
        # update the lead's website to the upgraded URL.
        upgrade = 'https://' + base_url[len('http://'):]
        html, final_url, ok = _http_get(upgrade)
        if ok:
            https_ok = True
            lead.website = upgrade
        else:
            html, final_url, ok = _http_get(base_url)
    else:
        # No scheme — assume https.
        upgrade = 'https://' + base_url
        html, final_url, ok = _http_get(upgrade)
        if ok:
            https_ok = True
            lead.website = upgrade
        else:
            html, final_url, ok = _http_get('http://' + base_url)
    lead.has_ssl = https_ok

    if not html:
        return

    # Extract from the homepage. Then walk a wider set of likely
    # contact-bearing pages, stopping the moment we land an email.
    # Order is by hit-rate empirically: /contact* first, then /about*,
    # then law-firm specifics, then catch-alls.
    _extract_from_html(lead, html, final_url)
    if not lead.email:
        candidate_paths = (
            '/contact', '/contact-us', '/contact.html', '/contact.php',
            '/about', '/about-us',
            '/team', '/our-team', '/staff', '/attorneys', '/lawyers',
            '/get-in-touch', '/connect',
        )
        for path in candidate_paths:
            extra_html, _, _ = _http_get(urljoin(final_url, path))
            if extra_html:
                _extract_from_html(lead, extra_html, final_url)
                if lead.email:
                    break


def _http_get(url):
    """Fetch a URL with realistic headers. Returns (html, final_url, ok).
    Never raises — caller branches on ``ok``."""
    if not url:
        return '', '', False
    try:
        resp = requests.get(
            url,
            headers={
                'User-Agent': USER_AGENT,
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            timeout=HTTP_TIMEOUT,
            allow_redirects=True,
        )
        if resp.status_code != 200 or not resp.text:
            return '', url, False
        # Hard cap on HTML size — some sites serve 5MB pages; we only
        # need the first chunk for emails + socials + footer.
        return resp.text[:400_000], resp.url, True
    except requests.RequestException:
        return '', url, False


def _extract_from_html(lead, raw_html, base_url):
    """Pull email, social URLs, copyright year out of a single page's
    HTML. Mutates lead in place; only fills fields that aren't set
    yet (so /contact extraction doesn't overwrite a homepage email)."""
    # Decode entities BEFORE any pattern runs. Plenty of small-business
    # sites emit `info&#64;firm&#46;com`, `&amp;` inside query strings, or
    # `&#169; 2019` in the footer. Against the raw markup EMAIL_RE misses
    # the address entirely and COPYRIGHT_RE misses the year — both read as
    # "no signal" and quietly cost us a contactable lead.
    html = _html.unescape(raw_html or '')
    # Email — try in order of reliability:
    #   1. mailto: hrefs        (most reliable — never obfuscated)
    #   2. plain regex          (catches anything in body text)
    #   3. Cloudflare-encoded   (data-cfemail="abcdef..." or hash links)
    #   4. obfuscated formats   (info [at] domain [dot] com etc.)
    if not lead.email:
        candidates = []
        candidates.extend(re.findall(
            r'mailto:([^"\'>\s?]+)', html, re.IGNORECASE))
        candidates.extend(EMAIL_RE.findall(html))
        candidates.extend(_decode_cloudflare_emails(html))
        candidates.extend(_deobfuscate_emails(html))
        for raw in candidates:
            email = raw.strip().lower()
            # mailto: hrefs and the de-obfuscators hand back whatever was
            # between the delimiters, which is often not just an address:
            # "info@firm.com?subject=Hello", trailing punctuation, or a
            # whole sentence. EMAIL_RE alone is a *search*, so those pass.
            # fullmatch is the guard — the winning candidate must BE an
            # address end to end, not merely contain one. A malformed
            # address here becomes a hard bounce and a suppression entry.
            email = email.split('?', 1)[0].strip().strip('.,;:<>()[]"\'')
            if not EMAIL_RE.fullmatch(email):
                continue
            if not _is_real_email(email):
                continue
            lead.email = email
            domain = email.rsplit('@', 1)[-1]
            lead.has_generic_email = (domain in GENERIC_EMAIL_DOMAINS)
            break

    # Socials — first hit per pattern wins.
    for field, pattern in SOCIAL_PATTERNS.items():
        if getattr(lead, field):
            continue
        m = pattern.search(html)
        if m:
            url = m.group(0).rstrip(')."\',;')
            # Strip sharer / login / plugins URLs — they're not the
            # firm's own page.
            if any(bad in url.lower() for bad in (
                    'sharer', 'plugins', 'login', 'oauth', 'tr?id=')):
                continue
            setattr(lead, field, url)

    # Other socials → JSON list, deduplicated.
    other = list(lead.other_social_urls or [])
    for _, pattern in OTHER_SOCIAL_PATTERNS.items():
        m = pattern.search(html)
        if m:
            url = m.group(0).rstrip(')."\',;')
            if url not in other:
                other.append(url)
    if other != (lead.other_social_urls or []):
        lead.other_social_urls = other

    # Copyright year — last match (footer wins over a stray quote up top).
    if lead.copyright_year is None:
        years = COPYRIGHT_RE.findall(html)
        for raw in reversed(years):
            try:
                year = int(raw)
                # Sanity: any year between 1990 and 2 years out.
                if 1990 <= year <= timezone.now().year + 1:
                    lead.copyright_year = year
                    break
            except ValueError:
                continue


def _is_real_email(email):
    """Filter placeholder / image-filename / Wix-trash matches."""
    if '@' not in email:
        return False
    if any(noise in email for noise in EMAIL_NOISE_PATTERNS):
        return False
    # Image/CSS file names containing "@2x" or extensions get caught
    # by the regex if a stray dot follows. Belt-and-suspenders.
    if email.split('@', 1)[-1].count('.') == 0:
        return False
    return True


# Cloudflare's email protection wraps real addresses in a hex blob,
# either as data-cfemail="abcdef..." on a span OR as a link to
# /cdn-cgi/l/email-protection#abcdef... The decoding scheme: first
# byte is an XOR key; remaining bytes are key-XOR'd ASCII codepoints.
# See https://usamaejaz.com/cloudflare-email-decoder/
_CFEMAIL_RE = re.compile(
    r'(?:data-cfemail="|/cdn-cgi/l/email-protection#)([a-fA-F0-9]+)')


def _decode_cloudflare_emails(html):
    """Yield decoded emails from every Cloudflare-protected token in html."""
    out = []
    for hex_blob in _CFEMAIL_RE.findall(html):
        try:
            key = int(hex_blob[:2], 16)
            chars = []
            for i in range(2, len(hex_blob), 2):
                byte = int(hex_blob[i:i + 2], 16)
                chars.append(chr(byte ^ key))
            out.append(''.join(chars))
        except (ValueError, IndexError):
            continue
    return out


# Common obfuscation patterns small businesses use to hide email
# from scrapers. Each regex captures (local, domain) groups; we
# rebuild the address from those. Whitespace tolerant + case
# insensitive.
_OBFUSCATED_PATTERNS = [
    # foo [at] bar [dot] com
    re.compile(
        r'([a-zA-Z0-9._%+-]+)\s*[\[(]?\s*at\s*[\])]?\s*'
        r'([a-zA-Z0-9.-]+)\s*[\[(]?\s*dot\s*[\])]?\s*'
        r'([a-zA-Z]{2,})',
        re.IGNORECASE,
    ),
    # foo (at) bar (dot) com
    re.compile(
        r'([a-zA-Z0-9._%+-]+)\s*\(at\)\s*'
        r'([a-zA-Z0-9.-]+)\s*\(dot\)\s*'
        r'([a-zA-Z]{2,})',
        re.IGNORECASE,
    ),
    # foo AT bar DOT com (all caps separators)
    re.compile(
        r'([a-zA-Z0-9._%+-]+)\s+AT\s+([a-zA-Z0-9.-]+)\s+DOT\s+([a-zA-Z]{2,})',
    ),
]


def _deobfuscate_emails(html):
    """Yield assembled emails from obfuscation patterns in html."""
    out = []
    for pat in _OBFUSCATED_PATTERNS:
        for local, dom, tld in pat.findall(html):
            out.append(f'{local}@{dom}.{tld}')
    return out


# ── Pass 2: PageSpeed Insights ─────────────────────────────────────────────

def _run_pagespeed(lead):
    """Run PageSpeed mobile audit and write the 3 score fields + the
    issues list. Quiet no-op when no API key is configured."""
    if not lead.website:
        return
    api_key = getattr(settings, 'GOOGLE_PAGESPEED_API_KEY', '')
    if not api_key:
        return

    params = [
        ('url', lead.website),
        ('strategy', 'mobile'),
        ('category', 'PERFORMANCE'),
        ('category', 'SEO'),
        ('category', 'BEST_PRACTICES'),
        ('category', 'ACCESSIBILITY'),
        ('key', api_key),
    ]
    try:
        resp = requests.get(
            PAGESPEED_URL, params=params, timeout=PAGESPEED_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f'PageSpeed request error: {exc}')
    if resp.status_code != 200:
        # 400 = unreachable URL, 429 = quota — neither is worth retrying
        # inline. Skip silently; the scorer treats missing data as zero.
        raise RuntimeError(f'PageSpeed HTTP {resp.status_code}')

    data = resp.json()
    cats = (data.get('lighthouseResult') or {}).get('categories', {})

    def _pct(key):
        score = (cats.get(key) or {}).get('score')
        if score is None:
            return None
        try:
            return int(round(float(score) * 100))
        except (TypeError, ValueError):
            return None

    lead.website_performance_score = _pct('performance')
    lead.website_seo_score = _pct('seo')
    # Best practices is the closest to "mobile" in our 3-column slot.
    lead.website_mobile_score = _pct('best-practices')
    lead.audit_run_at = timezone.now()

    # First 5 high-impact audits → website_issues for the lead detail
    # page. Keep it short — admin glances, they don't read.
    audits = (data.get('lheighthouseResult') or
              data.get('lighthouseResult') or {}).get('audits', {})
    issues = []
    for slug, audit in audits.items():
        score = audit.get('score')
        if score is None or score >= 0.9:
            continue
        title = audit.get('title') or slug
        issues.append({'slug': slug, 'title': title,
                       'score': int(round(float(score) * 100))})
        if len(issues) >= 5:
            break
    lead.website_issues = issues


# ── Pass 3: Brave Search API fallback ──────────────────────────────────────

def _web_search_fallback(lead, log_lines):
    """For leads with no website on Places, query Brave Search to find
    a website + social URLs. Returns the new website URL (or '').

    Uses Brave instead of Google CSE because Google deprecated
    'search the entire web' on the CSE side in 2025, and the per-engine
    site-restricted alternative is useless for finding unknown
    businesses. Brave Search API returns clean JSON, has a
    2000-query/mo free tier, and actively supports this use case.
    """
    api_key = getattr(settings, 'BRAVE_SEARCH_API_KEY', '')
    if not api_key:
        log_lines.append(
            '  Brave search: skipped (BRAVE_SEARCH_API_KEY not set)')
        return ''

    name = (lead.firm_name or '').strip()
    if not name:
        return ''
    loc_bits = [b for b in (lead.city, lead.state) if b]
    location = ' '.join(loc_bits)

    queries_made = 0
    found_website = ''

    # Every hit below is verified against the lead before it is stored.
    # Taking the first result on faith is what produced a ~50% wrong-
    # business rate on the Facebook path — see _matches_business.

    # Query 1: find the firm's website.
    q1 = f'{name} {location}'.strip()
    if queries_made < WEB_SEARCH_MAX_PER_LEAD:
        results = _brave_search(q1, api_key)
        queries_made += 1
        log_lines.append(f'  Q1 "{q1}" → {len(results)} hits')
        for hit in results:
            url = hit['url']
            domain = _domain_of(url)
            if not domain:
                continue
            ok, why = _matches_business(hit, lead)
            if _is_directory_domain(domain):
                if not ok:
                    log_lines.append(f'    rejected {domain}: {why}')
                    continue
                # Capture the social profile in passing — match on
                # base domain so e.g. m.facebook.com still counts.
                base = _base_domain(domain)
                if base == 'facebook.com' and not lead.facebook_url:
                    lead.facebook_url = url
                    log_lines.append(f'    facebook ✓ ({why})')
                elif base == 'instagram.com' and not lead.instagram_url:
                    lead.instagram_url = url
                    log_lines.append(f'    instagram ✓ ({why})')
                elif base == 'linkedin.com' and not lead.linkedin_url:
                    lead.linkedin_url = url
                    log_lines.append(f'    linkedin ✓ ({why})')
                continue
            if not ok:
                log_lines.append(f'    rejected website {domain}: {why}')
                continue
            # First VERIFIED non-directory hit becomes the website.
            found_website = url
            lead.website = url
            log_lines.append(f'    website ✓ {domain} ({why})')
            break

    # Query 2: Facebook (only if we haven't found one yet).
    if (not lead.facebook_url
            and queries_made < WEB_SEARCH_MAX_PER_LEAD):
        time.sleep(BRAVE_INTER_QUERY_DELAY)
        q2 = f'{name} {location} facebook'.strip()
        results = _brave_search(q2, api_key)
        queries_made += 1
        log_lines.append(f'  Q2 "{q2}" → {len(results)} hits')
        for hit in results:
            url = hit['url']
            if (_base_domain(_domain_of(url)) == 'facebook.com'
                    and '/sharer' not in url
                    and '/plugins' not in url):
                ok, why = _matches_business(hit, lead)
                if not ok:
                    log_lines.append(f'    rejected {url[:60]}: {why}')
                    continue
                lead.facebook_url = url
                log_lines.append(f'    facebook ✓ ({why})')
                break

    # Query 3: Instagram (only if we haven't found one yet).
    if (not lead.instagram_url
            and queries_made < WEB_SEARCH_MAX_PER_LEAD):
        time.sleep(BRAVE_INTER_QUERY_DELAY)
        q3 = f'{name} {location} instagram'.strip()
        results = _brave_search(q3, api_key)
        queries_made += 1
        log_lines.append(f'  Q3 "{q3}" → {len(results)} hits')
        for hit in results:
            url = hit['url']
            if _base_domain(_domain_of(url)) == 'instagram.com':
                ok, why = _matches_business(hit, lead)
                if not ok:
                    log_lines.append(f'    rejected {url[:60]}: {why}')
                    continue
                lead.instagram_url = url
                log_lines.append(f'    instagram ✓ ({why})')
                break

    log_lines.append(
        f'  Brave: {queries_made} query(ies) used (free tier '
        f'2000/mo)')
    # Persist whatever we found so far. Caller will save() again, but
    # writing now means a crash leaves correct partial data behind.
    lead.save()
    return found_website


def _brave_search(query, api_key, count=10):
    """One Brave Search call. Returns a list of result URLs (just URLs,
    not dicts — keeps the downstream loop simple). Returns [] on any
    error; caller treats empty as 'no results'."""
    try:
        resp = requests.get(
            BRAVE_SEARCH_URL,
            params={
                'q': query,
                'count': count,
                'country': 'us',
                'safesearch': 'moderate',
                # Asking only for web results keeps the response small
                # — we ignore news/videos/etc. anyway.
                'result_filter': 'web',
            },
            headers={
                'Accept': 'application/json',
                'Accept-Encoding': 'gzip',
                'X-Subscription-Token': api_key,
            },
            timeout=BRAVE_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning('Brave request failed for %r: %s', query, exc)
        return []
    if resp.status_code != 200:
        # 401 = bad key, 429 = quota / rate-limit, 422 = empty query.
        # Log + return empty; caller handles "no results" gracefully.
        logger.warning('Brave HTTP %s for %r: %s',
                       resp.status_code, query, resp.text[:200])
        return []

    # Count the query against this month's usage — drives the banner
    # on /admin-dashboard/leads/. Only counts successful (HTTP 200)
    # calls since failures don't draw from Brave's quota. Wrapped in
    # try/except so a DB hiccup never breaks the actual enrichment.
    try:
        from outreach.models import BraveSearchUsage
        BraveSearchUsage.increment()
    except Exception:
        logger.exception('Failed to increment BraveSearchUsage')

    try:
        data = resp.json()
    except ValueError:
        return []

    results = (data.get('web') or {}).get('results') or []
    hits = []
    seen = set()
    for r in results:
        url = (r.get('url') or '').strip()
        if not url:
            continue
        if not url.startswith(('http://', 'https://')):
            continue
        if url in seen:
            continue
        seen.add(url)
        # Title and description are carried because a URL alone cannot
        # be verified against the business it is supposed to belong to.
        # See _matches_business.
        hits.append({
            'url': url,
            'title': (r.get('title') or '').strip(),
            'description': (r.get('description') or '').strip(),
        })
    return hits


# ── Match verification ─────────────────────────────────────────────────
#
# WHY THIS EXISTS
# ---------------
# _web_search_fallback used to take the first facebook.com hit for
# "<name> <city> facebook" and store it as the lead's Facebook page, with
# no check that the page belonged to the business. Measured against 20
# phone-verifiable pairs on 2026-08-22, that was wrong ~50% of the time:
#
#   Godwin Law Office   -> Goodwin & Goodwin, LLP | Charleston
#   Gonzalez Raul A     -> raul.gonzalez@traviscountytx.gov
#   Family Vital PC     -> Vital Interaction | Austin
#
# A wrong social URL is not cosmetic here. It feeds the scorer, and it
# feeds the icebreaker as a "verified observation" — which is how an
# invented fact reaches a prospect.

# Words that carry no identifying signal, so "Law Office of Sarah Chen"
# and "Sarah Chen Law" compare as the same business.
_GENERIC_NAME_TOKENS = frozenset({
    'law', 'laws', 'lawyer', 'lawyers', 'attorney', 'attorneys', 'legal',
    'office', 'offices', 'firm', 'firms', 'group', 'associates',
    'associate', 'partners', 'partner', 'practice', 'services', 'service',
    'company', 'co', 'inc', 'corp', 'corporation', 'llc', 'pllc', 'llp',
    'lp', 'pc', 'pa', 'ltd', 'the', 'of', 'and', 'a', 'an', 'at', 'for',
    'dental', 'dentistry', 'dentist', 'dds', 'md', 'clinic', 'center',
    'centre', 'health', 'medical', 'care', 'cpa', 'accounting',
    'accountants', 'consulting', 'consultants', 'solutions',
})

_NAME_CLEAN_RE = re.compile(r'[^a-z0-9\s]')

# Tuned against the measured sample: 0.65 accepted every true pair and
# rejected every phone-disproved one. Deliberately strict — a missing
# Facebook URL costs one signal, a wrong one poisons the copy.
_NAME_SIMILARITY_THRESHOLD = 0.65


def _name_tokens(name):
    """Distinctive lowercase tokens from a business name."""
    cleaned = _NAME_CLEAN_RE.sub(' ', (name or '').lower())
    return [t for t in cleaned.split()
            if t and t not in _GENERIC_NAME_TOKENS]


def _phone_digits(value):
    digits = re.sub(r'\D', '', value or '')
    return digits[-10:] if len(digits) >= 10 else ''


def _matches_business(hit, lead):
    """Whether a search hit plausibly belongs to this lead.

    Three tests, cheapest and most reliable first:

    1. **Phone** — if the lead has a phone and the same last 10 digits
       appear in the hit's text, it is the same business with near
       certainty. This is the test that exposed the 50% error rate.
    2. **Distinctive tokens** — every identifying word of the firm name
       present in the hit's title. Handles "Law Office of X" vs "X Law".
    3. **String similarity** — fuzzy fallback for spelling drift.

    Returns (bool, reason) so the enrichment log records *why* a match
    was accepted or rejected.
    """
    haystack = f"{hit.get('title', '')} {hit.get('description', '')}".lower()

    lead_phone = _phone_digits(lead.phone)
    if lead_phone:
        if lead_phone in re.sub(r'\D', '', haystack):
            return True, 'phone match'
        # A hit that shows a DIFFERENT phone for a named business is
        # positive evidence against it, not merely absent evidence.
        hit_phones = re.findall(r'\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}',
                                haystack)
        if hit_phones and not any(
                _phone_digits(p) == lead_phone for p in hit_phones):
            return False, 'phone mismatch'

    tokens = _name_tokens(lead.firm_name)
    title = (hit.get('title') or '').lower()
    if tokens:
        title_blob = _NAME_CLEAN_RE.sub(' ', title)
        if all(t in title_blob for t in tokens):
            return True, 'all distinctive name tokens present'

    ratio = SequenceMatcher(
        None, ' '.join(tokens), ' '.join(_name_tokens(title))).ratio()
    if ratio >= _NAME_SIMILARITY_THRESHOLD:
        return True, f'name similarity {ratio:.2f}'

    return False, f'name similarity {ratio:.2f} below threshold'


def _domain_of(url):
    """Bare domain of a URL ('www.facebook.com' → 'facebook.com').
    Keeps non-www subdomains intact ('doctor.webmd.com' stays that way)
    so callers can decide whether to collapse to the registered domain
    via ``_base_domain``."""
    try:
        host = urlparse(url).hostname or ''
        host = host.lower()
        if host.startswith('www.'):
            host = host[4:]
        return host
    except Exception:
        return ''


def _base_domain(domain):
    """Strip ALL subdomains to the (rough) registered domain.

    Cheap heuristic: drop everything before the last two dot-separated
    pieces. Works for ``doctor.webmd.com → webmd.com``,
    ``m.facebook.com → facebook.com``, ``api.search.brave.com →
    brave.com``. Wrong for .co.uk / .com.au and similar two-piece
    public suffixes — but we only use this for the small
    DIRECTORY_DOMAINS allow-list which is all .com / .org, so the
    edge case never bites in practice. ``tldextract`` would do it
    perfectly but it's another dep for one helper.
    """
    if not domain:
        return ''
    parts = domain.split('.')
    if len(parts) <= 2:
        return domain
    return '.'.join(parts[-2:])


def _is_directory_domain(domain):
    """True when the host is a known directory / aggregator (yelp,
    healthgrades, webmd, avvo, …) we'd never want to pick as the
    firm's 'website'. Matches both the exact domain AND any
    subdomain of one — ``doctor.webmd.com`` resolves to True even
    though only ``webmd.com`` is in DIRECTORY_DOMAINS."""
    if not domain:
        return False
    if domain in DIRECTORY_DOMAINS:
        return True
    return _base_domain(domain) in DIRECTORY_DOMAINS
