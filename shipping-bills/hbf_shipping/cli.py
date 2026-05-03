"""
Command-line entry point for the shipping-bills tool.

Parses arguments, dispatches to the requested vendor's pipeline, and
emits the run artifacts: per-invoice logs, the customer-master
validation log, a `summary.csv` with one row per invoice processed, a
`manifest.json` indexing the run, and (unless `--dry-run` is set) the
QuickBooks bills-import CSV.

Run from the project root — output directories (`logs/`,
`quickbooks-imports/`) are CWD-relative.
"""

import argparse
import sys
from pathlib import Path

from .customer_address_map import MasterValidationError
from .pipeline import Pipeline
from .run_logging import setup_run
from .vendors import VENDORS


def _existing_dir(raw: str) -> Path:
    path = Path(raw)
    if not path.exists():
        raise argparse.ArgumentTypeError(f"path does not exist: {raw}")
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"not a directory: {raw}")
    return path


def main():
    parser = argparse.ArgumentParser(
        description=(
            "HBF shipping-vendor invoice ShipTo extractor (stage 1). "
            "Runs page-1 + BOL ShipTo extraction on every PDF in a directory "
            "and prints a comparison summary."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m hbf_shipping --vendor badger ./badger-invoices/
""",
    )
    parser.add_argument(
        '--vendor',
        required=True,
        choices=sorted(VENDORS.keys()),
        help='Shipping vendor whose invoice format to process.',
    )
    parser.add_argument(
        'invoice_dir',
        metavar='invoice-dir',
        type=_existing_dir,
        help=(
            'Path to a directory containing one or more invoice PDFs. '
            'All *.pdf files in the directory have their SHIP TO record '
            'extracted from page 1 (text) and page 2 (BOL OCR).'
        ),
    )
    parser.add_argument(
        '--strict-master',
        action='store_true',
        help=(
            'Treat hard-rule violations in the customer-master XLSX as '
            'fatal: abort startup with a non-zero exit if any are found. '
            'The validation report is always written to '
            '<run-dir>/customer_master_validation.log regardless of this '
            'flag; default behavior is to log violations and continue.'
        ),
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help=(
            'Run the full pipeline (parse, match, build bill entries) '
            'and write summary.csv + manifest.json, but DO NOT write '
            'the QuickBooks bills CSV. Bills are previewed in the run '
            'log instead.'
        ),
    )

    args = parser.parse_args()

    run_id, run_dir = setup_run(args.vendor)

    vendor = VENDORS[args.vendor]
    try:
        pipeline = Pipeline(
            vendor, run_id=run_id, run_dir=run_dir,
            vendor_slug=args.vendor,
            strict_master=args.strict_master,
        )
    except MasterValidationError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)

    pipeline.process_batch(args.invoice_dir)
    pipeline.report()
    artifacts = pipeline.finalize(dry_run=args.dry_run)

    print()
    print("Run artifacts:")
    for label, path in artifacts.items():
        print(f"  {label}: {path}")


if __name__ == '__main__':
    main()
