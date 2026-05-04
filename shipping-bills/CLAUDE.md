# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository contains automation workflows for Highland Beef Farms (HBF) shipping invoice processing. Jessica uses this system to process shipping vendor invoices that arrive via email, validate them against customer records, and prepare bill entries for QuickBooks Online.

**Key URLs:**
- Outlook: https://outlook.office.com/mail

## Common Commands

Always run from the project root. Output directories (`logs/`,
`quickbooks-imports/`, `master-validation-logs/`) are CWD-relative.
Always run inside the project venv (`venv/bin/python` or `source venv/bin/activate`).

### Process a directory of invoices (writes CSV)
```bash
venv/bin/python process_badger.py <invoice-dir>
# equivalent:
venv/bin/python -m hbf_shipping --vendor badger <invoice-dir>
```

### Preview only (skip bills CSV write)
```bash
venv/bin/python process_badger.py --dry-run <invoice-dir>
```

### Validate the customer master without running invoices
```bash
venv/bin/python tools/validate_master.py [--strict]
```

### Install Dependencies
```bash
pip3 install -r requirements.txt
```

The production pipeline runs **both page-1 text extraction and page-2 BOL OCR** for every invoice. Tesseract is required (`brew install tesseract` on macOS). BOL OCR is the most fragile component; the regression suite exercises it on every fixture so future changes surface immediately.

## Architecture

### Project Structure
```
process_badger.py                      # Thin shim — injects --vendor badger and calls cli.main
process_scotlynn.py                    # Thin shim — injects --vendor scotlynn and calls cli.main
hbf_shipping/                          # The package
  cli.py                               # Argument parsing + vendor dispatch
  pipeline.py                          # Vendor-agnostic per-invoice processing loop + finalize()
  bill_entry.py                        # Shared BillEntry dataclass (data only)
  ship_to.py                           # Canonical ShipTo / NormalizedAddress types + USPS Pub-28 normalization helpers (shared by both extractors)
  bol_ship_to.py                       # Page-2 BOL OCR extractor — produces a canonical ShipTo. Profile-driven: BADGER_PROFILE, SCOTLYNN_PROFILE
  customer_address_map.py              # Customer-master loader, validator, and stage-2 matcher
  csv_export.py                        # Writes the QB-shaped batch-bills CSV
  processing_log.py                    # Per-invoice summary CSV writer (23 cols)
  run_logging.py                       # Per-run logging setup + per-invoice log handler + manifest writer
  vendors/
    __init__.py                        # VENDORS = {'badger': ..., 'scotlynn': ...}
    badger/
      __init__.py                      # Re-exports parse_invoice, extract_invoice_ship_to, build_bill_entry, REQUIRED_FIELDS, SHIPPING_COMPANY
      parser.py                        # Page-1 text extraction (all fields, including structured ShipTo)
      ocr.py                           # Page-2 BOL OCR — predates bol_ship_to.py, kept for ad-hoc debugging only
      rules.py                         # Badger business rules (bill-date, due-date, shipper→category)
    scotlynn/
      __init__.py                      # Re-exports parse_invoice, extract_invoice_ship_to, build_bill_entry, REQUIRED_FIELDS, SHIPPING_COMPANY, BOL_PROFILE
      parser.py                        # Page-1 text extraction (label-anchored regex on plain text + layout mode for shipper/consignee block)
      rules.py                         # Scotlynn business rules (bill-date, due-date, shipper→category)
data/
  hbf-customer-shipping-addresses.xlsx # Customer master (production source; gitignored)
badger-invoices/                       # Drop-zone for Badger invoice PDFs (gitignored)
quickbooks-imports/                    # Generated batch-bills CSVs, one per run (gitignored)
logs/<run-id>/                         # Per-run dir: run.log + <invoice>.log + summary.csv + manifest.json + customer_master_validation.log
master-validation-logs/                # Standalone validate_master.py outputs (gitignored)
tools/
  refresh_goldens.py                   # Regenerate tests/fixtures/<vendor>/<stem>.expected.json against the production matcher
  validate_master.py                   # Standalone customer-master validator (no pipeline run needed)
  dump_customer_addresses.py           # Eyeball dump of the customer master (post-load)
  extract_ship_to_lines.py             # CLI wrapper over bol_ship_to.extract_ship_to (run production extractor over PDFs, write diagnostic PNGs)
  find_ship_to_bounds.py               # Visualize anchor detection (where 'Bill of Lading Number' / 'Highland Beef Farms' anchors land per page)
  crop_ship_to.py                      # Alternative full-page-OCR + label-anchor crop approach for cross-reference
  read_ship_to.py                      # Paired with crop_ship_to.py: OCR cropped PNGs and parse address fields
```

### Vendor contract

Each `hbf_shipping/vendors/<name>/__init__.py` re-exports:

