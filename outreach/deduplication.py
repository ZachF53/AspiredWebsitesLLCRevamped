"""
Lead deduplication — runs against the DB before importing a scraped lead.

Two-stage match:
  1. Exact case-insensitive (firm_name + city + state) → fast path
  2. Fuzzy match (SequenceMatcher ratio ≥ FUZZY_THRESHOLD) within same
     city + state → catches typos, suffix variations, etc.

Per CLAUDE.md → Data Model Decisions, this replaces the DB-level
unique_together constraint. Behavior is intentionally tolerant —
if location is unknown, we let the lead through rather than risk
false-positive dedup against location-less rows.
"""

from difflib import SequenceMatcher

from .models import Lead


FUZZY_THRESHOLD = 0.8


def is_duplicate(firm_name, city, state):
    """
    Return True if a lead with effectively the same firm + location
    already exists. Tolerant of casing and minor spelling variation.

    Empty firm_name → False (let it through; pipeline will skip on save).
    Empty city AND state → False (no reliable location key to dedup against).
    """
    if not firm_name:
        return False
    if not city and not state:
        return False

    # Stage 1 — exact case-insensitive
    if Lead.objects.filter(
        firm_name__iexact=firm_name,
        city__iexact=city,
        state__iexact=state,
    ).exists():
        return True

    # Stage 2 — fuzzy match within same city+state
    candidates = Lead.objects.filter(
        city__iexact=city,
        state__iexact=state,
    ).values_list('firm_name', flat=True)

    target = firm_name.lower()
    for candidate in candidates:
        if not candidate:
            continue
        ratio = SequenceMatcher(None, target, candidate.lower()).ratio()
        if ratio >= FUZZY_THRESHOLD:
            return True

    return False


def similarity(a, b):
    """Return 0.0-1.0 similarity between two strings. Used in tests."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()
