"""
End-to-end integration test for the stage-2 pipeline.

Wires up: synthetic customer master + stub vendor → Pipeline →
finalize → summary.csv + bills CSV + manifest.json. Verifies the
artifact-emitting layer (cli wiring) without depending on real PDFs
or OCR.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from hbf_shipping import bol_ship_to
from hbf_shipping.bill_entry import BillEntry
from hbf_shipping.pipeline import Pipeline
from hbf_shipping.processing_log import HEADERS as SUMMARY_HEADERS
from hbf_shipping.ship_to import (
    BolExtraction,
    InvoiceExtraction,
    NormalizedAddress,
    ShipTo,
)


# ============================================================
# Fixtures
# ============================================================


def _write_synthetic_master(path: Path) -> None:
    """Single-row customer master at a known address."""
    wb = Workbook()
    ws = wb.active
    ws.append(['Name', 'AddressLine1', 'AddressLine2', 'City', 'State', 'Postcode'])
    ws.append([
        'Test Customer',
        'Test Customer - 999999',
        '100 Main St',
        'Austin',
        'TX',
        '78701',
    ])
    wb.save(path)


def _make_stub_vendor():
    """Stub vendor that produces deterministic invoice data, ship-to,
    and bill entry. Address matches the synthetic master so the
    matcher resolves UNIQUE on page-1."""

    address = NormalizedAddress(
        street='100 MAIN ST', line_2='', city='AUSTIN',
        state='TX', postcode='78701',
    )

    def parse_invoice(pdf_path):
        return (
            {
                'invoice_number': '0001234',
                'invoice_date': None,
                'ship_date': None,
                'shipper': 'Stub Shipper',
                'consignee': 'Test Customer',
                'so_number': 'SO-99999',
                'total_amount': 123.45,
                'past_due_date': None,
            },
            {},
        )

    def extract_invoice_ship_to(pdf_path, invoice_data):
        return InvoiceExtraction(
            pdf_path=pdf_path,
            ship_to=ShipTo(
                name='Test Customer',
                name_candidates=['Test Customer'],
                address=address,
                source='page1',
            ),
            success=True,
            failure_reason=None,
            diagnostics='',
        )

    def build_bill_entry(invoice_data, customer_name):
        return BillEntry(
            vendor='Stub Vendor',
            bill_date='01/01/2026',
            due_date='01/02/2026',
            bill_number=invoice_data['invoice_number'],
            category='Test Category',
            description=invoice_data['so_number'],
            amount=invoice_data['total_amount'],
            customer=customer_name,
            memo=invoice_data['so_number'],
        )

    return SimpleNamespace(
        SHIPPING_COMPANY='Stub Shipping Co',
        REQUIRED_FIELDS=('invoice_number', 'so_number', 'total_amount'),
        BOL_PROFILE=bol_ship_to.BADGER_PROFILE,
        parse_invoice=parse_invoice,
        extract_invoice_ship_to=extract_invoice_ship_to,
        build_bill_entry=build_bill_entry,
    )


def _stub_bol_no_input(pdf_path, diagnostic_dir=None):
    """BOL extractor stub that yields a NO_INPUT result (no usable
    address). The matcher then runs page-1-only and resolves via INV_ONLY."""
    return BolExtraction(
        pdf_path=pdf_path,
        ship_to=ShipTo(name='', name_candidates=[], address=None, source='bol'),
        success=False,
        failure_reason='stub: no BOL extracted',
        raw_lines=[], csz_line=None,
        diagnostic_path=None, diagnostics='',
    )


@pytest.fixture
def stub_run(tmp_path, monkeypatch):
    """Set up a fully-stubbed pipeline run. Yields a dict with the
    pipeline + run_dir + customer_master_path so tests can drive
    process_invoice / finalize and inspect artifacts."""
    # Load synthetic master via DEFAULT_ADDRESS_FILE override.
    master_path = tmp_path / 'master.xlsx'
    _write_synthetic_master(master_path)
    monkeypatch.setattr(
        'hbf_shipping.customer_address_map.DEFAULT_ADDRESS_FILE',
        master_path,
    )

    # Stub the BOL extractor so we don't try to OCR a real PDF.
    monkeypatch.setattr(bol_ship_to, 'extract_ship_to', _stub_bol_no_input)

    # CWD-relative artifacts (quickbooks-imports/) need to land under tmp_path
    # so we don't pollute the project root.
    monkeypatch.chdir(tmp_path)

    run_dir = tmp_path / 'logs' / 'test-run-0001'
    run_dir.mkdir(parents=True)

    vendor = _make_stub_vendor()
    pipeline = Pipeline(
        vendor=vendor,
        run_id='test-run-0001',
        run_dir=run_dir,
        vendor_slug='stub',
    )

    return SimpleNamespace(
        pipeline=pipeline,
        run_dir=run_dir,
        tmp_path=tmp_path,
    )


# ============================================================
# Tests
# ============================================================


def test_pipeline_writes_all_artifacts_on_success(stub_run):
    """Single successful invoice → summary.csv (1 row, SUCCESS),
    bills CSV (1 row), manifest.json (indexes both)."""
    fake_pdf = stub_run.tmp_path / 'fake-invoice.pdf'
    fake_pdf.write_bytes(b'')  # parse_invoice is stubbed; content irrelevant

    ok = stub_run.pipeline.process_invoice(fake_pdf)
    assert ok, "process_invoice should return True on full-pipeline success"

    artifacts = stub_run.pipeline.finalize(dry_run=False)

    # All four artifacts written
    assert artifacts['summary_csv'].exists()
    assert artifacts['bills_csv'].exists()
    assert artifacts['consignee_discrepancies_csv'].exists()
    assert artifacts['manifest'].exists()

    # Summary CSV: header matches expected schema; one SUCCESS row
    with artifacts['summary_csv'].open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    row = rows[0]
    assert list(row.keys()) == SUMMARY_HEADERS
    assert row['Status'] == 'SUCCESS'
    assert row['Invoice File'] == 'fake-invoice.pdf'
    assert row['Bill Number'] == '0001234'
    assert row['SO Number'] == 'SO-99999'
    assert row['Total Amount'] == '123.45'
    assert row['Customer Name'] == 'Test Customer'
    assert row['Customer Number'] == '999999'
    assert row['Match Method'] == 'inv_only'
    assert row['Match Severity'] == 'ok'
    assert row['Page-1 Method'] == 'unique'
    assert row['BOL Method'] == 'no_input'
    assert row['Fail Step'] == 'N/A'

    # Bills CSV: one row, customer/invoice/SO threaded through
    with artifacts['bills_csv'].open() as f:
        bills = list(csv.DictReader(f))
    assert len(bills) == 1
    assert bills[0]['Bill no.'] == '0001234'
    assert bills[0]['Customer / Project'] == 'Test Customer'
    assert bills[0]['Description'] == 'SO-99999'
    assert bills[0]['Amount'] == '123.45'

    # Manifest: indexes the run, references all CSVs
    manifest = json.loads(artifacts['manifest'].read_text())
    assert manifest['run_id'] == 'test-run-0001'
    assert manifest['vendor'] == 'stub'
    assert manifest['totals'] == {'total': 1, 'succeeded': 1, 'failed': 0}
    assert manifest['artifacts']['bills_csv'] is not None
    assert manifest['artifacts']['summary_csv'].endswith('summary.csv')
    assert manifest['artifacts']['consignee_discrepancies_csv'].endswith(
        'consignee_discrepancies.csv'
    )
    assert len(manifest['artifacts']['invoice_logs']) == 1

    # Consignee discrepancies: stub vendor's page-1 ship_to matches the
    # synthetic master exactly, so the file is header-only (no rows).
    with artifacts['consignee_discrepancies_csv'].open() as f:
        disc_rows = list(csv.DictReader(f))
    assert disc_rows == []


def test_pipeline_dry_run_skips_bills_csv(stub_run):
    """`dry_run=True` writes summary + manifest + consignee_discrepancies
    but NOT bills CSV. Manifest's bills_csv field is None."""
    fake_pdf = stub_run.tmp_path / 'fake-invoice.pdf'
    fake_pdf.write_bytes(b'')
    stub_run.pipeline.process_invoice(fake_pdf)

    artifacts = stub_run.pipeline.finalize(dry_run=True)

    assert artifacts['summary_csv'].exists()
    assert artifacts['consignee_discrepancies_csv'].exists()
    assert artifacts['manifest'].exists()
    assert 'bills_csv' not in artifacts

    manifest = json.loads(artifacts['manifest'].read_text())
    assert manifest['artifacts']['bills_csv'] is None
    assert manifest['artifacts']['consignee_discrepancies_csv'].endswith(
        'consignee_discrepancies.csv'
    )


