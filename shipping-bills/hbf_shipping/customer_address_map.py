"""
Loader for the customer-address master list.

Reads `data/hbf-customer-shipping-addresses.xlsx` and returns a map from
`NormalizedAddress` (street + line_2 + city + state + postcode) to a list
of `CustomerEntry` (`name` from the `Name` column, `line_1_clean` derived
from `AddressLine1` per the dash rule below).

A single NormalizedAddress can hold multiple CustomerEntries — many HBF
customer sites share a physical address (e.g., the Butner federal complex
serves seven customer entities at one address).

The address shape (`NormalizedAddress`) is the canonical 5-field type
defined in `hbf_shipping.ship_to`; the same shape is produced by both
the page-1 invoice ShipTo extractor and the page-2 BOL ShipTo extractor,
so all three sources speak one address vocabulary.

NOTE (stage 1): the matching logic below predates the 5-field address
shape; `line_2` is currently captured but ignored at match time. The
next-stage refactor will make `line_2` a real participant in matching
(suite/unit-aware customer disambiguation).

Per `AddressLine1` interpretation:
  - Take text **before the first `-`**, whitespace-stripped.
  - If `AddressLine1` has no `-`, use the whole field, whitespace-stripped.
  - Result is then normalized (upper + collapsed whitespace) for matching.

Per skip rule: any row missing `City`, `State`, or `Postcode` is excluded
from the map (won't be available for address-based matching). A debug log
records the count and reason.
"""

from __future__ import annotations

import logging
import re
from collections import namedtuple
from pathlib import Path
from typing import Union

from openpyxl import load_workbook
from rapidfuzz import fuzz

from .ship_to import (
    NormalizedAddress,
    _normalize_address,
    _norm,
    _fmt_postcode,
)


logger = logging.getLogger(__name__)


CustomerEntry = namedtuple('CustomerEntry', 'name line_1_clean')
LookupResult = namedtuple(
    'LookupResult',
    'pairs cm_method addr_score name_method name_score',
)
# pairs:       list[(NormalizedAddress, CustomerEntry)] — match output, OR
#              best near-miss on no_match / multi_match_unresolved.
# cm_method:   one of:
#                'address_exact'                — exact address hit, single match
#                'address_fuzzy'                — fuzzy address hit, single match
#                'address_disambiguated_by_name' — address multi-hit, name picked one
#                'name_fallback'                — address missed, name found a match
#                'multi_match_unresolved'       — address multi-hit, name couldn't narrow
#                                                 (or generic consignee blocked the attempt)
#                'no_match'                     — neither tier matched
# addr_score:  address-tier score; 100 on address_exact, fuzz score on address_fuzzy,
#              best near-miss score on no_match (0 when no candidates considered).
# name_method: which name tier was used (only meaningful when name was actually used):
#                'exact'             — tier 1: verbatim equality
#                'case_insensitive'  — tier 2
#                'normalized'        — tier 3
#                'fuzzy'             — tier 4: WRatio at or above threshold
#                'tried_failed'      — name was attempted (fallback or disambiguator)
#                                      but no tier produced a unique winner
#                'n/a'               — name was not used at all (single-match address,
#                                      or generic consignee blocked the attempt)
# name_score:  100 for tiers 1–3, WRatio for tier 4, best near-miss WRatio on
#              tried_failed, 0 when name_method == 'n/a'.


# Default minimum token_set_ratio to accept a fuzzy address fallback within
# a CSZ bucket. Tunable per-call via lookup_by_address(..., fuzzy_threshold=…).
DEFAULT_FUZZY_THRESHOLD = 85

# Default minimum rapidfuzz WRatio score (0–100) to accept a fuzzy name match.
# WRatio blends ratio / partial_ratio / token_sort_ratio / token_set_ratio
# with sensible weights, so it handles both the "abbreviation vs full" case
# and the "containment" case (consignee = customer name + extra qualifiers
# like 'Commissary Whse') that strict ratio misses.
DEFAULT_NAME_FUZZY_THRESHOLD = 88

# Consignee strings that are too generic to fuzzy-match by name safely
# (would partial-match every facility of that type in the customer set).
# Comparison is against the name-normalized form (lowercase, alphanumerics
# only, whitespace collapsed).
_GENERIC_CONSIGNEE_NORMS = frozenset({
    'federal correctional institution',
})


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ADDRESS_FILE = _PROJECT_ROOT / 'data' / 'hbf-customer-shipping-addresses.xlsx'