| Name | Type | Purpose |
|------|------|---------|
| `parse_invoice(pdf_path)` | `(dict, dict)` | Returns `(invoice_data, reasons)`. `reasons` keys mirror `invoice_data`; non-`None` reason for any field that failed to extract. |
| `build_bill_entry(invoice_data, customer_name)` | `BillEntry` | Applies the vendor's business rules to produce a shared `BillEntry`. |
| `REQUIRED_FIELDS` | `tuple[str, ...]` | Field names that must be non-`None` in `invoice_data` for the pipeline to proceed past validation. |
| `SHIPPING_COMPANY` | `str` | Display name for processing-log entries. |
| `BOL_PROFILE` | `BolProfile` | Vendor's page-2 BOL extraction profile (anchors, header-fallback, divider, boundary phrases). Both Badger and Scotlynn declare their own profile constant, even though they're content-identical today (both ship under the same standard short-form BOL). Pipeline reads `vendor.BOL_PROFILE` directly — no implicit default. |

Then add the vendor to `hbf_shipping/vendors/__init__.py::VENDORS` and (optionally) create a top-level `process_<vendor>.py` shim. `pipeline.py` consumes only this contract — it never imports vendor internals.

### Workflow Components

**Email Monitoring** (Future): Will monitor Outlook for vendor emails with specific subject patterns

**PDF Processing**: Extracts structured data from vendor-specific invoice formats

**Customer Matching**: Stage-2 matcher consumes a page-1 `InvoiceExtraction` and a BOL `BolExtraction` and resolves to a single `MasterEntry` from `data/hbf-customer-shipping-addresses.xlsx`. Address match uses the exact 4-tuple (street + city + state + postcode); when the 4-tuple resolves to multiple master rows, a name-disambiguation matrix scores `name_candidates` against each row's `shipto_name`. BOL is preferred on cross-source disagreement. See "Customer Matching Logic" below.

**Bill Entry Construction**: Each vendor's `rules.py` applies its own business logic and returns a shared `BillEntry`; `csv_export.py` writes those entries as a QuickBooks batch-bills CSV for manual import.

## Badger State Western Invoice Processing Workflow

### Email Pattern Recognition
- **Subject Format**: `"Badger State Western Invoice #<invoice-number> | SO-<sales-order> |"`
- **Attachment**: PDF invoice file
- **Action**: Save PDF to `./badger-invoices/` folder

### PDF Data Extraction

All fields come from page 1. The parser uses pypdf's plain `extract_text()` for most fields and `extraction_mode='layout'` for the two-column CONSIGNEE block (where layout preserves the column gap so the right column can be split out).

- **Invoice Number**: Top right corner
- **Invoice Date**: Top right, labeled "DATE"
- **Ship Date**: Right side, labeled "SHIP DATE"
- **Shipper**: Left box with vertical "SHIPPER" label
- **Bill To**: Should be "Highland Beef Farms"
- **Consignee/Customer**: Right-hand column under the "CONSIGNEE" header in the layout-mode text. `_extract_page1_consignee` anchors on the line ending in `CONSIGNEE`, takes the next 3 non-empty lines, and splits each on 3+ spaces — the rightmost fragment of the first such line is the consignee name.
- **Sales Order (SO) Number**: Format "SO-#####". The parser also accepts "S0-#####" (digit-zero) since the page-1 text stream sometimes emits the prefix that way; the value is normalized back to letter-O before use.
- **Total Amount**: Bottom right, "PLEASE PAY THIS AMOUNT"
- **Past Due Date**: Bottom left, "THIS BILL IS PAST DUE ON"

Page-2 BOL OCR is part of the production path. `hbf_shipping/bol_ship_to.py` is the production extractor. `hbf_shipping/vendors/badger/ocr.py` is the older predecessor — kept around for ad-hoc debugging but not on the production code path.

### Customer Matching Logic

The pipeline uses **stage-2 address-first matching** with a name-disambiguation matrix. Source: `data/hbf-customer-shipping-addresses.xlsx` (columns `Name | AddressLine1 | AddressLine2 | City | State | Postcode`). The loader (`hbf_shipping/customer_address_map.py::load_master`) builds a `CustomerMaster` with two indexes:
- `by_address_4tuple`: `(street, city, state, postcode)` → list of `MasterEntry` (multi-tenant addresses collapse here)
- `by_customer_name`: normalized Customer Name → entries (used by `lookup_customer_name`)

Each invoice produces two source records: page-1 `InvoiceExtraction` (`vendor.extract_invoice_ship_to`) and page-2 `BolExtraction` (`bol_ship_to.extract_ship_to`). Both feed `match_invoice_customer(inv, bol, master)` → `InvoiceMatchResult`.

