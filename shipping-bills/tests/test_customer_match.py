"""
Unit tests for the stage-2 customer matcher.

Covers `run_match_for_source` outcomes, the `pick_best` truth table in
`match_invoice_customer`, and the Highland Beef Inventory deny-list.

All tests run against synthetic `CustomerMaster`s built in-memory; no
XLSX I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hbf_shipping.customer_address_map import (
    CustomerMaster,
    MasterEntry,
    MatchMethod,
    NAME_DISAMBIG_THRESHOLD,
    SourceMatchResult,
    match_invoice_customer,
    run_match_for_source,
)
from hbf_shipping.ship_to import (
    BolExtraction,
    InvoiceExtraction,
    NormalizedAddress,
    ShipTo,
)


# ============================================================
# Helpers
# ============================================================


def _addr(street, city='AUSTIN', state='TX', postcode='78701', line_2=''):
    return NormalizedAddress(
        street=street, line_2=line_2, city=city, state=state, postcode=postcode,
    )


def _entry(customer_name, shipto_name, street,
           city='AUSTIN', state='TX', postcode='78701', row=2,
           customer_number=None, line_2=''):
    return MasterEntry(
        customer_name=customer_name,
        shipto_name=shipto_name,
        address=_addr(street, city, state, postcode, line_2=line_2),
        customer_number=customer_number,
        row=row,
    )


def _bol(name, address, name_candidates=None):
    """Build a minimal BolExtraction for matcher input."""
    if name_candidates is None:
        name_candidates = [name] if name else []
    ship_to = ShipTo(
        name=name,
        name_candidates=list(name_candidates),
        address=address,
        source='bol',
    )
    return BolExtraction(
        pdf_path=None, ship_to=ship_to, success=address is not None,
        failure_reason=None, raw_lines=[], csz_line=None,
        diagnostic_path=None, diagnostics='',
    )


def _inv(name, address):
    """Build a minimal InvoiceExtraction for matcher input."""
    ship_to = ShipTo(
        name=name,
        name_candidates=[name] if name else [],
        address=address,
        source='page1',
    )
    return InvoiceExtraction(
        pdf_path=None, ship_to=ship_to, success=address is not None,
        failure_reason=None, diagnostics='',
    )


# ============================================================
# run_match_for_source — per-source outcomes
# ============================================================


def test_no_input_when_extraction_is_none():
    master = CustomerMaster([_entry('Sysco', 'Sysco', '123 MAIN ST')])
    result = run_match_for_source(None, master)
    assert result.method == MatchMethod.NO_INPUT


def test_no_input_when_address_is_none():
    master = CustomerMaster([_entry('Sysco', 'Sysco', '123 MAIN ST')])
    bol = _bol('Sysco', None)  # address None
    result = run_match_for_source(bol, master)
    assert result.method == MatchMethod.NO_INPUT


def test_no_match_when_4tuple_misses():
    master = CustomerMaster([_entry('Sysco', 'Sysco', '123 MAIN ST')])
    bol = _bol('Sysco', _addr('999 NOWHERE ST'))
    result = run_match_for_source(bol, master)
    assert result.method == MatchMethod.NO_MATCH


def test_unique_when_4tuple_matches_one_row():
    master = CustomerMaster([_entry('Sysco Foods', 'Sysco', '123 MAIN ST')])
    bol = _bol('Sysco', _addr('123 MAIN ST'))
    result = run_match_for_source(bol, master)
    assert result.method == MatchMethod.UNIQUE
    assert result.entry.customer_name == 'Sysco Foods'


def test_unique_does_not_check_name_agreement():
    """Locked-on 4-tuple match: name agreement is NOT checked. Even an
    obviously-wrong invoice name wins because we trust the address."""
    master = CustomerMaster([_entry('Sysco Foods', 'Sysco', '123 MAIN ST')])
    bol = _bol('TOTALLY DIFFERENT COMPANY', _addr('123 MAIN ST'))
    result = run_match_for_source(bol, master)
    assert result.method == MatchMethod.UNIQUE
    assert result.entry.customer_name == 'Sysco Foods'


def test_disambiguated_when_multiple_rows_and_name_picks_one():
    """Multi-tenant 4-tuple match → name matrix narrows to one row."""
    master = CustomerMaster([
        _entry('Butner FCC',         'Butner FCC',         '5980 KNAUTH RD', row=10),
        _entry('Butner FCI',         'Butner FCI',         '5980 KNAUTH RD', row=11),
        _entry('Butner FCI Camp',    'Butner FCI Camp',    '5980 KNAUTH RD', row=12),
    ])
    bol = _bol('Butner FCI', _addr('5980 KNAUTH RD'),
               name_candidates=['Butner FCI'])
    result = run_match_for_source(bol, master)
    assert result.method == MatchMethod.DISAMBIGUATED
    assert result.entry.customer_name == 'Butner FCI'
    assert result.score >= NAME_DISAMBIG_THRESHOLD


def test_disambiguated_uses_best_candidate_across_name_candidates():
    """When ship_to has multiple name_candidates, the matrix tries all
    and picks the best (cand × master_shipto_name) pair. Mirrors the
    Victorville-1 case: BOL OCR has garbled top line but a clean
    candidate further down."""
    master = CustomerMaster([
        _entry('Victorville FCI 1',   'Victorville FCI 1',   '13777 AIR EXPY', row=20),
        _entry('Victorville FCI 2',   'Victorville FCI 2',   '13777 AIR EXPY', row=21),
        _entry('Victorville USP',     'Victorville USP',     '13777 AIR EXPY', row=22),
    ])
    # BOL's primary name is OCR garbage; the GOOD name is in candidates.
    bol = _bol(
        'CTORVILLE FCT 4',  # primary — junk OCR
        _addr('13777 AIR EXPY'),
        name_candidates=['CTORVILLE FCT 4', 'VICTORVILLE FCI 1', 'Attn: Commissary FCI-1'],
    )
    result = run_match_for_source(bol, master)
    assert result.method == MatchMethod.DISAMBIGUATED
    assert result.entry.customer_name == 'Victorville FCI 1'


def test_ambiguous_when_no_name_candidate_clears_threshold():
    """Multi-row + name candidates all score below threshold."""
    master = CustomerMaster([
        _entry('Customer A', 'Acme One', '1 MAIN ST', row=2),
        _entry('Customer B', 'Acme Two', '1 MAIN ST', row=3),
    ])
    bol = _bol(
        'Totally Unrelated Name',  # won't fuzzy-match either shipto_name
        _addr('1 MAIN ST'),
        name_candidates=['Totally Unrelated Name'],
    )
    result = run_match_for_source(bol, master)
    assert result.method == MatchMethod.AMBIGUOUS
    assert result.entry is None
    assert len(result.candidates) == 2
    assert result.score < NAME_DISAMBIG_THRESHOLD


def test_address_4tuple_match_ignores_line_2():
    """line_2 is NOT in the 4-tuple key. Master entry with line_2='STE 5'
    matches an invoice with line_2='' on the same street/CSZ."""
    master = CustomerMaster([
        _entry('Acme Corp', 'Acme', '1 MAIN ST', line_2='STE 5'),
    ])
    bol = _bol('Acme', _addr('1 MAIN ST', line_2=''))
    result = run_match_for_source(bol, master)
    assert result.method == MatchMethod.UNIQUE
    assert result.entry.customer_name == 'Acme Corp'


# ============================================================
# match_invoice_customer — pick_best truth table
# ============================================================


@pytest.fixture
def basic_master():
    """Two distinct addresses; non-shared, single-row each."""
    return CustomerMaster([
        _entry('Sysco Foods', 'Sysco', '123 MAIN ST', row=2),
        _entry('Acme Foods',  'Acme',  '999 OTHER RD',
               city='HOUSTON', state='TX', postcode='77002', row=3),
    ])


def test_hard_fail_when_no_address_at_all(basic_master):
    """Both BOL and inv extraction failed to produce an address."""
    result = match_invoice_customer(None, None, basic_master)
    assert result.method == MatchMethod.HARD_FAIL
    assert result.customer_name is None
    assert result.severity == 'severe'
    assert 'no usable address' in result.fail_reason


def test_agree_when_both_resolve_same_customer(basic_master):
    """Both sources matched the same row — return that customer with
    method=AGREE."""
    bol = _bol('Sysco', _addr('123 MAIN ST'))
    inv = _inv('Sysco', _addr('123 MAIN ST'))
    result = match_invoice_customer(inv, bol, basic_master)
    assert result.method == MatchMethod.AGREE
    assert result.customer_name == 'Sysco Foods'
    assert result.severity == 'ok'


def test_bol_only_when_invoice_has_no_address(basic_master):
    bol = _bol('Sysco', _addr('123 MAIN ST'))
    result = match_invoice_customer(None, bol, basic_master)
    assert result.method == MatchMethod.BOL_ONLY
    assert result.customer_name == 'Sysco Foods'


def test_inv_only_when_bol_has_no_address(basic_master):
    inv = _inv('Sysco', _addr('123 MAIN ST'))
    result = match_invoice_customer(inv, None, basic_master)
    assert result.method == MatchMethod.INV_ONLY
    assert result.customer_name == 'Sysco Foods'


def test_bol_wins_disagreement_severe_when_both_unique(basic_master):
    """Both sources locked on UNIQUE 4-tuple matches but at different
    rows. BOL wins; severity is SEVERE (strongest signal of data
    inconsistency)."""
    bol = _bol('Sysco', _addr('123 MAIN ST'))
    inv = _inv('Acme',  _addr('999 OTHER RD',
                              city='HOUSTON', state='TX', postcode='77002'))
    result = match_invoice_customer(inv, bol, basic_master)
    assert result.method == MatchMethod.BOL_WINS_DISAGREEMENT
    assert result.customer_name == 'Sysco Foods'  # BOL's match
    assert result.severity == 'severe'


def test_bol_wins_disagreement_info_when_one_used_disambig():
    """BOL match required name disambig (multi-tenant); inv was UNIQUE
    on a different address. They disagree. BOL wins per policy, but
    severity is INFO (less alarming than UNIQUE-vs-UNIQUE)."""
    master = CustomerMaster([
        _entry('Butner FCC',  'Butner FCC',  '5980 KNAUTH RD', row=10),
        _entry('Butner FCI',  'Butner FCI',  '5980 KNAUTH RD', row=11),
        _entry('Other Customer', 'Other', '111 OTHER RD',
               city='DALLAS', state='TX', postcode='75201', row=99),
    ])
    bol = _bol('Butner FCI', _addr('5980 KNAUTH RD'),
               name_candidates=['Butner FCI'])
    inv = _inv('Other', _addr('111 OTHER RD',
                              city='DALLAS', state='TX', postcode='75201'))
    result = match_invoice_customer(inv, bol, master)
    assert result.method == MatchMethod.BOL_WINS_DISAGREEMENT
    assert result.customer_name == 'Butner FCI'
    assert result.severity == 'info'


def test_hard_fail_when_neither_resolved(basic_master):
    """Both sources had addresses but neither matched anything."""
    bol = _bol('X', _addr('555 MISSING RD'))
    inv = _inv('Y', _addr('666 ALSO MISSING RD'))
    result = match_invoice_customer(inv, bol, basic_master)
    assert result.method == MatchMethod.HARD_FAIL
    assert result.customer_name is None


# ============================================================
# Deny-list (Highland Beef Inventory)
# ============================================================


def test_highland_beef_inventory_match_is_denied():
    """Even though the 4-tuple match is unique, matching the
    'Highland Beef Farms Inventory' pseudo-customer is rejected as an
    error (rows 182, 183 in the real master are internal one-offs)."""
    master = CustomerMaster([
        _entry('Highland Beef Farms Inventory', 'Some Hotel',
               '7201 SW 22ND ST',
               city='DES MOINES', state='IA', postcode='50321', row=182),
    ])
    bol = _bol('Some Hotel', _addr('7201 SW 22ND ST',
                                    city='DES MOINES', state='IA',
                                    postcode='50321'))
    result = match_invoice_customer(None, bol, master)
    assert result.method == MatchMethod.DENIED
    assert result.customer_name is None
    assert result.severity == 'severe'
    assert 'Inventory' in result.fail_reason


def test_normal_customer_is_not_denied():
    """Sanity check: deny-list only fires for the specific HBF Inventory
    pseudo-customer, not for any random customer."""
    master = CustomerMaster([_entry('Some Real Customer', 'Some Real Customer', '1 MAIN ST')])
    bol = _bol('Some Real Customer', _addr('1 MAIN ST'))
    result = match_invoice_customer(None, bol, master)
    assert result.method != MatchMethod.DENIED
    assert result.customer_name == 'Some Real Customer'


# ============================================================
# CustomerMaster public API
# ============================================================


def test_lookup_address_returns_all_rows_at_4tuple():
    e1 = _entry('A', 'A', '1 MAIN ST', row=1)
    e2 = _entry('B', 'B', '1 MAIN ST', row=2)
    e3 = _entry('C', 'C', '2 OTHER ST', row=3)
    master = CustomerMaster([e1, e2, e3])
    rows = master.lookup_address(('1 MAIN ST', 'AUSTIN', 'TX', '78701'))
    assert len(rows) == 2
    assert {r.customer_name for r in rows} == {'A', 'B'}


def test_lookup_customer_name_keys_only_on_name_column():
    """The public by-name lookup keys ONLY on Customer Name (Name col),
    NEVER on shipto_name. Verify a row whose shipto_name differs from
    Customer Name is found by Customer Name and NOT by shipto_name."""
    master = CustomerMaster([
        _entry('Good Source Solutions, Inc.', 'Christian Broadcasting Network',
               '907 LIVE OAK DR',
               city='CHESAPEAKE', state='VA', postcode='23320', row=140),
    ])
    # Lookup by Customer Name → hit
    found = master.lookup_customer_name('Good Source Solutions, Inc.')
    assert len(found) == 1

    # Lookup by ship-to name → MISS (not in by_customer_name index)
    found = master.lookup_customer_name('Christian Broadcasting Network')
    assert len(found) == 0


def test_lookup_customer_name_normalizes():
    master = CustomerMaster([_entry('Sysco Foods, Inc.', 'Sysco', '1 MAIN ST')])
    # Variations should all normalize to the same key.
    assert len(master.lookup_customer_name('SYSCO FOODS, INC.')) == 1
    assert len(master.lookup_customer_name('sysco foods inc')) == 1
    assert len(master.lookup_customer_name('Sysco Foods, Inc.')) == 1
