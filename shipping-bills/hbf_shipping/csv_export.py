"""
CSV export for QuickBooks batch bill import.

Writes only the fields we compute. Headers follow the QB batch-bills template
order; "Description" / "Amount" / "Customer / Project" appear twice in the
full template (once for Category lines, once for Product/Service lines) — we
use the Category-line columns only.
"""

import csv
from pathlib import Path
from typing import Iterable

HEADERS = [
    "Bill no.",
    "Vendor",
    "Bill Date",
    "Due Date",
    "Type",
    "Category",
    "Description",
    "Amount",
    "Customer / Project",
    "Memo",
]


def _row_from_entry(entry: dict) -> dict:
    return {
        "Bill no.": entry["bill_number"],
        "Vendor": entry["vendor"],
        "Bill Date": entry["bill_date"],
        "Due Date": entry["due_date"],
        "Type": "Category Details",
        "Category": entry["category"],
        "Description": entry["description"],
        "Amount": f"{entry['amount']:.2f}",
        "Customer / Project": entry["customer"],
        "Memo": entry["memo"],
    }


def write_bills_csv(entries: Iterable[dict], path: str | Path) -> Path:
    """Write bill entries to a CSV at `path`. Creates parent dirs. Returns path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
        for entry in entries:
            writer.writerow(_row_from_entry(entry))
    return path


def format_bills_preview(entries: list[dict]) -> str:
    """Return a vertical, aligned preview of each bill — one block per entry.

    Used by dry-run to show the user exactly what would go into the CSV,
    in a form that's easy to eyeball field-by-field.
    """
    if not entries:
        return "(no bills)"
    label_w = max(len(h) for h in HEADERS)
    blocks = []
    for i, entry in enumerate(entries, 1):
        row = _row_from_entry(entry)
        header = f"───── Bill {i} of {len(entries)} ─────"
        lines = [header]
        for h in HEADERS:
            lines.append(f"  {h.ljust(label_w)}  {row[h]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