def _clean_line_1(al1) -> str:
    """Per dash rule: pre-dash text whitespace-stripped, or whole AL1 if
    no dash. Result is normalized."""
    if al1 is None:
        return ''
    s = str(al1)
    if '-' in s:
        s = s.split('-', 1)[0]
    return _norm(s)


def load_address_to_customers(
    xlsx_path: Union[str, Path, None] = None,
) -> dict:
    """Load the customer-address XLSX and return dict[NormalizedAddress, list[CustomerEntry]].

    Rows missing City, State, or Postcode are skipped (logged at debug).
    Same NormalizedAddress with multiple rows → list grows.
    """
    path = Path(xlsx_path) if xlsx_path else DEFAULT_ADDRESS_FILE
    wb = load_workbook(filename=str(path), data_only=True, read_only=True)
    try:
        ws = wb.active

        result: dict = {}
        skipped = 0
        total = 0

        for row in ws.iter_rows(min_row=2, values_only=True):
            if row is None:
                continue
            cells = list(row) + [None] * (6 - len(row))
            name, al1, al2, city, state, pc = cells[:6]

            if name is None and al1 is None and al2 is None:
                continue
            total += 1

            city_n = _norm(city)
            state_n = _norm(state)
            pc_n = _fmt_postcode(pc)

            if not city_n or not state_n or not pc_n:
                skipped += 1
                logger.debug(
                    "skip name=%r — missing city/state/postcode (city=%r state=%r postcode=%r)",
                    name, city, state, pc,
                )
                continue

            addr = _normalize_address(al2, city, state, pc)
            entry = CustomerEntry(
                name=str(name).strip() if name is not None else '',
                line_1_clean=_clean_line_1(al1),
            )
            result.setdefault(addr, []).append(entry)
    finally:
        wb.close()

    logger.debug(
        "loaded %d unique addresses (%d total rows, %d skipped for missing city/state/postcode)",
        len(result), total, skipped,
    )
    return result


_LEADING_NUMBER_RE = re.compile(r'^\s*(\d+(?:-\d+)?)')


def _leading_street_number(street: str):
    """Return the leading street number (e.g., '1176', '27072', '5980') or
    None if the street doesn't start with a digit. Treated as a hard
    discriminator in fuzzy fallback — different number = different address."""
    if not street:
        return None
    m = _LEADING_NUMBER_RE.match(street)
    return m.group(1) if m else None


def _normalize_name(name: str) -> str:
    """Lowercase, replace non-alphanumerics with spaces, collapse whitespace.

    Mirrors the legacy customer_lookup._normalize so the 0.88 difflib-ratio
    threshold stays semantically calibrated.
    """
    if not name:
        return ''
    s = re.sub(r'[^a-z0-9]+', ' ', str(name).lower())
    return re.sub(r'\s+', ' ', s).strip()


def _build_pairs_at_address(addr: NormalizedAddress, entries) -> list:
    """Helper: explode a single NormalizedAddress + entry list into pair tuples."""
    return [(addr, e) for e in entries]


