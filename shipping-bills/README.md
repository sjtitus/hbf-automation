# Highland Beef Farms - Shipping Invoice Automation

Automated processing of shipping vendor invoices for QuickBooks entry.

## Quick Start

### First Time Setup

1. **Activate the virtual environment:**
   ```bash
   source venv/bin/activate
   ```

2. **Verify installation:**
   ```bash
   python3 process_badger.py --help
   ```

### Processing Invoices

**Drop one or more PDFs into a directory, then run:**
```bash
venv/bin/python process_badger.py ./badger-invoices/
```
This processes every `*.pdf` in the directory and writes an aggregated
QuickBooks batch-bills CSV to `quickbooks-imports/bills-<run-id>.csv`
(run-id format `<vendor>-YYYY-MM-DDTHH-MM-SSET-XXXXXX`).

**Preview without writing the bills CSV:**
```bash
venv/bin/python process_badger.py --dry-run ./badger-invoices/
```

**Strict customer-master validation** (abort on any hard violation):
```bash
venv/bin/python process_badger.py --strict-master ./badger-invoices/
```

The vendor-agnostic form (equivalent):
```bash
venv/bin/python -m hbf_shipping --vendor badger ./badger-invoices/
```

**Always run from the project root and inside the venv.** Output
directories (`logs/`, `quickbooks-imports/`, `master-validation-logs/`)
are resolved relative to the current working directory.

## Local-only data (not in this repo)

Two things are needed at runtime but are intentionally **not** committed:

