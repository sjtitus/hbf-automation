"""
Command-line entry point for the shipping-bills tool.

Parses arguments, dispatches to the requested vendor's pipeline, prints a
CSV preview, and (unless --dry-run) writes the QuickBooks-shaped batch-bills
CSV to ./quickbooks-imports/.

Run from the project root — output directories (logs/, processing-logs/,
quickbooks-imports/) are CWD-relative.
"""

import argparse
from pathlib import Path

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
        description="HBF shipping-vendor invoice processor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m hbf_shipping --vendor badger ./badger-invoices/
  python3 -m hbf_shipping --vendor badger --dry-run ./badger-invoices/
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
            'All *.pdf files in the directory are processed and aggregated '
            'into a single batch-bills CSV.'
        ),
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help=(
            'Parse, apply business rules, and print the CSV preview only. '
            'Do NOT write the CSV file. Default behavior writes '
            'quickbooks-imports/bills-<vendor>-YYYYMMDD-HHMMSS.csv.'
        ),
    )

    args = parser.parse_args()

    run_id, run_dir = setup_run(args.vendor)

    vendor = VENDORS[args.vendor]
    pipeline = Pipeline(vendor, run_id=run_id, run_dir=run_dir, dry_run=args.dry_run)
    pipeline.process_batch(args.invoice_dir)
    pipeline.flush(args.vendor)


if __name__ == '__main__':
    main()
