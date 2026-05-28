"""
Lead scoring — pure function, no DB access.

Score 0-10 from prospect-quality signals. Higher = hotter = higher priority.
Temperature is derived from the score band so the CRM can color-code.

Signals (and why they matter for a web-design agency targeting lawyers + SMBs):
    - No website at all          → biggest opportunity (can't lose to existing site)
    - Bad website performance    → real pain we can fix and demo with metrics
    - No Google Business Profile → easy local-SEO win we can pitch
    - Few/no Google reviews      → suggests an under-managed online presence
    - No social media            → another adjacent service we can offer
"""

# Score bands → temperature
HOT_THRESHOLD = 7
WARM_THRESHOLD = 4
MAX_SCORE = 10

# Number of years since a copyright date before we score it as stale.
# 3 = "site hasn't been touched since 2023" feels stale in 2026.
STALE_COPYRIGHT_YEARS = 3


def score_lead(lead_data):
    """
    Score a raw lead dict 0-10 and assign a temperature.

    Accepts the same dict shape produced by scrapers and used by the
    import pipeline. All keys are optional — missing keys are treated
    as "no signal" rather than penalizing.

    Returns (score: int, temperature: str)
    """
    score = 0

    # Website quality — requires audit data on the dict (set when PageSpeed
    # has been run against the lead's site). Worse score = bigger pain we
    # can solve = hotter lead.
    perf = lead_data.get('website_performance_score')
    if perf is not None:
        if perf < 50:
            score += 3
        elif perf < 70:
            score += 2
        elif perf < 85:
            score += 1
        # 85+ already-good site, no points

    # No website at all is the strongest single signal.
    if not lead_data.get('website'):
        score += 4

    # Google presence signals
    if not lead_data.get('has_google_business'):
        score += 2

    review_count = lead_data.get('google_review_count') or 0
    if review_count == 0:
        score += 2
    elif review_count < 10:
        score += 1

    # Social media — now actually detected by the enricher. Treats
    # any of FB / IG / LinkedIn as a hit so a single channel counts.
    if not _any_social(lead_data):
        score += 1

    # ── Enrichment-derived signals (set by outreach/enricher.py) ──
    # No SSL — visible browser warning, big security pitch hook.
    if lead_data.get('has_ssl') is False:
        score += 1

    # Generic email (info@gmail.com etc.) — signals no real IT.
    if lead_data.get('has_generic_email') is True:
        score += 1

    # Stale copyright — site hasn't been updated in years.
    if _copyright_is_stale(lead_data.get('copyright_year')):
        score += 1

    score = min(score, MAX_SCORE)

    if score >= HOT_THRESHOLD:
        temperature = 'hot'
    elif score >= WARM_THRESHOLD:
        temperature = 'warm'
    else:
        temperature = 'cold'

    return score, temperature


def _any_social(lead_data):
    """True when the lead has at least one social channel detected."""
    return bool(
        lead_data.get('facebook_url')
        or lead_data.get('instagram_url')
        or lead_data.get('linkedin_url')
        or lead_data.get('has_social_media')  # legacy boolean key
    )


def _copyright_is_stale(year):
    """True when the parsed copyright year is at least STALE_COPYRIGHT_YEARS
    older than the current year. None / 0 → False (no signal)."""
    if not year:
        return False
    from datetime import date
    return (date.today().year - int(year)) >= STALE_COPYRIGHT_YEARS


