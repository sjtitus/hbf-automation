"""
End-to-end regression test for every vendor.

For each PDF in tests/fixtures/<vendor>/ that has a paired *.expected.json
sibling, we run the production code path (parse → page-1 ShipTo → BOL
OCR → stage-2 match → build BillEntry) and assert the live output
equals the golden.

Adding a new test case = drop a PDF into the right vendor subfolder and
run `python3 tools/refresh_goldens.py` to capture the golden. Both the
PDF and the JSON are gitignored (they reference real customer data).

When you intentionally change pipeline behavior, regenerate affected
goldens with the same script and review the diff before committing.

The BOL extractor is included on purpose. Tesseract on the same PDF is
deterministic; if a future change to `bol_ship_to.py` (or a tesseract
upgrade) shifts what gets extracted, the regression suite is the
mechanism that surfaces it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from hbf_shipping import bol_ship_to
from hbf_shipping.customer_address_map import (
    load_master,
    match_invoice_customer,
)
from hbf_shipping.vendors import VENDORS


FIXTURES = Path(__file__).parent / "fixtures"


def _discover_cases():
    """Yield (vendor_name, pdf_path, golden_path) for every PDF that has a
    paired .expected.json sibling. PDFs without goldens are silently
    skipped — run tools/refresh_goldens.py to populate them.
    """
    cases = []
    if not FIXTURES.exists():
        return cases
    for vendor_dir in sorted(FIXTURES.iterdir()):
        if not vendor_dir.is_dir() or vendor_dir.name not in VENDORS:
            continue
        for pdf in sorted(vendor_dir.glob("*.pdf")):
            golden = pdf.with_suffix(".expected.json")
            if golden.exists():
                cases.append(pytest.param(
                    vendor_dir.name, pdf, golden,
                    id=f"{vendor_dir.name}/{pdf.stem}",
                ))
    return cases


@pytest.fixture(scope="session")
def customer_master():
    """Load the production customer master once for the whole suite.
    `log_dir=None` skips writing a stray validation report under the
    pytest cwd."""
    return load_master(strict=False, log_dir=None)


def _serialize_invoice_data(d: dict) -> dict:
    """Datetimes → ISO date strings; everything else passes through."""
    return {
        k: (v.strftime("%Y-%m-%d") if isinstance(v, datetime) else v)
        for k, v in d.items()
    }


def _serialize_match(match) -> dict:
    """Capture the disambiguating identifiers + per-source match state.
    Mirror of the same helper in tools/refresh_goldens.py — kept inline
    in both rather than introducing a tests-only helper module for two
    callers."""
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


@pytest.mark.parametrize("vendor_name,pdf_path,golden_path", _discover_cases())
def test_vendor_regression(vendor_name, pdf_path, golden_path, customer_master):
    vendor = VENDORS[vendor_name]
    invoice_data, _reasons = vendor.parse_invoice(str(pdf_path))
    inv = vendor.extract_invoice_ship_to(pdf_path, invoice_data)
    bol = bol_ship_to.extract_ship_to(pdf_path)
    match = match_invoice_customer(inv, bol, customer_master)
    bill = vendor.build_bill_entry(invoice_data, match.customer_name)

    expected = json.loads(golden_path.read_text())
    actual = {
        "invoice_data": _serialize_invoice_data(invoice_data),
        "customer_match": _serialize_match(match),
        "bill_entry": bill.to_dict(),
    }
    assert actual == expected
