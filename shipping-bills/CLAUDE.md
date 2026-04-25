# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains automation workflows for Highland Beef Farms (HBF) shipping invoice processing. Jessica uses this system to process shipping vendor invoices that arrive via email, validate them against customer records, and prepare bill entries for QuickBooks Online.

**Key URLs:**
- Outlook: https://outlook.office.com/mail

## Common Commands

Always run from the project root. Output directories (`logs/`,
`processing-logs/`, `quickbooks-imports/`) are CWD-relative.

### Process a directory of invoices (writes CSV)
```bash
python3 process_badger.py <invoice-dir>
# equivalent:
python3 -m hbf_shipping --vendor badger <invoice-dir>
```

### Preview only (skip CSV write)
```bash
python3 process_badger.py --dry-run <invoice-dir>
```

### Install Dependencies
```bash
pip3 install -r requirements.txt
```

`hbf_shipping/vendors/badger/ocr.py` shells out to the `tesseract` binary. Install it once per machine:
```bash
brew install tesseract
```

## Architecture

### Project Structure
```
process_badger.py                      # Thin shim — injects --vendor badger and calls cli.main
hbf_shipping/                          # The package
  cli.py                               # Argument parsing + vendor dispatch
  pipeline.py                          # Vendor-agnostic per-invoice processing loop
  bill_entry.py                        # Shared BillEntry dataclass (data only)
  customer_lookup.py                   # Customer validation against master list
  csv_export.py                        # Writes the QB-shaped batch-bills CSV
  processing_log.py                    # Per-invoice processing-log CSV writer
  run_logging.py                       # Per-run logging setup + per-invoice log handler + manifest writer
  vendors/
    __init__.py                        # VENDORS = {'badger': ...}
    badger/
      __init__.py                      # Re-exports parse_invoice, build_bill_entry, REQUIRED_FIELDS, SHIPPING_COMPANY
      parser.py                        # Page-1 text extraction
      ocr.py                           # Page-2 BOL SHIP TO OCR (Badger-specific)
      rules.py                         # Badger business rules (bill-date, due-date, shipper→category)
data/
  hbf-customers.xls                    # Customer master list (editable)
badger-invoices/                       # Drop-zone for Badger invoice PDFs
quickbooks-imports/                    # Generated batch-bills CSVs (one per run)
logs/<run-id>/                         # Per-run dir: run.log + <invoice>.log + summary.csv + manifest.json
```

### Vendor contract

Each `hbf_shipping/vendors/<name>/__init__.py` re-exports:

| Name | Type | Purpose |
|------|------|---------|
| `parse_invoice(pdf_path)` | `(dict, dict)` | Returns `(invoice_data, reasons)`. `reasons` keys mirror `invoice_data`; non-`None` reason for any field that failed to extract. |
| `build_bill_entry(invoice_data, customer_name)` | `BillEntry` | Applies the vendor's business rules to produce a shared `BillEntry`. |
| `REQUIRED_FIELDS` | `tuple[str, ...]` | Field names that must be non-`None` in `invoice_data` for the pipeline to proceed past validation. |
| `SHIPPING_COMPANY` | `str` | Display name for processing-log entries. |

Then add the vendor to `hbf_shipping/vendors/__init__.py::VENDORS` and (optionally) create a top-level `process_<vendor>.py` shim. `pipeline.py` consumes only this contract — it never imports vendor internals.

### Workflow Components

**Email Monitoring** (Future): Will monitor Outlook for vendor emails with specific subject patterns

**PDF Processing**: Extracts structured data from vendor-specific invoice formats

**Customer Validation**: Matches invoice consignees against `data/hbf-customers.xls` to identify valid customers vs. distributor cases

**Bill Entry Construction**: Each vendor's `rules.py` applies its own business logic and returns a shared `BillEntry`; `csv_export.py` writes those entries as a QuickBooks batch-bills CSV for manual import.

## Badger State Western Invoice Processing Workflow

### Email Pattern Recognition
- **Subject Format**: `"Badger State Western Invoice #<invoice-number> | SO-<sales-order> |"`
- **Attachment**: PDF invoice file
- **Action**: Save PDF to `./badger-invoices/` folder

### PDF Data Extraction

