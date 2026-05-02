"""
Canonical ShipTo types and normalization for the HBF shipping pipeline.

A SHIP TO record (where the goods are being delivered) can be sourced from
either the page-1 invoice CONSIGNEE block or the page-2 BOL image. Both
paths produce the same downstream-comparable shape: a `ShipTo` carrying a
name, a list of name candidates (for downstream name matching), and a
USPS-normalized 5-field `NormalizedAddress`.

Each extraction path also produces an outer wrapper:
    InvoiceExtraction  — page-1 result + page-1-specific diagnostics
    BolExtraction      — page-2 result + BOL-specific diagnostics (raw OCR
                         lines, CSZ text, optional diagnostic PNG path, etc.)

Triage / customer-matching code consumes only the inner `ShipTo`. The outer
wrappers are for logging and debugging.

`NormalizedAddress` is the canonical address shape across the codebase:
five fields produced by `usaddress-scourgify` (USPS Pub 28). `address_line_2`
is preserved here even though current matching code does not yet use it —
preserving suite/unit information at the source so the next-stage matcher
can use it.

Also exposes the canonical normalization helpers (`_normalize_address`,
`_norm`, `_fmt_postcode`) and `extract_invoice_ship_to`, which both vendor
parsers and the customer-master loader use to land in the same address
shape.
"""

from __future__ import annotations

import logging
import re
from collections import namedtuple
from pathlib import Path
from typing import Optional

from scourgify import normalize_address_record


logger = logging.getLogger(__name__)


_MULTI_SPACE_RE = re.compile(r'\s+')


# A USPS-normalized address. Values are uppercase per scourgify output;
# missing components are '' (not None) for clean equality comparisons.
NormalizedAddress = namedtuple(
    'NormalizedAddress',
    'street line_2 city state postcode',
)


# The canonical SHIP TO record. Both sources produce this shape.
#
#   name:            best primary guess at the consignee name (top line of
#                    the SHIP TO block). Lightly cleaned (whitespace
#                    collapse) — apostrophes / periods preserved so the name
#                    matcher can do its own normalization.
#   name_candidates: every line above the address region that could be name
#                    material, in document order. `name`, when present, is
#                    `name_candidates[0]`. Empty list is valid.
#   address:         5-field NormalizedAddress, or None if no address could
#                    be normalized.
#   source:          which extraction path produced this record.
ShipTo = namedtuple('ShipTo', 'name name_candidates address source')


# -- Extraction wrappers ------------------------------------------------------
#
# Each wrapper carries the canonical `ShipTo` (possibly with None fields if
# extraction partially failed) plus source-specific diagnostics.
#
# `success` is True iff a usable ShipTo was produced (at minimum, a
# NormalizedAddress with a non-empty street). Callers needing finer-grained
# checks can inspect ship_to.address / ship_to.name directly.


InvoiceExtraction = namedtuple(
    'InvoiceExtraction',
    'pdf_path ship_to success failure_reason diagnostics',
)
# pdf_path:        Path to the source PDF.
# ship_to:         Canonical ShipTo (source='page1').
# success:         bool — True iff ship_to.address is populated and has a street.
# failure_reason:  Plain-English explanation when success=False; None on success.
# diagnostics:     Multi-line debug dump (raw page-1 fields, normalization steps).


BolExtraction = namedtuple(
    'BolExtraction',
    'pdf_path ship_to success failure_reason '
    'raw_lines csz_line diagnostic_path diagnostics',
)
# pdf_path:         Path to the source PDF.
# ship_to:          Canonical ShipTo (source='bol').
# success:          bool — True iff ship_to.address is populated and has a street.
# failure_reason:   Plain-English explanation when success=False; None on success.
# raw_lines:        OCR'd lines captured by the walker (top-down, CSZ last).
# csz_line:         The City/ST/ZIP row, e.g. 'Tucson, AZ 85756'; None if not found.
# diagnostic_path:  Path to annotated PNG, when diagnostic_dir was passed.
# diagnostics:      Multi-line debug dump (walker stop reason, line y-coords, etc.).


# -- Normalization helpers ----------------------------------------------------


def _norm(value) -> str:
    """Upper + strip + collapse internal whitespace. None/empty → ''."""
    if value is None:
        return ''
    return _MULTI_SPACE_RE.sub(' ', str(value).strip().upper())


def _fmt_postcode(pc) -> str:
    """5-digit zero-padded string for numeric cells (preserves leading
    zeros), pass-through stripped string for text cells, '' for empty."""
    if pc is None or pc == '':
        return ''
    if isinstance(pc, (int, float)):
        n = int(pc)
        return f'{n:05d}' if n else ''
    return str(pc).strip()


def _normalize_address_with_status(
    street, city, state, postcode, line_2=None,
) -> tuple[NormalizedAddress, bool]:
    """Like `_normalize_address` but also returns a `used_fallback` flag.
    `used_fallback=True` means scourgify failed to parse and we fell back
    to plain `_norm` + `_fmt_postcode`. Useful for validation reporting.
    """
    addr_dict = {
        'address_line_1': str(street).strip() if street else '',
        'address_line_2': str(line_2).strip() if line_2 else '',
        'city':           str(city).strip()   if city   else '',
        'state':          str(state).strip()  if state  else '',
        'postal_code':    _fmt_postcode(postcode),
    }
    try:
        r = normalize_address_record(addr_dict)
        return NormalizedAddress(
            street=(r.get('address_line_1') or '').upper(),
            line_2=(r.get('address_line_2') or '').upper(),
            city=(r.get('city') or '').upper(),
            state=(r.get('state') or '').upper(),
            postcode=r.get('postal_code') or '',
        ), False
    except Exception as e:
        logger.debug("scourgify failed for %r: %s — falling back to plain normalization",
                     addr_dict, e)
        return NormalizedAddress(
            street=_norm(street),
            line_2=_norm(line_2),
            city=_norm(city),
            state=_norm(state),
            postcode=_fmt_postcode(postcode),
        ), True