def test_pipeline_writes_consignee_discrepancy_row_when_page1_disagrees_with_master(
    stub_run, monkeypatch,
):
    """When the stub vendor's page-1 ship_to has a name that disagrees
    with the master (address still matches, so the matcher resolves
    INV_ONLY), a row lands in consignee_discrepancies.csv flagging the
    name diff and carrying a suggested consignee block composed from
    master fields."""
    # Override the stub vendor's page-1 extractor: same address (so
    # the 4-tuple lookup still resolves to the synthetic master row),
    # different name.
    def disagreeing_extract(pdf_path, invoice_data):
        return InvoiceExtraction(
            pdf_path=pdf_path,
            ship_to=ShipTo(
                name='Different Name LLC',
                name_candidates=['Different Name LLC'],
                address=NormalizedAddress(
                    street='100 MAIN ST', line_2='', city='AUSTIN',
                    state='TX', postcode='78701',
                ),
                source='page1',
            ),
            success=True,
            failure_reason=None,
            diagnostics='',
        )
    monkeypatch.setattr(
        stub_run.pipeline.vendor,
        'extract_invoice_ship_to',
        disagreeing_extract,
    )

    fake_pdf = stub_run.tmp_path / 'disagreeing-invoice.pdf'
    fake_pdf.write_bytes(b'')
    stub_run.pipeline.process_invoice(fake_pdf)

    artifacts = stub_run.pipeline.finalize(dry_run=False)

    with artifacts['consignee_discrepancies_csv'].open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    r = rows[0]
    assert r['Invoice File'] == 'disagreeing-invoice.pdf'
    assert r['Customer Name'] == 'Test Customer'           # master canonical
    assert r['Page-1 Name'] == 'Different Name LLC'         # what shipper put
    assert r['Name Differs'] == 'yes'
    assert r['Street Differs'] == 'no'                      # both '100 MAIN ST'
    assert r['City Differs'] == 'no'                        # both 'AUSTIN'
    # Current cell: what the shipper wrote on the invoice
    assert r['Current Consignee Name and Address'] == (
        'Different Name LLC, 100 MAIN ST, AUSTIN, TX 78701'
    )
    # Suggested cell: master (gold standard); ready for paste-into-email
    assert r['Suggested Consignee Name and Address'] == (
        'Test Customer, 100 MAIN ST, AUSTIN, TX 78701'
    )


