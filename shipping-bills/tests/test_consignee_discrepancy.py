"""
Unit tests for `compare_consignee_to_master`.

The function is pure: it consumes an InvoiceOutcome and returns either
None (nothing to report) or a populated ConsigneeDiscrepancy. These
tests construct InvoiceOutcomes by hand to exercise each branch.
"""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

from hbf_shipping.consignee_discrepancy import (
    DISCREPANCY_HEADERS,
    ConsigneeDiscrepancy,
    _format_consignee_line,
    compare_consignee_to_master,
    write_discrepancies_csv,
)
from hbf_shipping.customer_address_map import (
    InvoiceMatchResult,
    MasterEntry,
    MatchMethod,
    SourceMatchResult,
)
from hbf_shipping.ship_to import (
    InvoiceExtraction,
    NormalizedAddress,
    ShipTo,
)


# ============================================================
# Fixtures (helpers)
# ============================================================


def _master_entry(
    customer_name='Test Customer',
    customer_number='999999',
    row=42,
    street='100 MAIN ST',
    line_2='',
    city='AUSTIN',
    state='TX',
    postcode='78701',
):
    return MasterEntry(
        customer_name=customer_name,
        shipto_name=customer_name,
        address=NormalizedAddress(
            street=street, line_2=line_2, city=city,
            state=state, postcode=postcode,
        ),
        customer_number=customer_number,
        row=row,
    )


def _inv_extraction(
    *, name='Test Customer',
    street='100 MAIN ST', line_2='', city='AUSTIN',
    state='TX', postcode='78701',
):
    return InvoiceExtraction(
        pdf_path=Path('/fake/test.pdf'),
        ship_to=ShipTo(
            name=name,
            name_candidates=[name] if name else [],
            address=NormalizedAddress(
                street=street, line_2=line_2, city=city,
                state=state, postcode=postcode,
            ),
            source='page1',
        ),
        success=True,
        failure_reason=None,
        diagnostics='',
    )


def _outcome(*, inv, match):
    return SimpleNamespace(
        pdf_path=Path('/fake/test.pdf'),
        invoice_data={},
        inv=inv,
        bol=None,
        match=match,
        log_path=Path('/fake/test.log'),
        processing_start='', processing_end='',
        fail_step=None, fail_message=None, fail_detail=None,
        bill_entry=None,
    )


def _success_match(entry, method=MatchMethod.AGREE):
    """Build a minimal InvoiceMatchResult that successfully resolved
    to the given master entry."""
    src = SourceMatchResult(method='unique', entry=entry)
    return InvoiceMatchResult(
        matched_entry=entry,
        method=method,
        bol=src,
        inv=src,
        severity='ok',
    )


def _failed_match(method, entry=None):
    """HARD_FAIL or DENIED — match.success is False."""
    src = SourceMatchResult(method='no_match')
    return InvoiceMatchResult(
        matched_entry=entry,
        method=method,
        bol=src, inv=src,
        severity='severe' if method == MatchMethod.HARD_FAIL else 'info',
        fail_reason='test',
    )


# ============================================================
# Tests
# ============================================================


def test_identical_name_and_address_returns_none():
    """Page-1 matches master exactly — nothing to report."""
    master = _master_entry()
    inv = _inv_extraction()
    outcome = _outcome(inv=inv, match=_success_match(master))
    assert compare_consignee_to_master(outcome) is None


def test_punctuation_only_name_diff_returns_none():
    """`_normalize_name` collapses punctuation, so 'Test Customer Inc.'
    vs master 'Test Customer Inc' is NOT a discrepancy."""
    master = _master_entry(customer_name='Test Customer Inc')
    inv = _inv_extraction(name='Test Customer Inc.')
    outcome = _outcome(inv=inv, match=_success_match(master))
    assert compare_consignee_to_master(outcome) is None


def test_meaningfully_different_name_flags_name_differs():
    """Names that don't normalize to the same token sequence are
    real discrepancies."""
    master = _master_entry(customer_name='Texas Dept. of Criminal Justice')
    inv = _inv_extraction(name='Umoja Health')  # totally different name
    outcome = _outcome(inv=inv, match=_success_match(master))

    d = compare_consignee_to_master(outcome)
    assert d is not None
    assert d.name_differs is True
    assert d.street_differs is False
    assert d.master_customer_name == 'Texas Dept. of Criminal Justice'
    assert d.page1_name == 'Umoja Health'


