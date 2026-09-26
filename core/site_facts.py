"""
Approved public statements of business fact.

These are the sentences the owner has signed off in
`docs/brand_fact_matrix.md`. They live in one module because the failure
they exist to prevent is drift: before this, the About page said Aspired
was "Based in San Antonio, TX and Atlanta, GA", two meta descriptions said
"Based in San Antonio and Atlanta", and the footer and structured data said
Warner Robins, Georgia. Three different answers to "where is this company?"
on one website, and no single place to correct it.

Scope, deliberately narrow:

- Only facts an owner has approved go here. A PENDING row in the fact
  matrix does not get a constant invented for it.
- Pricing, packages and tier entitlements are NOT here. Those are
  database-authoritative through `billing.pricing_models`; copying them
  into constants would recreate the second-source-of-truth problem in a
  new place (CLAUDE.md forbids it outright).
- The city is not a contradiction of the state. "Warner Robins, GA" in the
  footer and structured data is a more specific true statement than "Based
  in Georgia", so both stand. LOCATION_BASE is the brand sentence used in
  prose and metadata; the structured-data address stays city-level because
  schema.org consumers expect a locality.

Approved 2026-08-16: "Based in Georgia. Serving clients nationwide."
Tightened 2026-09-25 (implementation plan D-14): the canonical location
is the city, Warner Robins, GA, everywhere, so prose now names it too.
"""

from django.utils.functional import SimpleLazyObject


# Where the company is. City-level since the Sept 2026 plan (D-14).
LOCATION_BASE = 'Warner Robins, GA'

# Reach. Approved: the business serves the whole US.
LOCATION_REACH = 'nationwide'

# The canonical one-line statement for prose and meta descriptions.
LOCATION_STATEMENT = 'Based in Warner Robins, Georgia. Serving clients nationwide.'

# Compact form for places with a tight character budget (meta
# descriptions, email signatures) where the full stop-separated sentence
# reads awkwardly mid-paragraph.
LOCATION_PHRASE = 'based in Warner Robins, Georgia, serving clients nationwide'

# Governing law. Approved 2026-08-16 and corroborated by both contract
# templates, which already specified it. The venue COUNTY is still
# unresolved, so nothing here names one.
GOVERNING_LAW_STATE = 'Georgia'

# ── Delivery timeline. Revised 2026-09-25 (plan D-18) ────────────────
# One build, one timeline: four to six weeks, depending on how quickly
# content comes back. The Week 1 -> Week 6 process on /services/web-design/
# is the same statement broken into steps.
BUILD_TIMELINE = 'four to six weeks'
BUILD_TIMELINE_TITLE = 'Four to Six Weeks'
DELIVERY_QUALIFIER = 'depending on how quickly content comes back'

# ── The sales call. Approved 2026-08-17 ───────────────────────────────
# One name, one duration, one destination. Before this the site offered
# "Book a Call", "Schedule", "Strategy Call", "Consultation", "Kickoff
# Call" and "Start Your Project", and roughly thirty Book/Schedule
# buttons pointed at the contact form rather than the calendar — so a
# visitor who wanted to pick a time landed on a message form instead.
#
# "Kickoff Call" was tried first and withdrawn: the refund policy makes
# the deposit refundable "until the kickoff call happens", meaning the
# post-payment project start. Using the same words for a free pre-sale
# call would have read as though the deposit is never refundable, because
# that call happens before anyone pays. A sales label is not worth
# muddying a refund term.
#
# "Strategy Call" was already the site's own dominant wording — 24 of the
# ~38 booking CTAs said it — so it is the established term rather than a
# new coinage, it keeps "kickoff" free for the post-payment event, and it
# tells the prospect what they get rather than what we want.
CALL_NAME = 'Strategy Call'
CALL_DURATION_MINUTES = 30
CALL_IS_FREE = True
CALL_CTA = 'Book a Free 30-Minute Strategy Call'
CALL_CTA_SHORT = 'Book a Strategy Call'
# Every booking CTA resolves here. Named rather than hardcoded so the
# canonical scheduler can move without another thirty-link sweep.
CALL_URL_NAME = 'scheduler:design_schedule'

