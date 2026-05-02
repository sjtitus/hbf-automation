"""
Unit tests for the customer-master validation phase.

Covers:
- `parse_al1` for each AL1 format variant.
- Each validation rule produces the expected violations on a synthetic
  XLSX with intentional defects.
- `strict=True` raises `MasterValidationError`; `strict=False` returns
  successfully.
- Validation report file is written with the expected sections + counts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from hbf_shipping.customer_address_map import (
    parse_al1,
    load_address_to_customers,
    MasterValidationError,
    RULE_REQUIRED_FIELDS,
    RULE_TRIPLE_UNIQUE,
    RULE_AL1_STRICT_PARSE,
    RULE_NO_DASH_AL1,
    RULE_SCOURGIFY_FALL,
    validate_master,
    _RowRecord,
)
from hbf_shipping.ship_to import _normalize_address_with_status


# ============================================================
# parse_al1
# ============================================================


@pytest.mark.parametrize("al1,expected", [
    # Standard format with comma separator + ATTN
    ("Allenwood FCI - 203437, ATTN: Commissary/Whse.",
     ("Allenwood FCI", "203437", "ATTN: Commissary/Whse.", True)),
    # Standard format with comma separator + non-ATTN extra
    ("A.F. Wendling's Food Service - 206492, Receiving Dock",
     ("A.F. Wendling's Food Service", "206492", "Receiving Dock", True)),
    # Number only, no extra
    ("Aliceville FCI - 205975",
     ("Aliceville FCI", "205975", None, True)),
    # Missing-space-after-dash format (real row from master). Whitespace
    # before the dash counts as the separator; no whitespace required after.
    ("Jacksonville Correctional Center -205997",
     ("Jacksonville Correctional Center", "205997", None, True)),
    # Internal-dash name preserved by greedy `(.+)` (the bug-fix)
    ("Avery-Mitchell Corr. Inst. - 205834, Deliver to Loading Dock",
     ("Avery-Mitchell Corr. Inst.", "205834", "Deliver to Loading Dock", True)),
    ("Cash-WA Distributing Co - 205911, Kearney Warehouse",
     ("Cash-WA Distributing Co", "205911", "Kearney Warehouse", True)),
    # Multi-dash name (greedy keeps last `- <digits>`)
    ("Fort Dix FCI - Camp - 208027, ATTN: Commissary",
     ("Fort Dix FCI - Camp", "208027", "ATTN: Commissary", True)),
    ("Odessa High School - School Nutrition - 208087",
     ("Odessa High School - School Nutrition", "208087", None, True)),
    # Comma-less ATTN format
    ("Ashland FCI Camp - 206954 ATTN: Commissary/Whse.",
     ("Ashland FCI Camp", "206954", "ATTN: Commissary/Whse.", True)),
    # No-dash row → whole AL1 is the shipping name
    ("Freshlunches, Inc. dba Unity Meals",
     ("Freshlunches, Inc. dba Unity Meals", None, None, True)),
    # Dash-with-letter (no number) → unparseable, parsed_ok=False
    ("Dairyfood - C",
     ("Dairyfood - C", None, None, False)),
    # Missing-dash-before-number (real failing case from the master)
    ("Lompoc FCC - Camp 208015",
     ("Lompoc FCC - Camp 208015", None, None, False)),
    # `#` prefix on number → fails strict parse
    ("Victorville USP - #204512, ATTN: Warehouse/Commissary (USP)",
     ("Victorville USP - #204512, ATTN: Warehouse/Commissary (USP)", None, None, False)),
    # Internal-hyphen ONLY (no whitespace around dash) → no customer-number
    # separator pattern → treated as no-customer-number row, parsed_ok=True.
    # (The dash in 'GSNA-Jekyll' is internal to the word, not a separator.)
    ("ABF Freight c/o PeakXpo GSNA-Jekyll Island",
     ("ABF Freight c/o PeakXpo GSNA-Jekyll Island", None, None, True)),
    # Multiple internal hyphens, no separator → still no-customer-number.
    ("Two-Word Name-With Hyphens Inc.",
     ("Two-Word Name-With Hyphens Inc.", None, None, True)),
    # Empty / None edge cases
    ("", ("", None, None, True)),
    (None, ("", None, None, True)),
])
def test_parse_al1(al1, expected):
    assert parse_al1(al1) == expected


# ============================================================
# Synthetic-XLSX helper
# ============================================================


def _make_master(tmp_path: Path, rows: list[dict]) -> Path:
    """Build an XLSX with the standard 6-column header + the given data
    rows. Cell values are taken from each row dict (missing keys → None).
    """
    wb = Workbook()
    ws = wb.active
    headers = ['Name', 'AddressLine1', 'AddressLine2', 'City', 'State', 'Postcode']
    ws.append(headers)
    for r in rows:
        ws.append([r.get(h) for h in headers])
    path = tmp_path / 'test_master.xlsx'
    wb.save(path)
    return path


# ============================================================
# Validation — full-load tests on synthetic data
# ============================================================


@pytest.fixture
def synthetic_master_path(tmp_path):
    """A small master file with intentional violations of every rule."""
    rows = [
        # row 2 — clean baseline (Allenwood)
        {'Name': 'Allenwood FCI', 'AddressLine1': 'Allenwood FCI - 203437, ATTN: Commissary',
         'AddressLine2': '7700 White Deer Run Rd', 'City': 'Allenwood', 'State': 'PA',
         'Postcode': '17810'},
        # row 3 — internal-dash name (verifies parser preserves it)
        {'Name': 'Avery-Mitchell Corr. Inst.', 'AddressLine1': 'Avery-Mitchell Corr. Inst. - 205834, Deliver to Loading Dock',
         'AddressLine2': '123 Mountain View Rd', 'City': 'Spruce Pine', 'State': 'NC',
         'Postcode': '28777'},
        # row 4 — TRIPLE-UNIQUE violation (duplicate of row 2)
        {'Name': 'Allenwood FCI', 'AddressLine1': 'Allenwood FCI - 999999',
         'AddressLine2': '7700 White Deer Run Rd', 'City': 'Allenwood', 'State': 'PA',
         'Postcode': '17810'},
        # row 5 — REQUIRED-FIELDS violation (missing City/State/Postcode)
        {'Name': 'Star Foods', 'AddressLine1': 'Star Foods',
         'AddressLine2': 'Customer Will Call', 'City': None, 'State': None,
         'Postcode': None},
        # row 6 — AL1-STRICT-PARSE violation (missing dash before number)
        {'Name': 'Lompoc FCC', 'AddressLine1': 'Lompoc FCC - Camp 208015',
         'AddressLine2': '3705 W Klein Blvd', 'City': 'Lompoc', 'State': 'CA',
         'Postcode': '93436'},
        # row 7 — AL1-STRICT-PARSE violation (`#` prefix on number)
        {'Name': 'Victorville USP', 'AddressLine1': 'Victorville USP - #204512, ATTN: Warehouse',
         'AddressLine2': '13777 Air Expressway', 'City': 'Victorville', 'State': 'CA',
         'Postcode': '92394'},
        # row 8 — NO-DASH-AL1 (legitimate)
        {'Name': 'Freshlunches, Inc. dba Unity Meals',
         'AddressLine1': 'Freshlunches, Inc. dba Unity Meals',
         'AddressLine2': '6800 Owensmouth Ave Ste 350', 'City': 'Canoga Park',
         'State': 'CA', 'Postcode': '91303'},
        # row 9 — comma-less ATTN format (parses OK; no violation)
        {'Name': 'Ashland FCI Camp', 'AddressLine1': 'Ashland FCI Camp - 206954 ATTN: Commissary/Whse.',
         'AddressLine2': '300 St Rt 716', 'City': 'Ashland', 'State': 'KY',
         'Postcode': '41101'},
        # row 10 — SCOURGIFY-FALLBACK candidate (highway address)
        {'Name': 'Butner FCI', 'AddressLine1': 'Butner FCI - 204487',
         'AddressLine2': 'Old Highway 75', 'City': 'Butner', 'State': 'NC',
         'Postcode': '27509'},
    ]
    return _make_master(tmp_path, rows)


def _violations_by_rule(violations):
    out = {}
    for v in violations:
        out.setdefault(v.rule, []).append(v)
    return out


def test_required_fields_violation(synthetic_master_path, tmp_path):
    load_address_to_customers(synthetic_master_path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    # row 5 (Star Foods) lacks CSZ → required-fields hard violation
    assert 'row   5' in log
    assert RULE_REQUIRED_FIELDS in log
    assert 'missing required field(s)' in log


def test_triple_unique_violation(synthetic_master_path, tmp_path):
    load_address_to_customers(synthetic_master_path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    # row 4 duplicates the (Name, shipto_name, address) triple from row 2
    assert RULE_TRIPLE_UNIQUE in log
    assert 'row   4' in log
    assert 'first seen at row 2' in log


def test_al1_strict_parse_violations(synthetic_master_path, tmp_path):
    load_address_to_customers(synthetic_master_path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    # row 6 (Lompoc missing-dash) and row 7 (Victorville `#`-prefix)
    assert RULE_AL1_STRICT_PARSE in log
    assert 'row   6' in log
    assert 'row   7' in log


def test_no_dash_al1_violation(synthetic_master_path, tmp_path):
    load_address_to_customers(synthetic_master_path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    # row 8 (Freshlunches) has no dash
    assert RULE_NO_DASH_AL1 in log
    assert 'row   8' in log


def test_strict_mode_raises(synthetic_master_path, tmp_path):
    """strict=True aborts with MasterValidationError; report still written."""
    with pytest.raises(MasterValidationError):
        load_address_to_customers(synthetic_master_path,
                                   strict=True, log_dir=tmp_path)
    # Report exists despite the abort.
    log_path = tmp_path / 'customer_master_validation.log'
    assert log_path.exists()
    log = log_path.read_text()
    assert 'aborted via --strict-master' in log


def test_non_strict_returns_dict(synthetic_master_path, tmp_path):
    """strict=False returns a dict despite hard violations."""
    result = load_address_to_customers(synthetic_master_path,
                                        strict=False, log_dir=tmp_path)
    # row 5 excluded (missing CSZ); rows with valid CSZ make it in.
    assert isinstance(result, dict)
    assert len(result) >= 1


def test_required_fields_excluded_from_dict(synthetic_master_path, tmp_path):
    """Rows with required-fields violations don't appear in the result dict."""
    result = load_address_to_customers(synthetic_master_path, log_dir=tmp_path)
    all_names = {e.name for entries in result.values() for e in entries}
    assert 'Star Foods' not in all_names  # row 5 was excluded