def test_different_street_flags_street_differs():
    """The Scotlynn '1621 TX-75' vs master '1621 HWY 75 N' case."""
    master = _master_entry(street='1621 HWY 75 N')
    inv = _inv_extraction(street='1621 TX-75')
    outcome = _outcome(inv=inv, match=_success_match(master))

    d = compare_consignee_to_master(outcome)
    assert d is not None
    assert d.street_differs is True
    assert d.name_differs is False
    assert d.city_differs is False
    assert d.master_street == '1621 HWY 75 N'
    assert d.page1_street == '1621 TX-75'


def test_different_postcode_flags_postcode_differs():
    """Surgical: only postcode differs, every other flag is False."""
    master = _master_entry(postcode='78701')
    inv = _inv_extraction(postcode='78702')
    outcome = _outcome(inv=inv, match=_success_match(master))

    d = compare_consignee_to_master(outcome)
    assert d is not None
    assert d.postcode_differs is True
    assert not (d.name_differs or d.street_differs or d.line_2_differs
                or d.city_differs or d.state_differs)


def test_page1_address_missing_flags_every_populated_master_field():
    """When page-1 produced a name but no parseable address, every
    populated master address field is a discrepancy."""
    master = _master_entry(line_2='STE 200')
    # Page-1 ship_to with no address (e.g., consignee block extraction failed)
    inv = InvoiceExtraction(
        pdf_path=Path('/fake/test.pdf'),
        ship_to=ShipTo(
            name='Test Customer',
            name_candidates=['Test Customer'],
            address=None,
            source='page1',
        ),
        success=False,
        failure_reason='consignee block missing CSZ',
        diagnostics='',
    )
    outcome = _outcome(inv=inv, match=_success_match(master))

    d = compare_consignee_to_master(outcome)
    assert d is not None
    assert d.name_differs is False  # name agreed
    assert d.street_differs is True
    assert d.line_2_differs is True
    assert d.city_differs is True
    assert d.state_differs is True
    assert d.postcode_differs is True
    assert d.page1_street == ''
    assert d.page1_line_2 == ''
    assert d.page1_postcode == ''


def test_page1_extraction_entirely_none_treats_all_fields_as_diffs():
    """When `outcome.inv` itself is None (page-1 extractor crashed
    before populating anything), discrepancy reflects every master field."""
    master = _master_entry()
    outcome = _outcome(inv=None, match=_success_match(master))

    d = compare_consignee_to_master(outcome)
    assert d is not None
    assert d.name_differs is True
    assert d.street_differs is True
    assert d.page1_name is None


def test_hard_fail_returns_none():
    """No matched entry at all — no canonical to compare against."""
    inv = _inv_extraction()
    outcome = _outcome(
        inv=inv, match=_failed_match(MatchMethod.HARD_FAIL),
    )
    assert compare_consignee_to_master(outcome) is None


def test_denied_returns_none():
    """Match resolved to deny-listed row (HBF Inventory pseudo-customer);
    not a billable customer, nothing to report."""
    denied_entry = _master_entry(customer_name='Highland Beef Farms Inventory')
    inv = _inv_extraction(name='Highland Beef Farms Inventory')
    outcome = _outcome(
        inv=inv, match=_failed_match(MatchMethod.DENIED, entry=denied_entry),
    )
    assert compare_consignee_to_master(outcome) is None


def test_match_is_none_returns_none():
    """No match attempted (e.g., parse failed earlier in the pipeline)."""
    outcome = _outcome(inv=None, match=None)
    assert compare_consignee_to_master(outcome) is None


def test_csv_writer_emits_header_only_when_no_discrepancies(tmp_path):
    """An empty discrepancy list writes a header-only CSV."""
    out = tmp_path / 'consignee_discrepancies.csv'
    write_discrepancies_csv(
        [], out, run_id='run-0', shipping_company='Test Co',
    )
    assert out.exists()
    with out.open() as f:
        rows = list(csv.reader(f))
    assert len(rows) == 1  # header only
    assert rows[0] == DISCREPANCY_HEADERS