Page 1 (text-extractable) supplies most fields:
- **Invoice Number**: Top right corner
- **Invoice Date**: Top right, labeled "DATE"
- **Ship Date**: Right side, labeled "SHIP DATE"
- **Shipper**: Left box with vertical "SHIPPER" label
- **Bill To**: Should be "Highland Beef Farms"
- **Sales Order (SO) Number**: Format "SO-#####" (or "S0-#####" with OCR-artifact digit-zero)
- **Total Amount**: Bottom right, "PLEASE PAY THIS AMOUNT"
- **Past Due Date**: Bottom left, "THIS BILL IS PAST DUE ON"

Page 2 (image-only BOL) supplies the customer, via OCR in `hbf_shipping/vendors/badger/ocr.py`:
- **Consignee/Customer**: First text line inside the SHIP TO block on the BOL. The page-1 "CONSIGNEE" field is often abbreviated (e.g. "FCI" → should be "Tucson FCI"), which breaks customer lookup; page 2 has the full name.

### Customer Validation Logic
1. Extract the Consignee (customer name) from the invoice
2. Look up customer in `data/hbf-customers.xls` (Column A or column labeled "Customer Name")
3. **If customer FOUND**: Proceed to bill-entry construction
4. **If customer NOT FOUND**: This is a "distributor invoice" case
   - Log the invoice as "distributor invoice - skipped"
   - Do NOT process further (this workflow variation will be implemented later)

### QuickBooks Bill Entry Field Mapping

The following fields must be populated in the QuickBooks bill form for vendor "Badger State Western":

| QB Field | Value Source | Business Logic |
|----------|-------------|----------------|
| **Bill Date** | Invoice Date OR Ship Date | If invoice date and ship date are in same month: use invoice date. Otherwise: use ship date |
| **Due Date** | Past Due Date - 1 day | The due date is 1 day before the "THIS BILL IS PAST DUE ON" date |
| **Bill No.** | Invoice Number | Direct copy from PDF |
| **Category** (Row 1) | Based on Shipper | See shipper category mapping below |
| **Description** | SO Number | Full format: "SO-#####" |
| **Amount** | Total from invoice | "PLEASE PAY THIS AMOUNT" value |
| **Customer** | Consignee | The Ship To address (validated customer) |
| **Memo** | SO Number | Full format: "SO-#####" (same as Description) |

### Shipper-to-Category Mapping

The Category field depends on the **Shipper** listed in the invoice:

| Shipper Name | QuickBooks Category |
|--------------|---------------------|
| Midwest Refrigerated Services (MRS) | Product Delivery - Customer:Outbound Transport - MRS |
| Old Wisconsin Sausage Company | 5127 Product Delivery - Customer:Old Wisconsin |
| DairyFood USA | 5128 Product Delivery - Customer:Dairyfood |

### Example: Invoice 0064452 Processing

From the example PDF `Badger-example-Invoice0064452.pdf`:
- Invoice #: 0064452
- Invoice Date: 03/23/2026
- Ship Date: 03/12/2026
- Shipper: Old Wisconsin Sausage Company
- Consignee: Gold Star Foods
- SO: SO-11251
- Total: $1,298.03
- Past Due: 04/07/2026

**Resulting QB Entry:**
- Bill Date: 03/12/2026 (ship date, because invoice and ship dates are in different months)
- Due Date: 04/06/2026 (past due date minus 1 day)
- Bill No.: 0064452
- Category: 5127 Product Delivery - Customer:Old Wisconsin (shipper is Old Wisconsin)
- Description: SO-11251
- Amount: 1298.03
- Customer: Gold Star Foods
- Memo: SO-11251

## Development Notes

### Testing

Regression test suite under `tests/`. Pattern: end-to-end golden-file comparison — each test PDF in `tests/fixtures/<vendor>/` has a paired `<stem>.expected.json` snapshot of the full pipeline output (parsed `invoice_data`, `customer_matched`, and `bill_entry`). pytest parametrizes one test case per fixture pair.

```bash
pip install -r requirements-dev.txt
pytest                              # all
pytest -k badger                    # one vendor
python3 tools/refresh_goldens.py    # regenerate goldens (review diff before committing)
python3 tools/refresh_goldens.py badger 0064452   # one PDF only
```

PDFs and goldens are gitignored (real customer data). The harness scales to additional vendors with zero code changes — drop PDFs into `tests/fixtures/<vendor>/`, refresh goldens, run pytest.