1. **`data/hbf-customer-shipping-addresses.xlsx`** — the customer master (columns `Name | AddressLine1 | AddressLine2 | City | State | Postcode`). Used by the production address-aware matcher. Business-confidential; obtain from the company's secure share.
2. **`badger-invoices/*.pdf`** (and other vendors' drop-zones) — the invoice PDFs you want to process. The directory is committed (with a `.gitkeep`); the PDFs are gitignored.

Per-run output (`logs/<run-id>/`, `quickbooks-imports/bills-<run-id>.csv`, `master-validation-logs/`) is also gitignored — these contain customer data and regenerate on every run.

## What it does

✓ **It DOES:**
- Parse PDF invoices and extract structured page-1 fields (consignee block, totals, dates, etc.)
- OCR page 2 (the BOL) and extract a canonical `ShipTo` from the SHIP TO box
- Match the invoice to a customer master row using a stage-2 matcher: exact 4-tuple address match (USPS Pub 28 normalization via `usaddress-scourgify`) on each source independently; name-disambiguation matrix (`rapidfuzz.WRatio`) when an address resolves to multiple master rows; cross-source policy that prefers BOL on disagreement
- Run a customer-master validation phase at startup (rules: required fields, triple uniqueness, could-not-extract customer number, duplicate / malformed customer number)
- Apply vendor business rules to produce a `BillEntry`
- Write a QuickBooks batch-bills CSV (or preview it with `--dry-run`)
- Produce a self-contained per-run artifact directory under `./logs/<run-id>/` containing a run log, per-invoice debug logs, a 23-column `summary.csv` capturing the full match outcome, the customer-master validation log, and a `manifest.json` indexing everything

✗ **It DOES NOT:**
- Connect to Outlook email
- Talk to QuickBooks directly — the CSV is imported manually
- Make any external connections

## What Gets Logged

Each run creates `logs/<vendor>-YYYY-MM-DDTHH-MM-SSET-XXXXXX/` containing:

- **`run.log`** — INFO+ run-wide log (mirrors stdout)
- **`<invoice-stem>.log`** — DEBUG+ per-invoice debug log (one per PDF; flushed aggressively for post-mortem after a crash)
- **`summary.csv`** — one row per invoice with status, extracted fields, and the full match outcome (`Match Method`, `Match Severity`, `Customer Name/Number`, `Master Row`, per-source `BOL Method`/`Page-1 Method` and disambig scores)
- **`customer_master_validation.log`** — validation report for the customer master used by this run
- **`manifest.json`** — machine-readable index of artifacts

## Supported Vendors

- ✓ Badger State Western
- ✓ Scotlynn USA Division
- ☐ MRS (planned)

## Running tests

The project has a regression test suite that runs the pipeline end-to-end against real invoice PDFs and compares the output against committed *golden* JSON files.

```bash
pip install -r requirements-dev.txt    # one-time, pulls pytest
pytest                                  # runs all regressions
pytest -k badger                        # one vendor only
```

Test PDFs live under `tests/fixtures/<vendor>/` and are gitignored (they contain real customer data). Goldens live alongside as `<stem>.expected.json` and are also gitignored. Both stay on each developer's local disk.

**Adding a new test case:**
```bash
cp /path/to/invoice.pdf tests/fixtures/badger/
python3 tools/refresh_goldens.py badger      # captures the golden
pytest -k badger                              # confirm it passes
```

**After an intentional pipeline change:**
```bash
python3 tools/refresh_goldens.py             # regenerate ALL goldens
git diff tests/fixtures/                     # review the diff
# Goldens are gitignored, but the diff command shows working-tree changes
# so you can sanity-check that the change is what you expected.
```

## Next Steps

1. Email monitoring (Outlook Graph API or folder watch)
2. Distributor-invoice handling (SO lookup in external system)
3. Additional vendor workflows

## File Structure

```
├── process_badger.py             # Thin shim → hbf_shipping.cli with --vendor badger
├── process_scotlynn.py           # Thin shim → hbf_shipping.cli with --vendor scotlynn
├── hbf_shipping/                 # The package
│   ├── cli.py                    # Argument parsing + vendor dispatch
│   ├── pipeline.py               # Vendor-agnostic processing pipeline + finalize() (artifacts)
│   ├── bill_entry.py             # Shared BillEntry dataclass
│   ├── ship_to.py                # Canonical ShipTo / NormalizedAddress + USPS Pub-28 normalization
│   ├── bol_ship_to.py            # Page-2 BOL OCR extractor (production)
│   ├── customer_address_map.py   # Customer-master loader, validator, stage-2 matcher
│   ├── csv_export.py             # CSV writer + dry-run preview
│   ├── processing_log.py         # Per-invoice summary CSV writer (23 cols, new shape)
│   ├── run_logging.py            # Per-run log dir + per-invoice log handler + manifest
│   └── vendors/
│       ├── __init__.py           # VENDORS registry
│       └── badger/
│           ├── parser.py         # PDF text extraction (page 1, all fields incl. structured address)
│           ├── ocr.py            # Page-2 BOL OCR — older predecessor, kept for debugging only
│           └── rules.py          # Badger business rules + build_bill_entry
├── data/
│   └── hbf-customer-shipping-addresses.xlsx   # Customer master (gitignored)
├── badger-invoices/              # Drop-zone for invoice PDFs to process
├── quickbooks-imports/           # Generated batch-bills CSVs, one per run (gitignored)
├── logs/                         # Per-run dirs (gitignored)
├── master-validation-logs/       # Standalone validate_master.py outputs (gitignored)
├── tests/
│   ├── test_vendor_regression.py    # Parametrized end-to-end regression (uses production stack incl. BOL OCR)
│   ├── test_pipeline_integration.py # Stub-vendor smoke-test of the artifact-emitting layer
│   ├── test_customer_match.py       # Unit tests for the stage-2 matcher
│   ├── test_customer_master_validation.py  # Unit tests for the loader + validator
│   └── fixtures/<vendor>/        # Test PDFs + .expected.json goldens (gitignored)
├── tools/
│   ├── refresh_goldens.py        # (Re)generate goldens against the production matcher
│   ├── validate_master.py        # Standalone customer-master validator
│   ├── dump_customer_addresses.py# Eyeball dump of the customer master
│   ├── extract_ship_to_lines.py  # CLI wrapper over the production BOL extractor (writes diagnostic PNGs)
│   ├── find_ship_to_bounds.py    # Visualize BOL anchor detection
│   ├── crop_ship_to.py           # Alternative crop approach for cross-reference
│   └── read_ship_to.py           # Paired with crop_ship_to.py: OCR cropped PNGs
├── pytest.ini
├── requirements-dev.txt          # Adds pytest for the test suite
└── venv/                         # Python virtual environment
```

## Questions or Issues?

Review `CLAUDE.md` for detailed workflow documentation and business rules.
