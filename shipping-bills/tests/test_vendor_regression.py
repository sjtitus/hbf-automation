"""
End-to-end regression test for every vendor.

For each PDF in tests/fixtures/<vendor>/ that has a paired *.expected.json
sibling, we run the full pipeline (parse → customer lookup → build BillEntry)
and assert the live output equals the golden.

Adding a new test case = drop a PDF into the right vendor subfolder and run
`python3 tools/refresh_goldens.py` to capture the golden. Both the PDF and
the JSON are gitignored (they reference real customer data).

When you intentionally change pipeline behavior, regenerate affected goldens
with the same script and review the diff before committing.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from hbf_shipping.customer_lookup import CustomerValidator
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
def customer_validator():
    return CustomerValidator()


@pytest.mark.parametrize("vendor_name,pdf_path,golden_path", _discover_cases())
def test_vendor_regression(vendor_name, pdf_path, golden_path, customer_validator):
    vendor = VENDORS[vendor_name]
    invoice_data, _reasons = vendor.parse_invoice(str(pdf_path))
    cust = customer_validator.validate_customer(invoice_data["consignee"])
    bill = vendor.build_bill_entry(invoice_data, cust["matched_name"])

    expected = json.loads(golden_path.read_text())
    actual = {
        "invoice_data": _serialize_invoice_data(invoice_data),
        "customer_matched": cust["matched_name"],
        "bill_entry": bill.to_dict(),
    }
    assert actual == expected


def _serialize_invoice_data(d: dict) -> dict:
    """Datetimes → ISO date strings; everything else passes through."""
    return {
        k: (v.strftime("%Y-%m-%d") if isinstance(v, datetime) else v)
        for k, v in d.items()
    }