### Execution modes

- **default** — parse + business rules, print a CSV-field preview, and write `quickbooks-imports/bills-<vendor>-YYYYMMDD-HHMMSS.csv`.
- **`--dry-run`** — parse + business rules + print the preview only. No bills CSV written.

### Run artifacts

Every run produces a self-contained directory at `logs/<run-id>/`, plus (in non-dry-run mode) a bills CSV at `quickbooks-imports/`.

**run-id format**: `<vendor>-YYYY-MM-DDTHH-MM-SSET-XXXXXX`
e.g. `badger-2026-04-25T09-30-45ET-a3f9c1`. Wall-clock US Eastern with literal `ET` suffix; 6-hex random tail makes same-second collisions effectively impossible. Sortable lexicographically into chronological order.

**Inside `logs/<run-id>/`:**
- `run.log` — INFO+ run-wide log, mirrors stdout.
- `<invoice-stem>.log` — DEBUG+ log scoped to one invoice, written through a flushing FileHandler so partial output survives a crash. The first place to look when an invoice breaks. Captures parser extractor results, OCR anchor decisions, customer-lookup match tier, business-rule branches, and full exception tracebacks.
- `summary.csv` — one row per PDF. Columns: `Run ID, Shipping Company, Invoice File, Processing Start, Processing End, Status, Bill Number, SO Number, Consignee, Customer Matched, Total Amount, Log File, Fail Step, Fail Message, Fail Detail`. Status is `SUCCESS` or `FAIL`; the extracted fields (Bill Number, SO Number, Consignee, Customer Matched, Total Amount) are populated on a best-effort basis even on FAIL. `Log File` points to the per-invoice log so triage can jump from a CSV row to its debug detail. On `FAIL`, `Fail Step` identifies where the failure occurred (`parse_pdf`, `validate_fields`, `customer_lookup`, or `build_bill_entry`); `Fail Detail` carries the specific extractor reason for `validate_fields` failures (e.g. "no match for pattern 'S[O0]-<digits>'"); otherwise `N/A`.
- `manifest.json` — machine-readable index of artifacts (run id, vendor, totals, paths to per-invoice logs, bills CSV, summary CSV). Future cloud worker uses this as the job-result document.

**Outside the run dir:**
- `quickbooks-imports/bills-<run-id>.csv` — the QuickBooks batch-bills import (default mode only). Filename embeds the run-id so you can correlate a bills CSV back to its run dir.

Stdout shows INFO; the per-invoice log shows DEBUG. To add a new debug breadcrumb in vendor or shared modules, declare `logger = logging.getLogger(__name__)` at module top and call `logger.debug(...)`. Noisy third-party loggers (`PIL`, `pypdf`, `pytesseract`) are silenced by `setup_run` in `run_logging.py` so per-invoice logs stay focused.

### CSV import path (current production path)

The QB batch-bills importer accepts a CSV with these headers (order matters):
`Bill no., Vendor, Bill Date, Due Date, Category, Description, Amount, Customer / Project, Memo`.

`hbf_shipping/csv_export.py` emits only those fields. Other columns from the full QB template (Mailing Address, Terms, Type, Billable, Tax, Product/Service, SKU, Qty, Rate, Total, etc.) are omitted — QB fills them from the vendor record or leaves them blank.

Batch processing aggregates every invoice in the run into a single CSV (single import, many bills).

### Outstanding work
- Outlook email intake (Graph API or manual drop into a watched folder)
- Distributor-invoice handling (SO lookup in external system)
- Scotlyn vendor workflow (`hbf_shipping/vendors/scotlyn/`)
- MRS vendor workflow (`hbf_shipping/vendors/mrs/`)

### Technology Stack
- **Python 3.14+**
- **pypdf** — PDF text extraction
- **openpyxl** / **xlrd** — customer list reading
- **python-dateutil** — date parsing

## Important Business Rules

1. **Month Comparison for Bill Date**: The bill date logic compares calendar months, not day counts
2. **Vertical Text in PDFs**: Badger invoices have rotated text that must be handled correctly
3. **Distributor Detection**: Any consignee not in customer list = distributor case (skip for now)
4. **SO Number Format**: Always include "SO-" prefix in Description and Memo fields
5. **Due Date Calculation**: Always subtract exactly 1 day from the "past due" date shown on invoice