def lookup_by_address(
    address_map: dict,
    street,
    city,
    state,
    postcode,
    *,
    fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD,
) -> LookupResult:
    """Look up customer entries by address.

    Match strategy:
      1. Exact match on the normalized Address key.
      2. On miss: narrow to candidates sharing the same (city, state, postcode).
         Filter further to candidates with the same leading street number
         (a different number is always a different address). Score remaining
         candidates with rapidfuzz.fuzz.token_set_ratio over the street string.
         Accept the highest-scoring candidate at or above `fuzzy_threshold`.

    On `no_match`, `pairs` carries the best near-miss candidate's entries
    so the caller can show the closest miss.
    """
    key = _normalize_address(street, city, state, postcode)

    exact = address_map.get(key, [])
    if exact:
        return LookupResult(
            _build_pairs_at_address(key, exact),
            'address_exact', 100, 'n/a', 0,
        )

    csz = (key.city, key.state, key.postcode)
    bucket = [(a, es) for a, es in address_map.items()
              if (a.city, a.state, a.postcode) == csz]
    if not bucket:
        return LookupResult([], 'no_match', 0, 'n/a', 0)

    key_num = _leading_street_number(key.street)
    if key_num is not None:
        same_num = [(a, es) for a, es in bucket
                    if _leading_street_number(a.street) == key_num]
        if not same_num:
            # Hard fail: different street numbers ⇒ different address. Surface the
            # CSZ-bucket candidates as near-miss diagnostic.
            near_miss_pairs = [(a, e) for a, es in bucket for e in es]
            return LookupResult(near_miss_pairs, 'no_match', 0, 'n/a', 0)
        bucket = same_num

    best_addr, best_entries, best_score = None, [], 0
    for cand_addr, cand_entries in bucket:
        score = fuzz.token_set_ratio(key.street, cand_addr.street)
        if score > best_score:
            best_score, best_addr, best_entries = score, cand_addr, cand_entries

    if best_score >= fuzzy_threshold:
        return LookupResult(
            _build_pairs_at_address(best_addr, best_entries),
            'address_fuzzy', int(best_score), 'n/a', 0,
        )
    near_miss_pairs = (
        _build_pairs_at_address(best_addr, best_entries) if best_addr else []
    )
    return LookupResult(near_miss_pairs, 'no_match', int(best_score), 'n/a', 0)


def _disambiguate_by_name(
    pairs: list,
    consignee_name,
    consignee_norm: str,
    name_fuzzy_threshold: int,
) -> tuple:
    """When address lookup returns multiple candidates at the same address,
    try to narrow to one by matching the PDF consignee name against each
    candidate's `CustomerEntry.name`. Same 4-tier match the name-fallback
    uses, but scoped to just these N pairs.

    Returns (narrowed_pairs, name_method, name_score):
      - narrowed_pairs: list with exactly one (Address, CustomerEntry) on
        success, empty on `tried_failed`.
      - name_method: 'exact' | 'case_insensitive' | 'normalized' | 'fuzzy'
        | 'tried_failed'.
      - name_score: 100 for tiers 1–3, WRatio for tier 4, best WRatio seen on
        tried_failed (0 if no candidates).
    """
    # Tier 1 — exact verbatim
    hits = [(a, e) for a, e in pairs if e.name == consignee_name]
    if len(hits) == 1:
        return hits, 'exact', 100
    if len(hits) > 1:
        return [], 'tried_failed', 100  # multiple exact ties — ambiguous

    # Tier 2 — case-insensitive
    if consignee_name:
        lower = consignee_name.lower()
        hits = [(a, e) for a, e in pairs if e.name.lower() == lower]
        if len(hits) == 1:
            return hits, 'case_insensitive', 100
        if len(hits) > 1:
            return [], 'tried_failed', 100

    # Tier 3 — normalized form
    hits = [(a, e) for a, e in pairs
            if _normalize_name(e.name) == consignee_norm]
    if len(hits) == 1:
        return hits, 'normalized', 100
    if len(hits) > 1:
        return [], 'tried_failed', 100

    # Tier 4 — fuzzy WRatio. Take the top scorer if it clears the threshold
    # AND is not tied with another candidate.
    scored = [
        (fuzz.WRatio(consignee_norm, _normalize_name(e.name)), a, e)
        for a, e in pairs
    ]
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return [], 'tried_failed', 0
    top_score = int(round(scored[0][0]))
    if scored[0][0] < name_fuzzy_threshold:
        return [], 'tried_failed', top_score
    if len(scored) > 1 and scored[1][0] >= scored[0][0]:
        return [], 'tried_failed', top_score  # tied at the top — still ambiguous
    return [(scored[0][1], scored[0][2])], 'fuzzy', top_score


