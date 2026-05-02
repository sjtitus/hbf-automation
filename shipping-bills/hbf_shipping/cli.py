"""
Command-line entry point for the shipping-bills tool (stage 1).

Parses arguments, dispatches to the requested vendor's stage-1 ShipTo
extraction pipeline, and prints per-invoice + batch summaries.

Stage 1 stops after producing canonical ShipTo records (page-1 + BOL).
Customer matching, BillEntry construction, and QuickBooks CSV export
will be re-introduced in stage 2 against the new ShipTo shape.

Run from the project root — output directory (logs/) is CWD-relative.
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

    args = parser.parse_args()

    run_id, run_dir = setup_run(args.vendor)

    vendor = VENDORS[args.vendor]
    pipeline = Pipeline(vendor, run_id=run_id, run_dir=run_dir)
    pipeline.process_batch(args.invoice_dir)
    pipeline.report()


if __name__ == '__main__':
    main()
