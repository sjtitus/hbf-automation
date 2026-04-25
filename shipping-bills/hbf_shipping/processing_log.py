"""
Per-run processing-log CSV — one row per invoice processed.

Separate from csv_export.py (which produces the QuickBooks bills import)
because the two CSVs have different schemas and different lifetimes.
"""

import csv
from pathlib import Path
from typing import Iterable

HEADERS = [
    "Run ID",
    "Shipping Company",
    "Invoice File",
    "Processing Start",
    "Processing End",
    "Status",
    "Bill Number",
    "SO Number",
    "Consignee",
    "Customer Matched",
    "Total Amount",
    "Log File",
    "Fail Step",
    "Fail Message",
    "Fail Detail",
]


def write_processing_log(rows: Iterable[dict], path: str | Path) -> Path:
    """Write processing-log rows to a CSV at `path`. Creates parent dirs. Returns path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path