def lookup_with_name_fallback(
    address_map: dict,
    consignee_name,
    street,
    city,
    state,
    postcode,
    *,
    fuzzy_threshold: int = DEFAULT_FUZZY_THRESHOLD,
    name_fuzzy_threshold: int = DEFAULT_NAME_FUZZY_THRESHOLD,
) -> LookupResult:
    """Look up customer entries by address first; on miss, fall back to a
    4-tier name match. When the consignee is a generic phrase like 'Federal
    Correctional Institution', skip the name-based steps (too generic to
    match safely).

    When address lookup returns multiple candidates at the same address,
    try to narrow them down by matching the consignee name against each
    candidate (same 4-tier match, scoped to that subset). If the name
    uniquely identifies one candidate, the result is `address_disambiguated_by_name`.

    Returns a LookupResult; see the LookupResult docstring for the meaning
    of each field. On no_match / multi_match_unresolved, `pairs` holds the
    best near-miss for diagnostic purposes.
    """
    addr_result = lookup_by_address(
        address_map, street, city, state, postcode,
        fuzzy_threshold=fuzzy_threshold,
    )

    consignee_norm = _normalize_name(consignee_name)

    if addr_result.cm_method in ('address_exact', 'address_fuzzy'):
        # Single match → done.
        if len(addr_result.pairs) <= 1:
            return addr_result

        # Multi-match. Generic consignee → can't disambiguate safely.
        if not consignee_norm or consignee_norm in _GENERIC_CONSIGNEE_NORMS:
            return LookupResult(
                addr_result.pairs, 'multi_match_unresolved',
                addr_result.addr_score, 'n/a', 0,
            )

        narrowed, name_method, name_score = _disambiguate_by_name(
            addr_result.pairs, consignee_name, consignee_norm,
            name_fuzzy_threshold,
        )
        if narrowed:
            return LookupResult(
                narrowed, 'address_disambiguated_by_name',
                addr_result.addr_score, name_method, name_score,
            )
        return LookupResult(
            addr_result.pairs, 'multi_match_unresolved',
            addr_result.addr_score, name_method, name_score,
        )

    # Address tier didn't match. Try name fallback unless consignee is generic.
    if not consignee_norm or consignee_norm in _GENERIC_CONSIGNEE_NORMS:
        return LookupResult(
            addr_result.pairs, 'no_match',
            addr_result.addr_score, 'n/a', 0,
        )

    found, name_method, name_score = _name_fallback_search(
        address_map, consignee_name, consignee_norm, name_fuzzy_threshold,
    )
    if name_method != 'tried_failed':
        return LookupResult(
            found, 'name_fallback',
            addr_result.addr_score, name_method, name_score,
        )

    # Both tiers missed. Surface whichever near-miss scored higher.
    if name_score > addr_result.addr_score:
        return LookupResult(
            found, 'no_match',
            addr_result.addr_score, 'tried_failed', name_score,
        )
    return LookupResult(
        addr_result.pairs, 'no_match',
        addr_result.addr_score, 'tried_failed', name_score,
    )


def _name_fallback_search(
    address_map: dict,
    consignee_name,
    consignee_norm: str,
    name_fuzzy_threshold: int,
) -> tuple:
    """Run the 4-tier name match against every CustomerEntry in the map.

    Returns (pairs, name_method, name_score) — same shape as
    `_disambiguate_by_name`, but searching the entire address map instead of
    a constrained candidate list. On a tier-1/2/3 hit, returns *all* pairs
    sharing the matched normalized name (one customer can have many
    addresses). On `tried_failed`, `pairs` is the best WRatio-near-miss for
    diagnostics.
    """
    by_norm: dict = {}
    canonical: dict = {}
    for addr, entries in address_map.items():
        for e in entries:
            n = _normalize_name(e.name)
            if not n:
                continue
            by_norm.setdefault(n, []).append((addr, e))
            canonical.setdefault(n, e.name)

    # Tier 1 — exact verbatim
    for addr, entries in address_map.items():
        for e in entries:
            if e.name == consignee_name:
                return by_norm[_normalize_name(e.name)], 'exact', 100

    # Tier 2 — case-insensitive
    if consignee_name:
        lower = consignee_name.lower()
        for n, pairs in by_norm.items():
            if canonical[n].lower() == lower:
                return pairs, 'case_insensitive', 100

    # Tier 3 — normalized
    if consignee_norm in by_norm:
        return by_norm[consignee_norm], 'normalized', 100

    # Tier 4 — fuzzy WRatio
    best_norm, best_score = None, 0.0
    for cand_norm in by_norm.keys():
        s = fuzz.WRatio(consignee_norm, cand_norm)
        if s > best_score:
            best_score, best_norm = s, cand_norm

    name_score = int(round(best_score))
    if best_score >= name_fuzzy_threshold:
        return by_norm[best_norm], 'fuzzy', name_score

    near_miss = by_norm.get(best_norm, []) if best_norm else []
    return near_miss, 'tried_failed', name_score
