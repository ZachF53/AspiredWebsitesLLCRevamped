"""
Texas TDLR licence register -> the name of the person who owns the lead.

Texas requires an Air Conditioning & Refrigeration contractor licence,
and the Department of Licensing and Regulation publishes the whole
register as a public CSV -- 20,448 rows, each carrying the LICENCE
HOLDER'S NAME alongside the business name.

For a segment with no website and often no email, that name is the most
valuable field available. "Is Bob around?" opens a call that "can I
speak to the owner?" does not.

Measured 2026-10-10 against the 148-lead HVAC batch: 32 confident
matches, 33 ambiguous (discarded), 83 no match.

WHY MATCHING IS ON NAME ALONE
-----------------------------
The register has BUSINESS PHONE and PHONE NUMBER columns and both are
redacted in the public file -- 0 of 20,448 rows carry a usable number.
Phone would have been the reliable join, so its absence is what forces
everything below.

AMBIGUITY IS THE WHOLE PROBLEM
------------------------------
Normalising a business name means stripping trade words -- "air",
"heating", "services", "llc" -- or "SOUTH AUSTIN AC & HEATING" never
matches "South Austin AC". But stripping them collapses distinct
businesses together: 2,055 of 15,676 normalised names in the register
are shared by more than one licensee.

A first pass that simply took the first candidate reported 60 matches.
It was wrong: "Air Action Air Conditioning" maps to SIX licensees, so
naming one of them was a one-in-six guess presented as fact. A wrong
owner name is worse than none -- it goes into a call opening, and
getting someone's name wrong ends the call.

So a match is only recorded when it is unique, or when the city
corroborates exactly one candidate. Everything else is reported as
ambiguous and left alone.
"""

import collections
import csv
import io
import logging
import re
from difflib import SequenceMatcher

import requests

logger = logging.getLogger(__name__)


TDLR_ACR_CSV = 'https://www.tdlr.texas.gov/dbproduction2/ltairref.csv'
TDLR_TIMEOUT = 180

# Column positions in the published file, verified 2026-10-10. The file
# ships WITH a header row, but positions are used rather than names
# because several headers contain commas of their own
# ('BUSINESS CITY, STATE ZIP').
COL_LICENCE_NO = 1
COL_EXPIRES = 2
COL_NAME = 4            # the licence holder -- the person
COL_MAILING_CITY = 7
COL_BUSINESS_NAME = 9
COL_BUSINESS_CITY = 12

# Trade and corporate words that identify nothing. Stripping them is
# required for real matches and is also what creates the collisions, so
# the uniqueness check below is not optional.
_STRIP_RE = re.compile(
    r'\b(llc|inc|co|company|corp|the|and|of|services|service|heating|'
    r'air|conditioning|ac|a c|hvac|refrigeration|cooling|systems|'
    r'solutions|repair)\b')

FUZZY_THRESHOLD = 0.93


def normalise(name):
    """Business name -> comparable token string."""
    s = re.sub(r'[^a-z0-9 ]', ' ', (name or '').lower())
    s = _STRIP_RE.sub(' ', s)
    return ' '.join(s.split())


def format_person(raw):
    """'CASH, BOB C' -> 'Bob C Cash'."""
    raw = (raw or '').strip()
    if ',' not in raw:
        return raw.title()
    last, first = [p.strip() for p in raw.split(',', 1)]
    return ' '.join(f'{first} {last}'.title().split())


def fetch_register(url=TDLR_ACR_CSV):
    """Download the public register. Returns data rows (no header)."""
    resp = requests.get(
        url, headers={'User-Agent': 'AspiredWebsitesBot/1.0'},
        timeout=TDLR_TIMEOUT)
    resp.raise_for_status()
    # The file is not UTF-8; latin-1 round-trips every byte rather than
    # raising partway through a 20k-row download.
    text = resp.content.decode('latin-1', errors='replace')
    rows = list(csv.reader(io.StringIO(text)))
    return rows[1:] if rows else []


def index_by_name(rows):
    """Normalised business name -> [row, ...]."""
    groups = collections.defaultdict(list)
    for row in rows:
        if len(row) <= COL_BUSINESS_NAME:
            continue
        key = normalise(row[COL_BUSINESS_NAME])
        if key:
            groups[key].append(row)
    return groups


def _row_city(row):
    biz = row[COL_BUSINESS_CITY] if len(row) > COL_BUSINESS_CITY else ''
    mail = row[COL_MAILING_CITY] if len(row) > COL_MAILING_CITY else ''
    return (biz or mail or '').upper()


def match_lead(lead, groups):
    """Find this lead's licence row.

    Returns ``(row, how)`` or ``(None, reason)``. ``how`` is recorded so
    a match can be audited later without re-running anything.
    """
    key = normalise(lead.firm_name)
    if not key:
        return None, 'no distinctive tokens in the business name'

    candidates = groups.get(key, [])
    how = 'exact'
    if not candidates and len(key) > 6:
        for other, rows in groups.items():
            if SequenceMatcher(None, key, other).ratio() >= FUZZY_THRESHOLD:
                candidates, how = rows, 'fuzzy'
                break
    if not candidates:
        return None, 'no licensed contractor with that name'

    if len(candidates) == 1:
        return candidates[0], how

    # Several licensees share this name. The city breaks the tie only if
    # it singles ONE of them out; otherwise there is no evidence saying
    # which, and guessing puts a stranger's name in a call opening.
    city = (lead.city or '').upper()
    if city:
        same_city = [c for c in candidates if city in _row_city(c)]
        if len(same_city) == 1:
            return same_city[0], f'{how}+city'

    return None, (
        f'{len(candidates)} licensees share this business name; '
        f'no way to tell which')


def apply_to_leads(leads, groups, overwrite=False):
    """Write owner names onto leads. Returns ``(matched, report)``."""
    matched = 0
    report = []
    for lead in leads:
        if lead.attorney_name and not overwrite:
            continue
        row, how = match_lead(lead, groups)
        if row is None:
            report.append((lead, None, how))
            continue
        person = format_person(row[COL_NAME])
        if not person:
            report.append((lead, None, 'licence row has no holder name'))
            continue
        lead.attorney_name = person[:120]
        lead.notes = (lead.notes or '') + (
            f' | TDLR A/C licence #{row[COL_LICENCE_NO]} '
            f'expires {row[COL_EXPIRES].strip()} '
            f'holder {row[COL_NAME]} (matched {how})')
        lead.save(update_fields=['attorney_name', 'notes'])
        matched += 1
        report.append((lead, person, how))
    return matched, report
