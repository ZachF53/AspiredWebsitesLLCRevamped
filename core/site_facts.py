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
"""


# Where the company is. State-level, per the approved public wording.
LOCATION_BASE = 'Georgia'

# Reach. Approved: the business serves the whole US.
LOCATION_REACH = 'nationwide'

# The canonical one-line statement for prose and meta descriptions.
LOCATION_STATEMENT = 'Based in Georgia. Serving clients nationwide.'

# Compact form for places with a tight character budget (meta
# descriptions, email signatures) where the full stop-separated sentence
# reads awkwardly mid-paragraph.
LOCATION_PHRASE = 'based in Georgia, serving clients nationwide'

# Governing law. Approved 2026-08-16 and corroborated by both contract
# templates, which already specified it. The venue COUNTY is still
# unresolved, so nothing here names one.
GOVERNING_LAW_STATE = 'Georgia'

# The build guarantee, as the signed contract actually grants it
# (clients/contract_template.py §7 and §10). Public copy must not promise
# more or less than this.
BUILD_GUARANTEE = (
    'If you are not satisfied with your website build, you may request a '
    'full refund of the build fee within 30 days of signing your agreement.'
)
BUILD_GUARANTEE_SHORT = '30-Day Money-Back Guarantee'


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
    }