**Per-source flow** (`run_match_for_source`):
1. No address → `NO_INPUT`.
2. Exact 4-tuple lookup. 0 rows → `NO_MATCH`. 1 row → `UNIQUE` (locked on, do not check name).
3. Multi-row 4-tuple → name-disambig matrix: for each pair `(cand in ship_to.name_candidates, row in matched rows)`, score `fuzz.WRatio(_normalize_name(cand), _normalize_name(row.shipto_name))`. Best ≥ `NAME_DISAMBIG_THRESHOLD` (75) → `DISAMBIGUATED`. Else → `AMBIGUOUS`.

**Cross-source policy** (`match_invoice_customer`):
1. Both `NO_INPUT` → `HARD_FAIL`.
2. Both resolved + agree → `AGREE`.
3. Both resolved + disagree → `BOL_WINS_DISAGREEMENT`. Severity `severe` if both were `UNIQUE` (strongest signal of data inconsistency); `info` otherwise.
4. Only one resolved → `BOL_ONLY` or `INV_ONLY`.
5. Neither resolved → `HARD_FAIL`.

**Deny-list**: `customer_name == 'Highland Beef Farms Inventory'` (rows 182, 183 — internal one-offs) → `DENIED` regardless of how the match resolved. The rejected `MasterEntry` is preserved on the result for diagnostic visibility.

**Outcomes** (the `Match Method` value in `summary.csv`):

| Method | What happened |
|---|---|
| `agree` | BOL and page-1 both resolved to the same master row |
| `bol_wins_disagreement` | Both sources resolved but to different rows; BOL wins per policy |
| `bol_only` | Only BOL produced a usable address; matched on it |
| `inv_only` | Only page-1 produced a usable address; matched on it |
| `hard_fail` | Neither source resolved |
| `denied` | Match resolved to the HBF Inventory pseudo-customer; rejected |

Per-source method (recorded in `BOL Method` / `Page-1 Method`): `no_input`, `no_match`, `unique`, `disambiguated`, `ambiguous`. `Score` columns carry the WRatio when the disambig matrix ran, else empty.

`SUCCESS` requires `match.matched_entry is not None` AND `method` not in `{hard_fail, denied}`. The `success` property on `InvoiceMatchResult` is the affirmative test used by callers.

### Inspection tools

- `venv/bin/python tools/validate_master.py [--strict]` — run the customer-master validator without invoking the invoice pipeline. Writes `master-validation-logs/<timestamp>/customer_master_validation.log`.
- `venv/bin/python tools/dump_customer_addresses.py` — load the master and dump it grouped by 4-tuple address; multi-customer addresses and multi-row customers are called out separately. Useful for spot-checking before a vendor add.
- `venv/bin/python tools/extract_ship_to_lines.py <pdf>...` — run the production BOL extractor over a set of PDFs and write diagnostic PNGs. First reach when BOL extraction is misbehaving on a new fixture.
- `venv/bin/python tools/find_ship_to_bounds.py <pdf>...` — visualize the anchor detection (Bill-of-Lading-Number + Highland Beef Farms anchors). Reach when the BOL crop is wrong and you need to see why.
- `venv/bin/python tools/crop_ship_to.py <pdf>...` + `tools/read_ship_to.py` — alternative crop+OCR pipeline (different approach from production), useful for cross-reference when production gets a wrong answer.

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

Three test suites under `tests/`:

- **`test_vendor_regression.py`** — end-to-end golden-file comparison. Each PDF in `tests/fixtures/<vendor>/` has a paired `<stem>.expected.json` snapshot capturing `invoice_data`, a `customer_match` block (customer_name, customer_number, master_row, match_method, severity, bol_method, page1_method), and `bill_entry`. The test runs the production stack: `vendor.parse_invoice` → page-1 ShipTo → page-2 BOL OCR → `match_invoice_customer` → `build_bill_entry`. **BOL OCR is included on purpose** — same tesseract on the same PDF is deterministic, and the BOL extractor is the most fragile component, so regression is exactly where we want it exercised.
- **`test_pipeline_integration.py`** — covers the artifact-emitting layer (summary CSV, bills CSV, manifest) using a stub vendor + synthetic master. Fast, no PDFs.
- **`test_customer_master_validation.py`** + **`test_customer_match.py`** — unit tests for the loader + validator + matcher.

```bash
pip install -r requirements-dev.txt
venv/bin/python -m pytest                                # all (~2 min — OCR-bound)
venv/bin/python -m pytest -k 'not vendor_regression'     # skip OCR (sub-second)
venv/bin/python -m pytest tests/test_vendor_regression.py -v
venv/bin/python tools/refresh_goldens.py                 # regenerate ALL goldens (review diff)
venv/bin/python tools/refresh_goldens.py badger 0064452  # one PDF only
```

PDFs and goldens are gitignored (real customer data). The harness scales to additional vendors with zero code changes — drop PDFs into `tests/fixtures/<vendor>/`, refresh goldens, run pytest.