def test_pipeline_validate_fields_failure_records_fail_step(stub_run, monkeypatch):
    """When parse_invoice returns a row missing a required field, the
    invoice fails with fail_step=validate_fields and the summary row
    captures the missing field plus the parser's reason."""
    def parse_returning_missing_field(pdf_path):
        return (
            {
                'invoice_number': '0001234',
                'invoice_date': None,
                'ship_date': None,
                'shipper': 'Stub Shipper',
                'consignee': 'Test Customer',
                'so_number': None,                # ← missing required
                'total_amount': 123.45,
                'past_due_date': None,
            },
            {'so_number': "no match for pattern 'S[O0]-<digits>'"},
        )

    monkeypatch.setattr(
        stub_run.pipeline.vendor, 'parse_invoice', parse_returning_missing_field,
    )

    fake_pdf = stub_run.tmp_path / 'broken.pdf'
    fake_pdf.write_bytes(b'')

    ok = stub_run.pipeline.process_invoice(fake_pdf)
    assert not ok

    artifacts = stub_run.pipeline.finalize(dry_run=False)

    with artifacts['summary_csv'].open() as f:
        row = next(csv.DictReader(f))
    assert row['Status'] == 'FAIL'
    assert row['Fail Step'] == 'validate_fields'
    assert 'so_number' in row['Fail Message']
    assert "no match for pattern" in row['Fail Detail']

    # No bill entry → no bills CSV emitted
    assert 'bills_csv' not in artifacts
