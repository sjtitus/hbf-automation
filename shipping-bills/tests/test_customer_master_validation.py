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
    RULE_MISSING_CUSTOMER_NUMBER,
    RULE_DUPLICATE_CUSTOMER_NUMBER,
    RULE_MALFORMED_CUSTOMER_NUMBER,
    RULE_AL1_STRICT_PARSE,
    RULE_NO_DASH_AL1,
    validate_master,
    _RowRecord,
)


# ============================================================
# parse_al1
# ============================================================


@pytest.mark.parametrize("al1,expected", [
    # Standard format with comma separator + ATTN
    ("Allenwood FCI - 203437, ATTN: Commissary/Whse.",
     ("Allenwood FCI", "203437", "ATTN: Commissary/Whse.", True, False)),
    # Standard format with comma separator + non-ATTN extra
    ("A.F. Wendling's Food Service - 206492, Receiving Dock",
     ("A.F. Wendling's Food Service", "206492", "Receiving Dock", True, False)),
    # Number only, no extra
    ("Aliceville FCI - 205975",
     ("Aliceville FCI", "205975", None, True, False)),
    # Missing-space-after-dash format (real row from master). Whitespace
    # before the dash counts as the separator; no whitespace required after.
    ("Jacksonville Correctional Center -205997",
     ("Jacksonville Correctional Center", "205997", None, True, False)),
    # Internal-dash name preserved by greedy `(.+)` (the bug-fix)
    ("Avery-Mitchell Corr. Inst. - 205834, Deliver to Loading Dock",
     ("Avery-Mitchell Corr. Inst.", "205834", "Deliver to Loading Dock", True, False)),
    ("Cash-WA Distributing Co - 205911, Kearney Warehouse",
     ("Cash-WA Distributing Co", "205911", "Kearney Warehouse", True, False)),
    # Multi-dash name (greedy keeps last `- <digits>`)
    ("Fort Dix FCI - Camp - 208027, ATTN: Commissary",
     ("Fort Dix FCI - Camp", "208027", "ATTN: Commissary", True, False)),
    ("Odessa High School - School Nutrition - 208087",
     ("Odessa High School - School Nutrition", "208087", None, True, False)),
    # Comma-less ATTN format
    ("Ashland FCI Camp - 206954 ATTN: Commissary/Whse.",
     ("Ashland FCI Camp", "206954", "ATTN: Commissary/Whse.", True, False)),
    # No-dash row → whole AL1 is the shipping name
    ("Freshlunches, Inc. dba Unity Meals",
     ("Freshlunches, Inc. dba Unity Meals", None, None, True, False)),
    # Dash-with-letter (no number) → strict-parse fails AND no 6-digit
    # fallback available → unparseable.
    ("Dairyfood - C",
     ("Dairyfood - C", None, None, False, False)),
    # Missing-dash-before-number (real failing case from the master).
    # Strict parse fails BUT the 6-digit number is recovered from
    # post-separator text → recovered_via_fallback=True.
    ("Lompoc FCC - Camp 208015",
     ("Lompoc FCC", "208015", None, False, True)),
    # `#` prefix on number → strict parse fails BUT 6-digit fallback
    # recovers the number.
    ("Victorville USP - #204512, ATTN: Warehouse/Commissary (USP)",
     ("Victorville USP", "204512", None, False, True)),
    # Strict-fail with NO 6-digit anywhere after the separator → not
    # recovered. Validation will fire al1_strict_parse + missing_customer_number.
    ("Foo - bar baz",
     ("Foo - bar baz", None, None, False, False)),
    # Internal-hyphen ONLY (no whitespace around dash) → no customer-number
    # separator pattern → treated as no-customer-number row, parsed_ok=True.
    # (The dash in 'GSNA-Jekyll' is internal to the word, not a separator.)
    ("ABF Freight c/o PeakXpo GSNA-Jekyll Island",
     ("ABF Freight c/o PeakXpo GSNA-Jekyll Island", None, None, True, False)),
    # Multiple internal hyphens, no separator → still no-customer-number.
    ("Two-Word Name-With Hyphens Inc.",
     ("Two-Word Name-With Hyphens Inc.", None, None, True, False)),
    # Empty / None edge cases
    ("", ("", None, None, True, False)),
    (None, ("", None, None, True, False)),
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
    # Message dumps the full row so HBF can identify it at a glance.
    assert 'row data:' in log
    assert "Name='Star Foods'" in log


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
    # Hard violations on the fixture:
    #   row 4: triple_unique (duplicate of row 2)
    #   row 5: required_fields_present (Star Foods CSZ blank)
    #   row 5: missing_customer_number (Star Foods AL1 no dash)
    #   row 8: missing_customer_number (Freshlunches no dash)
    # Rows 6 (Lompoc) and 7 (Victorville) NO LONGER fire
    # missing_customer_number — the loose 6-digit fallback recovers
    # their customer numbers; they're flagged soft via
    # malformed_customer_number_format instead.
    # Total hard = 4.
    assert 'Hard violations: 4' in log
    # Soft warnings include: malformed-format (rows 6, 7), AL1
    # strict-parse fail (rows 6, 7), no-dash AL1 (rows 5, 8). Don't pin
    # exact count — just sanity-check the section appears.
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


# ============================================================
# New customer-number rules
# ============================================================


def test_missing_customer_number_fires_for_no_dash_row(tmp_path):
    """Hard rule: every row needs a parseable HBF customer number.
    A no-dash row gets parsed as `customer_number=None` and fires
    RULE_MISSING_CUSTOMER_NUMBER (in addition to the soft no_dash_al1
    rule)."""
    rows = [
        {'Name': 'Freshlunches', 'AddressLine1': 'Freshlunches',
         'AddressLine2': '100 Main St', 'City': 'Austin',
         'State': 'TX', 'Postcode': '78701'},
    ]
    path = _make_master(tmp_path, rows)
    load_address_to_customers(path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    assert 'rule=missing_customer_number' in log
    assert 'row   2' in log
    assert 'no parseable HBF customer number' in log


def test_malformed_customer_number_format_recovers_lompoc_style(tmp_path):
    """A Lompoc-style row ('Camp 208015' missing the dash before the
    number) fails strict parse but the 6-digit fallback recovers the
    customer number. Fires `malformed_customer_number_format` (soft)
    and `al1_strict_parse` (soft); does NOT fire
    `missing_customer_number` (hard) — the number is usable."""
    rows = [
        {'Name': 'Lompoc FCC', 'AddressLine1': 'Lompoc FCC - Camp 208015',
         'AddressLine2': '3705 W Klein Blvd', 'City': 'Lompoc',
         'State': 'CA', 'Postcode': '93436'},
    ]
    path = _make_master(tmp_path, rows)
    load_address_to_customers(path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    assert 'rule=malformed_customer_number_format' in log
    assert 'rule=al1_strict_parse' in log
    # Customer number was recovered, so the missing-number rule must NOT fire.
    assert '[FAIL] Every row has an HBF customer number' not in log
    assert '[OK] Every row has an HBF customer number' in log
    # Recovered number surfaced in the message
    assert '208015' in log


def test_malformed_customer_number_format_recovers_hash_prefix(tmp_path):
    """Victorville-style row ('- #204512, ATTN...') has a `#` prefix
    that breaks strict parse but the 6-digit fallback recovers the
    number. Same pattern as the Lompoc case."""
    rows = [
        {'Name': 'Victorville USP',
         'AddressLine1': 'Victorville USP - #204512, ATTN: Warehouse',
         'AddressLine2': '13777 Air Expressway', 'City': 'Victorville',
         'State': 'CA', 'Postcode': '92394'},
    ]
    path = _make_master(tmp_path, rows)
    load_address_to_customers(path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    assert 'rule=malformed_customer_number_format' in log
    assert '204512' in log


def test_malformed_format_does_not_fire_for_clean_row(tmp_path):
    """Sanity check: a cleanly-formatted row does NOT fire the
    malformed-format rule."""
    rows = [
        {'Name': 'Allenwood FCI',
         'AddressLine1': 'Allenwood FCI - 203437, ATTN: Commissary',
         'AddressLine2': '7700 White Deer Run Rd', 'City': 'Allenwood',
         'State': 'PA', 'Postcode': '17810'},
    ]
    path = _make_master(tmp_path, rows)
    load_address_to_customers(path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    assert '[OK] Customer number recovered from malformed AL1 format' in log


def test_strict_parse_fails_with_no_recoverable_number_still_hard(tmp_path):
    """When a separator is present, strict parse fails, AND there's
    no 6-digit number anywhere in the post-separator text, the row
    falls through to `missing_customer_number` (hard) — fallback
    couldn't rescue it."""
    rows = [
        # 'Dairyfood - C': separator present but only a single letter
        # follows; no 6-digit fallback available.
        {'Name': 'Dairyfood', 'AddressLine1': 'Dairyfood - C',
         'AddressLine2': '100 Main St', 'City': 'Austin',
         'State': 'TX', 'Postcode': '78701'},
    ]
    path = _make_master(tmp_path, rows)
    load_address_to_customers(path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    assert 'rule=missing_customer_number' in log
    assert 'rule=al1_strict_parse' in log
    # Did NOT recover, so malformed-format must NOT fire for this row.
    assert '[OK] Customer number recovered from malformed AL1 format' in log


def test_missing_customer_number_does_not_fire_for_clean_row(tmp_path):
    """Sanity check: a row with a clean parseable customer number
    does NOT trigger RULE_MISSING_CUSTOMER_NUMBER."""
    rows = [
        {'Name': 'Allenwood FCI',
         'AddressLine1': 'Allenwood FCI - 203437, ATTN: Commissary',
         'AddressLine2': '7700 White Deer Run Rd', 'City': 'Allenwood',
         'State': 'PA', 'Postcode': '17810'},
    ]
    path = _make_master(tmp_path, rows)
    load_address_to_customers(path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    # The OK header line for the rule should be present; no violation
    # entries.
    assert '[OK] Every row has an HBF customer number' in log


def test_duplicate_customer_number_fires_when_two_rows_share(tmp_path):
    """Soft rule: each customer number should appear on at most one
    row. (Per HBF, some duplicates are legitimate — they review the
    flagged entries manually.)"""
    rows = [
        {'Name': 'Customer A', 'AddressLine1': 'Customer A - 999999',
         'AddressLine2': '100 Main St', 'City': 'Austin',
         'State': 'TX', 'Postcode': '78701'},
        # Same customer number on a different row — violation.
        {'Name': 'Customer B', 'AddressLine1': 'Customer B - 999999',
         'AddressLine2': '200 Oak Ave', 'City': 'Houston',
         'State': 'TX', 'Postcode': '77002'},
    ]
    path = _make_master(tmp_path, rows)
    load_address_to_customers(path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    assert 'rule=duplicate_customer_number' in log
    assert 'row   3' in log     # row 3 is the duplicate
    assert 'also on row 2' in log


def test_duplicate_customer_number_is_soft_not_hard(tmp_path):
    """Duplicate-customer-number is a SOFT rule (HBF says some
    duplicates are legitimate). A duplicate-only fixture should NOT
    abort under strict mode."""
    rows = [
        {'Name': 'Customer A', 'AddressLine1': 'Customer A - 999999',
         'AddressLine2': '100 Main St', 'City': 'Austin',
         'State': 'TX', 'Postcode': '78701'},
        {'Name': 'Customer B', 'AddressLine1': 'Customer B - 999999',
         'AddressLine2': '200 Oak Ave', 'City': 'Houston',
         'State': 'TX', 'Postcode': '77002'},
    ]
    path = _make_master(tmp_path, rows)
    # Strict mode must NOT raise — duplicate is soft.
    result = load_address_to_customers(path, strict=True, log_dir=tmp_path)
    assert isinstance(result, dict)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    # Confirms it's filed under soft warnings, not hard violations.
    assert 'rule=duplicate_customer_number' in log
    assert 'Hard violations: 0' in log


def test_duplicate_customer_number_does_not_fire_for_unique_numbers(tmp_path):
    """Sanity check: distinct customer numbers across rows produce no
    duplicate-number violations."""
    rows = [
        {'Name': 'Customer A', 'AddressLine1': 'Customer A - 111111',
         'AddressLine2': '100 Main St', 'City': 'Austin',
         'State': 'TX', 'Postcode': '78701'},
        {'Name': 'Customer B', 'AddressLine1': 'Customer B - 222222',
         'AddressLine2': '200 Oak Ave', 'City': 'Houston',
         'State': 'TX', 'Postcode': '77002'},
    ]
    path = _make_master(tmp_path, rows)
    load_address_to_customers(path, log_dir=tmp_path)
    log = (tmp_path / 'customer_master_validation.log').read_text()
    assert '[OK] Customer numbers are unique' in log


def test_missing_customer_number_is_hard(tmp_path):
    """Missing-customer-number is hard — strict mode aborts when a
    row has no recoverable customer number at all."""
    rows = [
        {'Name': 'Missing Number', 'AddressLine1': 'Missing Number',
         'AddressLine2': '100 Main St', 'City': 'Austin',
         'State': 'TX', 'Postcode': '78701'},
    ]
    path = _make_master(tmp_path, rows)
    with pytest.raises(MasterValidationError):
        load_address_to_customers(path, strict=True, log_dir=tmp_path)
