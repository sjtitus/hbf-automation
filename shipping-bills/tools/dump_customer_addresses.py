#!/usr/bin/env python3
"""
Preview script: load the customer-address map XLSX and dump it to stdout
for manual inspection. No assertions — just print what loaded.

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
    load_address_to_customers,
)


def main() -> int:
    # Surface the loader's skip count.
    logging.basicConfig(level=logging.DEBUG, format='%(levelname)s %(name)s: %(message)s')

    print(f"Loading: {DEFAULT_ADDRESS_FILE}\n")
    m = load_address_to_customers()

    total_entries = sum(len(v) for v in m.values())
    print(f"\n{'=' * 78}")
    print(f"  {len(m)} unique addresses, {total_entries} customer-entries total")
    print(f"{'=' * 78}\n")

    keys = sorted(m.keys(), key=lambda a: (a.state, a.city, a.street))

    # Full dump
    for addr in keys:
        entries = m[addr]
        print(f"  {addr.street}")
        print(f"  {addr.city}, {addr.state} {addr.postcode}")
        for e in entries:
            print(f"    -> {e.name}   [line_1_clean={e.line_1_clean}]")
        print()

    # Multi-customer addresses
    multi_cust = [(a, m[a]) for a in keys if len(m[a]) > 1]
    print(f"\n{'=' * 78}")
    print(f"  Addresses shared by 2+ customers: {len(multi_cust)}")
    print(f"{'=' * 78}\n")
    for addr, entries in sorted(multi_cust, key=lambda x: -len(x[1])):
        print(f"  {addr.street}, {addr.city}, {addr.state} {addr.postcode}  ({len(entries)} customers)")
        for e in entries:
            print(f"    -> {e.name}")
        print()

    # Multi-address customers
    by_name = defaultdict(list)
    for addr, entries in m.items():
        for entry in entries:
            by_name[entry.name].append(addr)
    multi_addr = sorted(
        ((n, a) for n, a in by_name.items() if len(a) > 1),
        key=lambda x: (-len(x[1]), x[0]),
    )
    print(f"\n{'=' * 78}")
    print(f"  Customers with 2+ addresses: {len(multi_addr)}")
    print(f"{'=' * 78}\n")
    for name, addrs in multi_addr:
        print(f"  {name}  ({len(addrs)} addresses)")
        for a in sorted(addrs, key=lambda x: (x.state, x.city, x.street)):
            print(f"    -> {a.street}, {a.city}, {a.state} {a.postcode}")
        print()

    return 0


if __name__ == '__main__':
    sys.exit(main())
