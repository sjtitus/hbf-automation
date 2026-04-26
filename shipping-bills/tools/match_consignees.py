#!/usr/bin/env python3
"""
Run the Badger parser + customer-address lookup against every PDF in
tests/fixtures/badger/. For each invoice, print the extracted consignee
fields and the lookup result. On no_match / multi_match_unresolved, print
the best near-miss candidate so the failure mode is visible.

Run from project root:
    python3 tools/match_consignees.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hbf_shipping.customer_address_map import (  # noqa: E402
    _normalize_address,
    load_address_to_customers,
    lookup_with_name_fallback,
)
from hbf_shipping.vendors.badger.parser import parse_invoice  # noqa: E402


FIXTURES = Path('tests/fixtures/badger')


def fmt(v):
    return repr(v) if v is not None else 'None'


def fmt_addr(a):
    return f"{a.street!r} / {a.city!r} / {a.state!r} / {a.postcode!r}"


def print_pairs(pairs):
    for i, (addr, e) in enumerate(pairs, 1):
        print(f"      [{i}] {e.name}")
        print(f"          address      : {fmt_addr(addr)}")
        print(f"          line_1_clean : {e.line_1_clean!r}")


def main() -> int:
    addr_map = load_address_to_customers()
    pdfs = sorted(FIXTURES.glob('*.pdf'))
    print(f"Loaded {len(addr_map)} addresses; matching against {len(pdfs)} Badger PDFs")

    summary = {
        'address_exact': 0, 'address_fuzzy': 0,
        'address_disambiguated_by_name': 0, 'name_fallback': 0,
        'multi_match_unresolved': 0, 'no_match': 0, 'parse_fail': 0,
    }

    for pdf in pdfs:
        print(f"\n{'=' * 78}")
        print(f"  {pdf.name}")
        print('=' * 78)

        try:
            data, reasons = parse_invoice(str(pdf))
        except Exception as e:
            print(f"  PARSE EXCEPTION: {e}")
            summary['parse_fail'] += 1
            continue

        consignee = data.get('consignee')
        al1       = data.get('consignee_address_line_1')
        al2       = data.get('consignee_address_line_2')
        city      = data.get('consignee_city')
        state     = data.get('consignee_state')
        pc        = data.get('consignee_postcode')

        print(f"  PDF consignee : {fmt(consignee)}")
        print(f"  PDF address   : {fmt(al1)} / {fmt(al2)} / {fmt(city)} / {fmt(state)} / {fmt(pc)}")

        for k in ('consignee_address_line_1', 'consignee_address_line_2',
                  'consignee_city', 'consignee_state', 'consignee_postcode'):
            r = reasons.get(k)
            if r:
                print(f"    [{k}] reason: {r}")

        if not (al1 and city and state and pc):
            print(f"\n  >>> NO MATCH: required address fields missing from PDF")
            summary['no_match'] += 1
            continue

        lookup_key = _normalize_address(al1, city, state, pc)
        print(f"  Lookup key    : {fmt_addr(lookup_key)}")

        r = lookup_with_name_fallback(addr_map, consignee, al1, city, state, pc)
        n = len(r.pairs)
        cust_word = "customer" if n == 1 else "customers"
        method_summary = (
            f"cm_method={r.cm_method}  addr_score={r.addr_score}  "
            f"name_method={r.name_method}  name_score={r.name_score}  count={n}"
        )

        if r.cm_method in ('address_exact', 'address_fuzzy'):
            print(f"\n  >>> MATCHED {n} {cust_word} ({method_summary})")
            print_pairs(r.pairs)
        elif r.cm_method == 'address_disambiguated_by_name':
            print(f"\n  >>> MATCHED {n} {cust_word} ({method_summary})")
            print(f"      name picked the unique winner from a {n}-candidate address bucket")
            print_pairs(r.pairs)
        elif r.cm_method == 'name_fallback':
            print(f"\n  >>> MATCHED {n} {cust_word} ({method_summary})")
            if r.pairs:
                print(f"      XLSX name : {r.pairs[0][1].name!r}")
            print_pairs(r.pairs)
        elif r.cm_method == 'multi_match_unresolved':
            print(f"\n  >>> MULTI-MATCH UNRESOLVED ({n} candidates; {method_summary})")
            print_pairs(r.pairs)
        else:  # no_match
            print(f"\n  >>> NO MATCH ({method_summary})")
            if r.pairs:
                print(f"      Closest candidate(s):")
                print_pairs(r.pairs)
            else:
                print(f"      (no candidates at this city/state/postcode and no name near-miss)")

        summary[r.cm_method] += 1

    total = len(pdfs)
    matched = (
        summary['address_exact'] + summary['address_fuzzy']
        + summary['address_disambiguated_by_name'] + summary['name_fallback']
    )
    failed = summary['multi_match_unresolved'] + summary['no_match']
    print(f"\n{'=' * 78}")
    print(f"SUMMARY of {total} PDFs:  matched={matched}  failed={failed}  parse-error={summary['parse_fail']}")
    print(f"  address_exact                 : {summary['address_exact']}")
    print(f"  address_fuzzy                 : {summary['address_fuzzy']}")
    print(f"  address_disambiguated_by_name : {summary['address_disambiguated_by_name']}")
    print(f"  name_fallback                 : {summary['name_fallback']}")
    print(f"  multi_match_unresolved        : {summary['multi_match_unresolved']}")
    print(f"  no_match                      : {summary['no_match']}")
    print(f"  parse error                   : {summary['parse_fail']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
