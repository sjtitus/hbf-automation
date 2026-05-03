#!/usr/bin/env python3
"""
Preview script: load the customer master and dump it to stdout for
manual inspection. No assertions — just print what loaded.

Useful when adding a new vendor: spot-check that a customer is in the
master before debugging why the matcher missed.

Run from project root:
    python3 tools/dump_customer_addresses.py
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hbf_shipping.customer_address_map import (  # noqa: E402
    DEFAULT_ADDRESS_FILE,
    load_master,
)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

    print(f"Loading: {DEFAULT_ADDRESS_FILE}\n")
    master = load_master()

    print(f"\n{'=' * 78}")
    print(f"  {len(master.entries)} entries / "
          f"{len(master.by_address_4tuple)} unique 4-tuple addresses / "
          f"{len(master.by_customer_name)} unique customer names")
    print(f"{'=' * 78}\n")

    addrs = sorted(
        master.by_address_4tuple.keys(),
        key=lambda k: (k[1], k[0], k[2], k[3]),  # state, street, city, postcode — stable for visual scan
    )

    # Full dump — one block per unique 4-tuple address
    for key in addrs:
        entries = master.by_address_4tuple[key]
        street, city, state, postcode = key
        print(f"  {street}")
        print(f"  {city}, {state} {postcode}")
        for e in entries:
            extra = (
                f"   [cust#={e.customer_number}, row={e.row}]"
                if e.customer_number else f"   [row={e.row}]"
            )
            print(f"    -> {e.customer_name}   [shipto_name={e.shipto_name}]{extra}")
        print()

    # Multi-customer addresses — multi-tenant sites that need name disambig
    multi_cust = [(k, v) for k, v in master.by_address_4tuple.items() if len(v) > 1]
    print(f"\n{'=' * 78}")
    print(f"  4-tuple addresses shared by 2+ customers: {len(multi_cust)}")
    print(f"{'=' * 78}\n")
    for key, entries in sorted(multi_cust, key=lambda x: -len(x[1])):
        street, city, state, postcode = key
        print(f"  {street}, {city}, {state} {postcode}  ({len(entries)} customers)")
        for e in entries:
            print(f"    -> {e.customer_name}   [shipto_name={e.shipto_name}]")
        print()

    # Multi-address customers — same Customer Name appearing on multiple rows
    by_name: dict = defaultdict(list)
    for e in master.entries:
        by_name[e.customer_name].append(e)
    multi_addr = sorted(
        ((n, es) for n, es in by_name.items() if len(es) > 1),
        key=lambda x: (-len(x[1]), x[0]),
    )
    print(f"\n{'=' * 78}")
    print(f"  Customer Names appearing on 2+ rows: {len(multi_addr)}")
    print(f"{'=' * 78}\n")
    for name, entries in multi_addr:
        print(f"  {name}  ({len(entries)} rows)")
        for e in sorted(entries, key=lambda x: (x.address.state, x.address.city, x.address.street)):
            a = e.address
            print(f"    -> {a.street}, {a.city}, {a.state} {a.postcode}   [row={e.row}]")
        print()

    return 0


if __name__ == '__main__':
    sys.exit(main())