def score_breakdown(lead_data):
    """
    Same scoring logic as ``score_lead`` but returns a human-readable
    breakdown so the admin can see WHY a lead scored what it did.

    Mirrors the rules above 1:1 — kept as a separate function (rather
    than refactoring score_lead to return both) so the hot-path
    scoring stays a tight ``int`` return that the rest of the
    pipeline already calls in loops.

    Returns a list of dicts, in evaluation order:
      {
        'label':   short rule name shown in the table,
        'signal':  what the scraper actually captured (str),
        'points':  int contribution to this lead's score,
        'max':     int max this rule could ever award,
        'applied': bool — True if this rule contributed >0 points,
      }
    """
    rows = []

    # ── Rule: PageSpeed performance score ──
    perf = lead_data.get('website_performance_score')
    if perf is None:
        rows.append({
            'label': 'Website performance (PageSpeed)',
            'signal': 'no audit run',
            'points': 0,
            'max': 3,
            'applied': False,
        })
    elif perf < 50:
        rows.append({
            'label': 'Website performance (PageSpeed)',
            'signal': f'{perf}/100 — broken',
            'points': 3, 'max': 3, 'applied': True,
        })
    elif perf < 70:
        rows.append({
            'label': 'Website performance (PageSpeed)',
            'signal': f'{perf}/100 — poor',
            'points': 2, 'max': 3, 'applied': True,
        })
    elif perf < 85:
        rows.append({
            'label': 'Website performance (PageSpeed)',
            'signal': f'{perf}/100 — mediocre',
            'points': 1, 'max': 3, 'applied': True,
        })
    else:
        rows.append({
            'label': 'Website performance (PageSpeed)',
            'signal': f'{perf}/100 — already good',
            'points': 0, 'max': 3, 'applied': False,
        })

    # ── Rule: no website at all (biggest single signal) ──
    has_site = bool(lead_data.get('website'))
    rows.append({
        'label': 'Has a website',
        'signal': lead_data.get('website') or '(none found)',
        'points': 0 if has_site else 4,
        'max': 4,
        'applied': not has_site,
    })

    # ── Rule: Google Business Profile ──
    has_gbp = bool(lead_data.get('has_google_business'))
    rows.append({
        'label': 'Google Business Profile',
        'signal': 'present' if has_gbp else 'missing',
        'points': 0 if has_gbp else 2,
        'max': 2,
        'applied': not has_gbp,
    })

    # ── Rule: review count ──
    review_count = lead_data.get('google_review_count') or 0
    if review_count == 0:
        rev_points, rev_signal = 2, '0 reviews'
    elif review_count < 10:
        rev_points, rev_signal = 1, f'{review_count} reviews (<10)'
    else:
        rev_points, rev_signal = 0, f'{review_count} reviews — established'
    rows.append({
        'label': 'Google review count',
        'signal': rev_signal,
        'points': rev_points,
        'max': 2,
        'applied': rev_points > 0,
    })

    # ── Rule: social media presence (detected by the enricher) ──
    socials_found = []
    for key, label in (
        ('facebook_url', 'Facebook'),
        ('instagram_url', 'Instagram'),
        ('linkedin_url', 'LinkedIn'),
    ):
        if lead_data.get(key):
            socials_found.append(label)
    has_social = bool(socials_found) or bool(
        lead_data.get('has_social_media'))
    if has_social:
        sig = (', '.join(socials_found)
               if socials_found else 'detected (legacy flag)')
    else:
        sig = 'none detected'
    rows.append({
        'label': 'Social media presence',
        'signal': sig,
        'points': 0 if has_social else 1,
        'max': 1,
        'applied': not has_social,
    })

    # ── Rule: SSL (https://) ──
    ssl_state = lead_data.get('has_ssl')
    if ssl_state is None:
        ssl_sig, ssl_pts = 'not checked', 0
    elif ssl_state:
        ssl_sig, ssl_pts = 'https OK', 0
    else:
        ssl_sig, ssl_pts = 'no HTTPS — browser warning', 1
    rows.append({
        'label': 'SSL / HTTPS',
        'signal': ssl_sig,
        'points': ssl_pts,
        'max': 1,
        'applied': ssl_pts > 0,
    })

    # ── Rule: generic email domain (gmail/yahoo/…) ──
    generic = lead_data.get('has_generic_email')
    if generic is None:
        gen_sig, gen_pts = 'no email found', 0
    elif generic:
        gen_sig, gen_pts = 'free provider (gmail/yahoo/etc)', 1
    else:
        gen_sig, gen_pts = "uses firm's own domain", 0
    rows.append({
        'label': 'Email domain quality',
        'signal': gen_sig,
        'points': gen_pts,
        'max': 1,
        'applied': gen_pts > 0,
    })

    # ── Rule: stale copyright year ──
    year = lead_data.get('copyright_year')
    if not year:
        c_sig, c_pts = 'no © year on site', 0
    elif _copyright_is_stale(year):
        c_sig, c_pts = f'© {year} — stale (3+ yrs)', 1
    else:
        c_sig, c_pts = f'© {year} — current', 0
    rows.append({
        'label': 'Site freshness (© year)',
        'signal': c_sig,
        'points': c_pts,
        'max': 1,
        'applied': c_pts > 0,
    })

    return rows