# The build guarantee, as the signed contract grants it
# (clients/contract_template.py). Revised 2026-09-25 by the owner: within
# 30 days of signing the client may cancel; 25% of what has been paid is
# retained for work already done and the other 75% is refunded, and any
# remaining installments are cancelled. Public copy must not promise more
# or less than this. The refund itself is executed from the admin
# dashboard (website detail -> Billing -> 30-day guarantee).
GUARANTEE_DAYS = 30
GUARANTEE_REFUND_PERCENT = 75
GUARANTEE_RETAINED_PERCENT = 25
BUILD_GUARANTEE = (
    'If you are not satisfied with your website build, you may cancel '
    'within 30 days of signing your agreement. We refund 75% of what you '
    'have paid, keep 25% for the work already done, and cancel any '
    'remaining payments.'
)
BUILD_GUARANTEE_SHORT = '30-Day Guarantee'

# Phone. A San Antonio number that rings in Warner Robins; explained
# wherever it is shown so the 210 area code doesn't read as a Texas office.
PHONE_DISPLAY = '(210) 896-2536'
PHONE_TEL = '+12108962536'
PHONE_NOTE = 'A San Antonio number that rings in Warner Robins, GA'


# Routes where the visitor tracker and session recorder never load:
# anything carrying a credential, a token in the URL, payment details or
# a logged-in client's data. Recording is for improving the marketing
# pages, nothing else (plan M-1.08, disclosed in the privacy policy).
TRACKING_EXCLUDED_PREFIXES = (
    '/login', '/logout', '/password-reset', '/set-password', '/portal',
    '/onboarding', '/pay/', '/plan-pay/', '/billing/', '/checkout',
    '/maintenance/', '/admin', '/proposals/', '/intelligence/', '/nps/',
    '/ref/', '/api/',
)


def tracking_allowed(path):
    return not any(path.startswith(p) for p in TRACKING_EXCLUDED_PREFIXES)


def _site_content():
    from public.models import SiteContent
    return SiteContent.get_solo()


def site_facts(request):
    """Context processor exposing the approved facts to every template."""
    return {
        'LOCATION_BASE': LOCATION_BASE,
        'LOCATION_REACH': LOCATION_REACH,
        'LOCATION_STATEMENT': LOCATION_STATEMENT,
        'LOCATION_PHRASE': LOCATION_PHRASE,
        'GOVERNING_LAW_STATE': GOVERNING_LAW_STATE,
        'BUILD_GUARANTEE': BUILD_GUARANTEE,
        'BUILD_GUARANTEE_SHORT': BUILD_GUARANTEE_SHORT,
        'DELIVERY_QUALIFIER': DELIVERY_QUALIFIER,
        'BUILD_TIMELINE': BUILD_TIMELINE,
        'BUILD_TIMELINE_TITLE': BUILD_TIMELINE_TITLE,
        'GUARANTEE_DAYS': GUARANTEE_DAYS,
        'GUARANTEE_REFUND_PERCENT': GUARANTEE_REFUND_PERCENT,
        'GUARANTEE_RETAINED_PERCENT': GUARANTEE_RETAINED_PERCENT,
        'PHONE_DISPLAY': PHONE_DISPLAY,
        'PHONE_TEL': PHONE_TEL,
        'PHONE_NOTE': PHONE_NOTE,
        'SITE_CONTENT': SimpleLazyObject(_site_content),
        'TRACKING_ALLOWED': tracking_allowed(getattr(request, 'path', '') or ''),
        'CALL_NAME': CALL_NAME,
        'CALL_DURATION_MINUTES': CALL_DURATION_MINUTES,
        'CALL_CTA': CALL_CTA,
        'CALL_CTA_SHORT': CALL_CTA_SHORT,
    }
