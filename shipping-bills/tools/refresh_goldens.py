#!/usr/bin/env python3
"""
Regenerate .expected.json golden files for the regression test suite.

Usage:
    python3 tools/refresh_goldens.py                        # all vendors, all PDFs
    python3 tools/refresh_goldens.py badger                 # one vendor only
    python3 tools/refresh_goldens.py badger 0064452         # one PDF (substring match on filename)

Each PDF in tests/fixtures/<vendor>/ gets a paired <stem>.expected.json
capturing the parsed invoice fields, the matched customer, and the resulting
BillEntry. The pytest suite asserts pipeline output against these goldens.

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

from hbf_shipping.customer_lookup import CustomerValidator  # noqa: E402
from hbf_shipping.vendors import VENDORS  # noqa: E402


FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def _serialize_invoice_data(d: dict) -> dict:
    return {
        k: (v.strftime("%Y-%m-%d") if isinstance(v, datetime) else v)
        for k, v in d.items()
    }


def _golden_for(vendor, pdf_path: Path, validator: CustomerValidator) -> dict:
    invoice_data, _reasons = vendor.parse_invoice(str(pdf_path))
    cust = validator.validate_customer(invoice_data["consignee"])
    bill = vendor.build_bill_entry(invoice_data, cust["matched_name"])
    return {
        "invoice_data": _serialize_invoice_data(invoice_data),
        "customer_matched": cust["matched_name"],
        "bill_entry": bill.to_dict(),
    }


def main():
    args = sys.argv[1:]
    vendor_filter = args[0] if len(args) >= 1 else None
    pdf_filter = args[1] if len(args) >= 2 else None

    if not FIXTURES.exists():
        print(f"no fixtures directory: {FIXTURES}", file=sys.stderr)
        sys.exit(1)

    validator = CustomerValidator()
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
            golden = _golden_for(vendor, pdf, validator)
            golden_path = pdf.with_suffix(".expected.json")
            golden_path.write_text(json.dumps(golden, indent=2) + "\n")
            print(f"wrote {golden_path.relative_to(FIXTURES.parent.parent)}")
            written += 1

    if written == 0:
        print("no PDFs matched the filter — nothing written", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
