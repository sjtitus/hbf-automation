#!/usr/bin/env python3
"""
Standalone validator for the HBF customer-master XLSX.

Useful when the spreadsheet has been edited and you want to check it
*before* running the full invoice pipeline. Produces the same
`customer_master_validation.log` report the pipeline writes, just
without needing an invoice directory.

Usage:
    venv/bin/python tools/validate_master.py [XLSX] [--out-dir DIR] [--strict]

Examples:
    # Validate the default master file:
    venv/bin/python tools/validate_master.py

    # Validate a specific file:
    venv/bin/python tools/validate_master.py path/to/master.xlsx

    # Strict mode: exit non-zero if any hard rule is violated:
    venv/bin/python tools/validate_master.py --strict

    # Custom output directory:
    venv/bin/python tools/validate_master.py --out-dir /tmp/validate
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

# Allow running from the project root without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hbf_shipping.customer_address_map import (  # noqa: E402
    DEFAULT_ADDRESS_FILE,
    MasterValidationError,
    load_master,
)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        'xlsx',
        nargs='?',
        type=Path,
        default=DEFAULT_ADDRESS_FILE,
        help=(
            'Path to the customer-master XLSX. Defaults to the standard '
            f'project location: {DEFAULT_ADDRESS_FILE}'
        ),
    )
    ap.add_argument(
        '--out-dir',
        type=Path,
        default=None,
        help=(
            'Directory to write the validation log. Defaults to '
            'master-validation-logs/<YYYY-MM-DDThh-mm-ss>/.'
        ),
    )
    ap.add_argument(
        '--strict',
        action='store_true',
        help=(
            'Exit with non-zero status if any hard-rule violation is '
            'found. The validation report is still written either way.'
        ),
    )
    args = ap.parse_args(argv)

    if not args.xlsx.exists():
        print(f"ERROR: master XLSX not found: {args.xlsx}", file=sys.stderr)
        return 2

    out_dir = args.out_dir
    if out_dir is None:
        ts = datetime.now().strftime('%Y-%m-%dT%H-%M-%S')
        out_dir = Path('master-validation-logs') / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    # Mirror minimal stdout output: one INFO line on load, plus warnings.
    logging.basicConfig(level=logging.WARNING, format='%(message)s')

    log_path = out_dir / 'customer_master_validation.log'

    try:
        master = load_master(
            xlsx_path=args.xlsx,
            strict=args.strict,
            log_dir=out_dir,
        )
    except MasterValidationError as e:
        print(f"\nFAIL (strict mode): {e}", file=sys.stderr)
        print(f"Validation log: {log_path}", file=sys.stderr)
        return 1

    print(
        f"Master loaded: {len(master.entries)} entries / "
        f"{len(master.by_address_4tuple)} unique addresses / "
        f"{len(master.by_customer_name)} unique customers"
    )
    print(f"Validation log: {log_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
