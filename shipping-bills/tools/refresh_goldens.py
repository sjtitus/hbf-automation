#!/usr/bin/env python3
"""
Regenerate .expected.json golden files for the regression test suite.

Usage:
    python3 tools/refresh_goldens.py                        # all vendors, all PDFs
    python3 tools/refresh_goldens.py badger                 # one vendor only
    python3 tools/refresh_goldens.py badger 0064452         # one PDF (substring match on filename)

Each PDF in tests/fixtures/<vendor>/ gets a paired <stem>.expected.json
capturing the parsed invoice fields, the stage-2 customer match, and the
resulting BillEntry. The pytest suite asserts pipeline output against
these goldens.

The match runs the production code path: page-1 ShipTo + page-2 BOL OCR
both feed `match_invoice_customer`. Including BOL is intentional — the
BOL extractor is the most brittle component, and golden tests are how
we catch regressions in it.

Always review the diff (`git diff tests/fixtures/`) before committing — the
whole point of goldens is that they only change when you mean them to.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# Make the project package importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hbf_shipping import bol_ship_to  # noqa: E402
from hbf_shipping.customer_address_map import (  # noqa: E402
    load_master,
    match_invoice_customer,
)
from hbf_shipping.vendors import VENDORS  # noqa: E402


FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _serialize_invoice_data(d: dict) -> dict:
    return {
        k: (v.strftime("%Y-%m-%d") if isinstance(v, datetime) else v)
        for k, v in d.items()
    }


def _serialize_match(match) -> dict:
    """Capture the disambiguating identifiers + per-source match state.
    `customer_number` and `master_row` are populated only when the
    matcher resolved to a master row."""
    e = match.matched_entry
    return {
        "customer_name": e.customer_name if e else None,
        "customer_number": e.customer_number if e else None,
        "master_row": e.row if e else None,
        "match_method": match.method,
        "severity": match.severity,
        "bol_method": match.bol.method,
        "page1_method": match.inv.method,
    }


def _golden_for(vendor, pdf_path: Path, master) -> dict:
    invoice_data, _reasons = vendor.parse_invoice(str(pdf_path))
    inv = vendor.extract_invoice_ship_to(pdf_path, invoice_data)
    bol = bol_ship_to.extract_ship_to(pdf_path)
    match = match_invoice_customer(inv, bol, master)
    bill = vendor.build_bill_entry(invoice_data, match.customer_name)
    return {
        "invoice_data": _serialize_invoice_data(invoice_data),
        "customer_match": _serialize_match(match),
        "bill_entry": bill.to_dict(),
    }


def main():
    args = sys.argv[1:]
    vendor_filter = args[0] if len(args) >= 1 else None
    pdf_filter = args[1] if len(args) >= 2 else None

    if not FIXTURES.exists():
        print(f"no fixtures directory: {FIXTURES}", file=sys.stderr)
        sys.exit(1)

    master = load_master(strict=False, log_dir=None)
    written = 0

    for vendor_dir in sorted(FIXTURES.iterdir()):
        if not vendor_dir.is_dir() or vendor_dir.name not in VENDORS:
            continue
        if vendor_filter and vendor_dir.name != vendor_filter:
            continue

        vendor = VENDORS[vendor_dir.name]
        for pdf in sorted(vendor_dir.glob("*.pdf")):
            if pdf_filter and pdf_filter not in pdf.name:
                continue
            golden = _golden_for(vendor, pdf, master)
            golden_path = pdf.with_suffix(".expected.json")
            golden_path.write_text(json.dumps(golden, indent=2) + "\n")
            print(f"wrote {golden_path.relative_to(FIXTURES.parent.parent)}")
            written += 1

    if written == 0:
        print("no PDFs matched the filter — nothing written", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