def test_csv_writer_emits_row_with_run_level_columns(tmp_path):
    """The writer threads run_id + shipping_company into each row, and
    the Current/Suggested columns carry full single-line consignees."""
    master = _master_entry(street='1621 HWY 75 N')
    inv = _inv_extraction(street='1621 TX-75')
    outcome = _outcome(inv=inv, match=_success_match(master))
    d = compare_consignee_to_master(outcome)

    out = tmp_path / 'consignee_discrepancies.csv'
    write_discrepancies_csv(
        [d], out, run_id='run-42', shipping_company='Acme Logistics',
    )
    with out.open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    r = rows[0]
    assert r['Run ID'] == 'run-42'
    assert r['Shipping Company'] == 'Acme Logistics'
    assert r['Street Differs'] == 'yes'
    assert r['Name Differs'] == 'no'
    assert r['Master Street'] == '1621 HWY 75 N'
    assert r['Page-1 Street'] == '1621 TX-75'

    # Current cell: page-1 fields composed in mailing-line format
    assert r['Current Consignee Name and Address'] == (
        'Test Customer, 1621 TX-75, AUSTIN, TX 78701'
    )
    # Suggested cell: master (gold standard) fields composed identically
    assert r['Suggested Consignee Name and Address'] == (
        'Test Customer, 1621 HWY 75 N, AUSTIN, TX 78701'
    )


def test_suggested_includes_master_line_2_when_present_and_skips_when_master_omits(tmp_path):
    """Gold-standard rule: the Suggested cell carries master.line_2 if
    the master has one; the Current cell carries page-1.line_2 if
    page-1 has one. They're independent — Current may show a line_2
    that Suggested doesn't (the page-1 'STE 200' shouldn't have been
    written)."""
    master = _master_entry(line_2='')  # master is canonical: NO line_2
    # Page-1 has line_2 'STE 200' that the shipper added but shouldn't have.
    inv = _inv_extraction(line_2='STE 200')
    outcome = _outcome(inv=inv, match=_success_match(master))
    d = compare_consignee_to_master(outcome)

    out = tmp_path / 'consignee_discrepancies.csv'
    write_discrepancies_csv(
        [d], out, run_id='r', shipping_company='Co',
    )
    with out.open() as f:
        r = next(csv.DictReader(f))

    assert r['Line 2 Differs'] == 'yes'
    # Page-1 had STE 200 → present in Current
    assert 'STE 200' in r['Current Consignee Name and Address']
    # Master has no line_2 → absent from Suggested
    assert 'STE 200' not in r['Suggested Consignee Name and Address']

    # Now flip it: master has STE 200, page-1 doesn't.
    master2 = _master_entry(line_2='STE 200')
    inv2 = _inv_extraction(line_2='')
    outcome2 = _outcome(inv=inv2, match=_success_match(master2))
    d2 = compare_consignee_to_master(outcome2)

    out2 = tmp_path / 'consignee_discrepancies_2.csv'
    write_discrepancies_csv(
        [d2], out2, run_id='r', shipping_company='Co',
    )
    with out2.open() as f:
        r2 = next(csv.DictReader(f))
    assert 'STE 200' in r2['Suggested Consignee Name and Address']
    assert 'STE 200' not in r2['Current Consignee Name and Address']


def test_format_consignee_line_skips_empty_fields():
    """The single-line composer skips empty fields rather than emitting
    leading/double commas. The per-field *_Differs columns already
    convey missingness, so the formatted cell stays clean."""
    # All fields present
    s = _format_consignee_line(
        name='Acme Co', street='100 Main St', line_2='STE 200',
        city='AUSTIN', state='TX', postcode='78701',
    )
    assert s == 'Acme Co, 100 Main St, STE 200, AUSTIN, TX 78701'

    # No line_2 — common case
    s = _format_consignee_line(
        name='Acme Co', street='100 Main St', line_2='',
        city='AUSTIN', state='TX', postcode='78701',
    )
    assert s == 'Acme Co, 100 Main St, AUSTIN, TX 78701'

    # Page-1 had no name (parser failed on it) — Current cell drops it
    s = _format_consignee_line(
        name='', street='100 Main St', line_2='',
        city='AUSTIN', state='TX', postcode='78701',
    )
    assert s == '100 Main St, AUSTIN, TX 78701'

    # Page-1 produced nothing usable — empty cell
    s = _format_consignee_line(
        name='', street='', line_2='', city='', state='', postcode='',
    )
    assert s == ''

    # Just name + street (no city/state/zip)
    s = _format_consignee_line(
        name='Acme Co', street='100 Main St', line_2='',
        city='', state='', postcode='',
    )
    assert s == 'Acme Co, 100 Main St'