def test_internal_dash_name_preserved(synthetic_master_path, tmp_path):
    """The parser bug-fix: row 3 'Avery-Mitchell Corr. Inst.' should
    survive intact, not be truncated to 'Avery'."""
    result = load_address_to_customers(synthetic_master_path, log_dir=tmp_path)
    found = False
    for entries in result.values():
        for e in entries:
            if e.name == 'Avery-Mitchell Corr. Inst.':
                assert e.shipto_name == 'Avery-Mitchell Corr. Inst.', \
                    f"shipto_name should preserve the internal dash, got {e.shipto_name!r}"
                found = True
    assert found, "Avery-Mitchell row was not loaded"


def test_validation_report_summary_counts(synthetic_master_path, tmp_path):
    """The report's summary block reflects the right counts."""
    load_address_to_customers(synthetic_master_path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    # Row 5 = required_fields. Row 4 = triple_unique. Both hard.
    assert 'Hard violations: 2' in log
    # Soft: AL1 strict-parse fail (rows 6, 7), no-dash AL1 (row 8),
    # scourgify fallback (probably row 10 'Old Highway 75').
    # We don't pin exact soft count (scourgify is sensitive to its data
    # model); just sanity-check Soft warnings appear.
    assert 'Soft warnings:' in log


def test_validation_report_no_violations_pass(tmp_path):
    """A clean master produces a PASS verdict with zero violations."""
    rows = [
        {'Name': 'Clean Customer', 'AddressLine1': 'Clean Customer - 123456',
         'AddressLine2': '100 Main St', 'City': 'Springfield', 'State': 'IL',
         'Postcode': '62701'},
    ]
    path = _make_master(tmp_path, rows)
    result = load_address_to_customers(path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    assert 'Verdict:         PASS' in log
    assert 'Hard violations: 0' in log
    assert len(result) == 1


def test_log_dir_none_skips_log_writing(synthetic_master_path):
    """When log_dir is None, no log file is written; loading still works."""
    result = load_address_to_customers(synthetic_master_path, log_dir=None)
    assert isinstance(result, dict)


def test_internal_hyphen_only_is_no_customer_number_not_strict_fail(tmp_path):
    """A row whose only dash is internal to a word (e.g. 'GSNA-Jekyll')
    has no customer-number separator pattern and must be flagged by
    `no_dash_al1` (no customer number), NOT by `al1_strict_parse`
    (which is for malformed customer-number attempts).
    """
    rows = [
        # Internal hyphen only — no separator. Mirrors ABF Freight row 171.
        {'Name': 'ABF Freight', 'AddressLine1': 'ABF Freight c/o PeakXpo GSNA-Jekyll Island',
         'AddressLine2': '100 Main St', 'City': 'Jekyll Island', 'State': 'GA',
         'Postcode': '31527'},
    ]
    path = _make_master(tmp_path, rows)
    load_address_to_customers(path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    # Should be flagged as no_dash_al1 (no customer number)
    assert 'rule=no_dash_al1' in log
    assert 'row   2' in log
    # Should NOT be flagged as al1_strict_parse (it's not a malformed
    # customer-number attempt)
    assert '[WARN] AL1 strict-parse failure' not in log
    # The OK section header for that rule SHOULD be present (zero violations)
    assert '[OK] AL1 strict-parse failure' in log