### Execution modes

- **default** — full pipeline (parse + validate + extract + match + build bill), writes `summary.csv`, `manifest.json`, per-invoice logs, customer-master validation log, AND `quickbooks-imports/bills-<run-id>.csv`.
- **`--dry-run`** — same pipeline, summary + manifest still written, bills CSV is **suppressed** and a vertical bills preview is logged instead. Useful for verifying outcomes before producing a QB import file.
- **`--strict-master`** — abort startup if the customer-master validator finds any hard violation. Validation log is written either way.

### Run artifacts

Every run produces a self-contained directory at `logs/<run-id>/`, plus (in non-dry-run mode) a bills CSV at `quickbooks-imports/bills-<run-id>.csv`.

**run-id format**: `<vendor>-YYYY-MM-DDTHH-MM-SSET-XXXXXX`
e.g. `badger-2026-04-25T09-30-45ET-a3f9c1`. Wall-clock US Eastern with literal `ET` suffix; 6-hex random tail makes same-second collisions effectively impossible. Sortable lexicographically into chronological order.

**Inside `logs/<run-id>/`:**
- `run.log` — INFO+ run-wide log, mirrors stdout.
- `<invoice-stem>.log` — DEBUG+ log scoped to one invoice, written through a flushing FileHandler so partial output survives a crash. First place to look when an invoice breaks. Captures parser extractor results (per-field value or failure reason), the cross-source match outcome (with full per-source breakdown for non-trivial cases), business-rule branches, and full exception tracebacks.
- `customer_master_validation.log` — the validation report run at startup against `data/hbf-customer-shipping-addresses.xlsx`. Hard rules: `required_fields_present`, `triple_unique`, `could_not_extract_customer_number`. Soft rules: `duplicate_customer_number`, `malformed_customer_number_format`. AL1 cells with malformed format but a recoverable 6-digit customer number land under the soft rule (number is usable; cell should be reformatted). See `tools/validate_master.py` for standalone runs.
- `summary.csv` — one row per PDF, **23 columns**:
  `Run ID, Shipping Company, Invoice File, Processing Start, Processing End, Status, Bill Number, SO Number, Total Amount, Match Method, Match Severity, Customer Name, Customer Number, Master Row, BOL Method, BOL Score, Page-1 Method, Page-1 Score, Fail Step, Fail Message, Fail Detail, Log File, Match Fail Reason`.
  Status is `SUCCESS` (when a `BillEntry` was built) or `FAIL`. `Match Method` is one of `agree`, `bol_wins_disagreement`, `bol_only`, `inv_only`, `hard_fail`, `denied` (see Customer Matching Logic). `Match Severity` is `ok`/`info`/`severe`. `Customer Name`, `Customer Number`, and `Master Row` come from the resolved `MasterEntry` and are blank on `hard_fail`/`denied`. `BOL Method` / `Page-1 Method` describe each source's outcome (`no_input`/`no_match`/`unique`/`disambiguated`/`ambiguous`); the `*Score` columns carry the name-disambig WRatio when the matrix ran, else empty. On `FAIL`, `Fail Step` identifies where the invoice fell off (`parse_pdf` / `validate_fields` / `extract_ship_to` / `match_customer` / `build_bill_entry`) and `Fail Detail` / `Fail Message` carry specifics. `Match Fail Reason` carries `match.fail_reason` (populated on `hard_fail` / `denied`).
- `manifest.json` — machine-readable index of artifacts (run id, vendor, started/ended timestamps, totals, absolute paths to run log, summary CSV, validation log, bills CSV, per-invoice logs).

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
- MRS vendor workflow (`hbf_shipping/vendors/mrs/`)

### Technology Stack
- **Python 3.14+**
- **pypdf** — PDF text extraction (page-1 invoice fields)
- **pytesseract** + **tesseract** — page-2 BOL OCR
- **opencv-python** + **Pillow** — BOL image preprocessing for OCR
- **PyMuPDF (fitz)** — high-DPI rendering of page 2
- **openpyxl** — read `hbf-customer-shipping-addresses.xlsx`
- **usaddress-scourgify** — USPS Pub 28 address normalization (suffix abbreviations, directional collapsing, suite splitting)
- **rapidfuzz** — `WRatio` for the name-disambiguation matrix
- **python-dateutil** — date parsing

## Important Business Rules

1. **Month Comparison for Bill Date**: The bill date logic compares calendar months, not day counts
2. **Vertical Text in PDFs**: Badger invoices have rotated text that must be handled correctly
3. **Distributor Detection**: Any consignee not in customer list = distributor case (skip for now)
4. **SO Number Format**: Always include "SO-" prefix in Description and Memo fields
5. **Due Date Calculation**: Always subtract exactly 1 day from the "past due" date shown on invoice
