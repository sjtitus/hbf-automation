"""
Consignee discrepancy detection + CSV export.

When an invoice resolves to a master customer entry, compare the page-1
CONSIGNEE block (name + address the shipper typed into the invoice)
against the resolved master entry. Any difference is recorded so a
human can later harass the shipper to fix what they put in the consignee
block — the goal is for every shipper's invoice CONSIGNEE to literally
echo HBF's canonical name + address, so we can rely on page-1 alone.

Detection rules (per outcome):
  - Skip if the match did not resolve a billable customer (HARD_FAIL,
    DENIED, or no match was attempted).
  - Compare names with `_normalize_name` (the same helper the matcher
    itself uses for disambiguation) — tolerates pure-punctuation noise
    like "Inc." vs "Inc".
  - Compare each NormalizedAddress field (street, line_2, city, state,
    postcode) with strict `!=`. Both sides are USPS-Pub-28 normalized
    via scourgify, so any leftover difference is a real one.
  - If page-1 produced no parseable address at all, every populated
    master address field counts as a discrepancy (the shipper printed
    nothing usable).

The CSV is always written (header-only when nothing differs) — an empty
file IS a result ("this run, every consignee matched").
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .customer_address_map import _normalize_name


@dataclass(frozen=True)
class ConsigneeDiscrepancy:
    """One row of the consignee discrepancy report. Per-invoice; the
    run-level columns (Run ID, Shipping Company) are added at write
    time."""
    invoice_file: str
    match_method: str

    master_customer_name: str
    master_customer_number: Optional[str]
    master_row: int
    master_street: str
    master_line_2: str
    master_city: str
    master_state: str
    master_postcode: str

    # Page-1 actual values. Empty strings (not None) when page-1 had no
    # address. `page1_name` is None when page-1 produced no name at all.
    page1_name: Optional[str]
    page1_street: str
    page1_line_2: str
    page1_city: str
    page1_state: str
    page1_postcode: str

    name_differs: bool
    street_differs: bool
    line_2_differs: bool
    city_differs: bool
    state_differs: bool
    postcode_differs: bool


def compare_consignee_to_master(outcome) -> Optional[ConsigneeDiscrepancy]:
    """Compare the page-1 CONSIGNEE block to the resolved master entry.

    Returns None when there is nothing to report:
      - the match did not resolve a billable customer (HARD_FAIL/DENIED
        or no match attempted), OR
      - every comparable field is equal.

    Returns a populated `ConsigneeDiscrepancy` otherwise.
    """
    match = outcome.match
    if match is None or not match.success:
        return None

    master = match.matched_entry

    page1_ship_to = outcome.inv.ship_to if outcome.inv is not None else None
    page1_name = page1_ship_to.name if page1_ship_to is not None else None
    page1_addr = page1_ship_to.address if page1_ship_to is not None else None

    name_differs = (
        _normalize_name(page1_name or '') != _normalize_name(master.customer_name)
    )

    if page1_addr is None:
        page1_street = page1_line_2 = page1_city = page1_state = page1_postcode = ''
        street_differs = bool(master.address.street)
        line_2_differs = bool(master.address.line_2)
        city_differs = bool(master.address.city)
        state_differs = bool(master.address.state)
        postcode_differs = bool(master.address.postcode)
    else:
        page1_street = page1_addr.street
        page1_line_2 = page1_addr.line_2
        page1_city = page1_addr.city
        page1_state = page1_addr.state
        page1_postcode = page1_addr.postcode

        street_differs = page1_addr.street != master.address.street
        line_2_differs = page1_addr.line_2 != master.address.line_2
        city_differs = page1_addr.city != master.address.city
        state_differs = page1_addr.state != master.address.state
        postcode_differs = page1_addr.postcode != master.address.postcode

    any_differs = (
        name_differs or street_differs or line_2_differs
        or city_differs or state_differs or postcode_differs
    )
    if not any_differs:
        return None

    return ConsigneeDiscrepancy(
        invoice_file=outcome.pdf_path.name,
        match_method=match.method,
        master_customer_name=master.customer_name,
        master_customer_number=master.customer_number,
        master_row=master.row,
        master_street=master.address.street,
        master_line_2=master.address.line_2,
        master_city=master.address.city,
        master_state=master.address.state,
        master_postcode=master.address.postcode,
        page1_name=page1_name,
        page1_street=page1_street,
        page1_line_2=page1_line_2,
        page1_city=page1_city,
        page1_state=page1_state,
        page1_postcode=page1_postcode,
        name_differs=name_differs,
        street_differs=street_differs,
        line_2_differs=line_2_differs,
        city_differs=city_differs,
        state_differs=state_differs,
        postcode_differs=postcode_differs,
    )


DISCREPANCY_HEADERS = [
    "Run ID",
    "Shipping Company",
    "Invoice File",
    "Match Method",
    "Customer Name",
    "Customer Number",
    "Master Row",
    "Master Street",
    "Master Line 2",
    "Master City",
    "Master State",
    "Master Postcode",
    "Page-1 Name",
    "Page-1 Street",
    "Page-1 Line 2",
    "Page-1 City",
    "Page-1 State",
    "Page-1 Postcode",
    "Name Differs",
    "Street Differs",
    "Line 2 Differs",
    "City Differs",
    "State Differs",
    "Postcode Differs",
    "Current Consignee Name and Address",
    "Suggested Consignee Name and Address",
]


def _format_consignee_line(
    *, name: str, street: str, line_2: str,
    city: str, state: str, postcode: str,
) -> str:
    """Compose a single-line consignee in standard US-mailing format
    (`Name, Street, [Line 2,] City, ST ZIP`). Empty fields are skipped —
    leading commas would look broken, and the per-field `*_Differs`
    columns already convey what's missing.

    Single-line / comma-delimited (not multi-line with embedded
    newlines) so the cell is robust to every CSV reader, including
    naive line-based tools.
    """
    parts = [v for v in (name, street, line_2) if v]
    state_zip = ' '.join(p for p in (state, postcode) if p)
    csz = ', '.join(p for p in (city, state_zip) if p)
    if csz:
        parts.append(csz)
    return ', '.join(parts)


def _row_from_discrepancy(
    d: ConsigneeDiscrepancy, run_id: str, shipping_company: str,
) -> dict:
    def _yn(b: bool) -> str:
        return 'yes' if b else 'no'
    return {
        "Run ID": run_id,
        "Shipping Company": shipping_company,
        "Invoice File": d.invoice_file,
        "Match Method": d.match_method,
        "Customer Name": d.master_customer_name,
        "Customer Number": d.master_customer_number or '',
        "Master Row": d.master_row,
        "Master Street": d.master_street,
        "Master Line 2": d.master_line_2,
        "Master City": d.master_city,
        "Master State": d.master_state,
        "Master Postcode": d.master_postcode,
        "Page-1 Name": d.page1_name or '',
        "Page-1 Street": d.page1_street,
        "Page-1 Line 2": d.page1_line_2,
        "Page-1 City": d.page1_city,
        "Page-1 State": d.page1_state,
        "Page-1 Postcode": d.page1_postcode,
        "Name Differs": _yn(d.name_differs),
        "Street Differs": _yn(d.street_differs),
        "Line 2 Differs": _yn(d.line_2_differs),
        "City Differs": _yn(d.city_differs),
        "State Differs": _yn(d.state_differs),
        "Postcode Differs": _yn(d.postcode_differs),
        "Current Consignee Name and Address": _format_consignee_line(
            name=d.page1_name or '',
            street=d.page1_street,
            line_2=d.page1_line_2,
            city=d.page1_city,
            state=d.page1_state,
            postcode=d.page1_postcode,
        ),
        "Suggested Consignee Name and Address": _format_consignee_line(
            name=d.master_customer_name,
            street=d.master_street,
            line_2=d.master_line_2,
            city=d.master_city,
            state=d.master_state,
            postcode=d.master_postcode,
        ),
    }


def write_discrepancies_csv(
    discrepancies: Iterable[ConsigneeDiscrepancy],
    path: str | Path,
    *,
    run_id: str,
    shipping_company: str,
) -> Path:
    """Write consignee discrepancies to a CSV at `path`. Always emits the
    header even when `discrepancies` is empty. Creates parent dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DISCREPANCY_HEADERS)
        writer.writeheader()
        for d in discrepancies:
            writer.writerow(_row_from_discrepancy(d, run_id, shipping_company))
    return path