def _normalize_address(street, city, state, postcode, line_2=None) -> NormalizedAddress:
    """Normalize address components via usaddress-scourgify, which applies
    USPS Publication 28 rules (suffix abbreviations like Rd↔Road,
    directional collapsing W↔West, punctuation stripping, suite/unit
    splitting). Returns a 5-field NormalizedAddress.

    Both `street` and (optionally) `line_2` are passed to scourgify. When
    suite/unit information already lives on its own line (caller has
    pre-split it), pass it as `line_2`. When it's mixed into the street
    string, scourgify will split it into `address_line_2` automatically.
    Either way the returned NormalizedAddress carries it in `line_2`.

    Falls back to plain `_norm` + `_fmt_postcode` if scourgify can't parse
    the input — better to have an unnormalized key than no key at all.
    Use `_normalize_address_with_status` if you need to know whether
    fallback was used.
    """
    addr, _ = _normalize_address_with_status(street, city, state, postcode, line_2)
    return addr


_LEADING_DOT_OR_SPACE_RE = re.compile(r'^[\s.]+')


def _clean_name(s) -> str:
    """Light cleanup for name lines.

    Removes characters that are NEVER legitimate in a real company name and
    only appear as OCR transcription noise:
      - Pipes (`|`) anywhere — column-divider lines picked up by tesseract.
      - Leading periods (and any leading whitespace) — stray dot at line
        start. Interior and trailing periods are preserved (meaningful in
        "U.S. Foods", "Co.", "Inc.", "St. John's Foods").

    Other punctuation (apostrophes, ampersands, hyphens, colons, slashes,
    commas) is preserved — those characters carry meaning, and the
    downstream stage-2 matcher's `_normalize_name` strips them at compare
    time anyway. Stripping at extraction would only lose display fidelity.
    """
    if not s:
        return ''
    # Pipes anywhere → space, then collapse.
    s = str(s).replace('|', ' ')
    # Strip leading whitespace + periods in any combination.
    s = _LEADING_DOT_OR_SPACE_RE.sub('', s)
    # Collapse internal whitespace, strip remaining edge whitespace.
    return _MULTI_SPACE_RE.sub(' ', s).strip()


# -- Invoice ShipTo extractor (vendor-agnostic) -------------------------------


def extract_invoice_ship_to(
    pdf_path: Path,
    *,
    name: Optional[str],
    line_1: Optional[str],
    line_2: Optional[str],
    city: Optional[str],
    state: Optional[str],
    postcode,
) -> InvoiceExtraction:
    """Build an InvoiceExtraction from already-parsed page-1 fields.

    The caller (a vendor parser) extracts the structured CONSIGNEE block
    from page 1 of the invoice, then hands the fields to this function.
    Address fields go through the same USPS Pub 28 + scourgify pipeline
    as the BOL extractor, so both sources land in the canonical
    NormalizedAddress shape.

    `success` is True iff a NormalizedAddress with a non-empty street was
    produced. `failure_reason` carries a plain-English explanation when
    success=False.
    """
    name_clean = _clean_name(name) if name else None
    name_candidates: list[str] = [name_clean] if name_clean else []

    raw_line = (
        f"  RAW       name={name!r}\n"
        f"            line_1={line_1!r} line_2={line_2!r}\n"
        f"            city={city!r} state={state!r} postcode={postcode!r}"
    )

    addr: Optional[NormalizedAddress] = None
    failure_reason: Optional[str] = None

    if line_1 and city and state and postcode:
        candidate = _normalize_address(line_1, city, state, postcode, line_2=line_2)
        if candidate.street:
            addr = candidate
        else:
            failure_reason = (
                f"Page-1 address fields were present but normalization "
                f"produced an empty street: {candidate!r}"
            )
    else:
        missing = [
            k for k, v in (
                ('line_1', line_1), ('city', city),
                ('state', state), ('postcode', postcode),
            ) if not v
        ]
        failure_reason = (
            f"Page-1 CONSIGNEE block missing required address field(s): "
            f"{missing}"
        )

    ship_to = ShipTo(
        name=name_clean,
        name_candidates=name_candidates,
        address=addr,
        source='page1',
    )

    name_str = (
        f"\n  NAME      {name_clean!r}" if name_clean
        else "\n  NAME      None"
    )
    name_cands_str = (
        f"\n  NAME_CAND {name_candidates}" if name_candidates
        else "\n  NAME_CAND []"
    )
    addr_str = (
        f"\n  ADDRESS   {addr}" if addr is not None
        else "\n  ADDRESS   None"
    )
    diagnostics = raw_line + name_str + name_cands_str + addr_str
    if failure_reason:
        diagnostics = f"FAIL: {failure_reason}\n" + diagnostics

    return InvoiceExtraction(
        pdf_path=pdf_path,
        ship_to=ship_to,
        success=(addr is not None),
        failure_reason=failure_reason,
        diagnostics=diagnostics,
    )


__all__ = [
    'NormalizedAddress',
    'ShipTo',
    'InvoiceExtraction',
    'BolExtraction',
    '_norm',
    '_fmt_postcode',
    '_normalize_address',
    '_normalize_address_with_status',
    '_clean_name',
    'extract_invoice_ship_to',
]
