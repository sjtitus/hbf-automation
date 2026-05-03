"""
Per-run processing-log CSV — one row per invoice processed.

Separate from csv_export.py (which produces the QuickBooks bills import)
because the two CSVs have different schemas and different lifetimes.

Columns reflect the stage-2 customer matcher (`InvoiceMatchResult`):
the cross-source method/severity, the matched master row's identity,
and the per-source detail (BOL + page-1 methods + name disambig
scores). Operational columns (timing, fail-step, log pointer) round
out a single 23-column row per invoice.
"""

import csv
from pathlib import Path
from typing import Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from .pipeline import InvoiceOutcome

HEADERS = [
    # Identity
    'Run ID',
    'Shipping Company',
    'Invoice File',
    'Processing Start',
    'Processing End',
    'Status',                       # SUCCESS | FAIL
    # Extracted
    'Bill Number',
    'SO Number',
    'Total Amount',
    # Cross-source match
    'Match Method',                 # AGREE | BOL_WINS_DISAGREEMENT | BOL_ONLY | INV_ONLY | HARD_FAIL | DENIED
    'Match Severity',               # ok | info | severe
    'Customer Name',
    'Customer Number',
    'Master Row',
    # Per-source detail
    'BOL Method',                   # NO_INPUT | NO_MATCH | UNIQUE | DISAMBIGUATED | AMBIGUOUS
    'BOL Score',                    # WRatio if DISAMBIGUATED/AMBIGUOUS, else empty
    'Page-1 Method',
    'Page-1 Score',
    # Failure context
    'Fail Step',                    # parse_pdf | validate_fields | extract_ship_to | match_customer | build_bill_entry | N/A
    'Fail Message',
    'Fail Detail',
    # Pointers
    'Log File',                     # per-invoice .log path
    'Match Fail Reason',            # match.fail_reason — populated on HARD_FAIL/DENIED
]


def _fmt_score(score) -> str:
    """Render a 0-100 float score as a short integer-ish string, or
    empty when score is 0 (which the matcher uses as 'name not used')."""
    if score is None or score == 0:
        return ''
    return f"{score:.0f}"


def build_summary_row(
    outcome: 'InvoiceOutcome',
    run_id: str,
    shipping_company: str,
) -> dict:
    """Map one `InvoiceOutcome` to a single processing-log row dict
    keyed by `HEADERS`. Missing/non-applicable fields render as the
    empty string."""
    inv_data = outcome.invoice_data or {}
    match = outcome.match
    entry = match.matched_entry if match else None

    if match is not None:
        match_method = match.method
        match_severity = match.severity
        bol_method = match.bol.method
        bol_score = _fmt_score(match.bol.score)
        inv_method = match.inv.method
        inv_score = _fmt_score(match.inv.score)
        match_fail_reason = match.fail_reason or ''
    else:
        match_method = match_severity = ''
        bol_method = bol_score = inv_method = inv_score = ''
        match_fail_reason = ''

    customer_name = entry.customer_name if entry else ''
    customer_number = (entry.customer_number if entry else '') or ''
    master_row = str(entry.row) if entry else ''

    total = inv_data.get('total_amount')
    total_str = f"{total:.2f}" if isinstance(total, (int, float)) else (str(total) if total else '')

    return {
        'Run ID': run_id,
        'Shipping Company': shipping_company,
        'Invoice File': outcome.pdf_path.name,
        'Processing Start': outcome.processing_start or '',
        'Processing End': outcome.processing_end or '',
        'Status': 'SUCCESS' if outcome.bill_entry is not None else 'FAIL',
        'Bill Number': inv_data.get('invoice_number', '') or '',
        'SO Number': inv_data.get('so_number', '') or '',
        'Total Amount': total_str,
        'Match Method': match_method,
        'Match Severity': match_severity,
        'Customer Name': customer_name or '',
        'Customer Number': customer_number,
        'Master Row': master_row,
        'BOL Method': bol_method,
        'BOL Score': bol_score,
        'Page-1 Method': inv_method,
        'Page-1 Score': inv_score,
        'Fail Step': outcome.fail_step or 'N/A',
        'Fail Message': outcome.fail_message or '',
        'Fail Detail': outcome.fail_detail or '',
        'Log File': str(outcome.log_path) if outcome.log_path else '',
        'Match Fail Reason': match_fail_reason,
    }


def write_processing_log(rows: Iterable[dict], path: str | Path) -> Path:
    """Write processing-log rows to a CSV at `path`. Creates parent
    dirs. Returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path
