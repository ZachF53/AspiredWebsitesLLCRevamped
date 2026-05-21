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

    # Social media — placeholder until scrapers detect it
    if not lead_data.get('has_social_media'):
        score += 1

    score = min(score, MAX_SCORE)

    if score >= HOT_THRESHOLD:
        temperature = 'hot'
    elif score >= WARM_THRESHOLD:
        temperature = 'warm'
    else:
        temperature = 'cold'

    return score, temperature
