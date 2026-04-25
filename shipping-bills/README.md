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
python3 process_badger.py ./badger-invoices/
```
This processes every `*.pdf` in the directory and writes an aggregated
QuickBooks batch-bills CSV to `quickbooks-imports/bills-badger-YYYYMMDD-HHMMSS.csv`.

**Preview without writing the CSV:**
```bash
python3 process_badger.py --dry-run ./badger-invoices/
```

The vendor-agnostic form (equivalent):
```bash
python3 -m hbf_shipping --vendor badger ./badger-invoices/
```

**Always run from the project root.** Output directories (`logs/`,
`quickbooks-imports/`) are resolved relative to the current working
directory.

## Local-only data (not in this repo)

Two things are needed at runtime but are intentionally **not** committed:

1. **`data/hbf-customers.xls`** — the master customer list (business-confidential). Obtain from the company's secure share and place it in `data/`. The directory is committed (with a `.gitkeep` placeholder); the spreadsheet is gitignored.
2. **`badger-invoices/*.pdf`** — production invoice PDFs you want to process. Drop them into `badger-invoices/` (or pass a different directory on the command line). The directory is committed; PDFs are gitignored.

Per-run output (`logs/<run-id>/`, `quickbooks-imports/bills-<run-id>.csv`) is also gitignored — these contain customer data and regenerate on every run.

## What it does

✓ **It DOES:**
- Parse PDF invoices and extract all relevant data
- Validate customers against the customer list
- Apply all business rules for QuickBooks entry
- Write a QuickBooks batch-bills CSV (or preview it with `--dry-run`)
- Produce a self-contained per-run artifact directory under `./logs/<run-id>/` containing a run log, per-invoice debug logs, a summary CSV, and a manifest

✗ **It DOES NOT:**
- Connect to Outlook email
- Talk to QuickBooks directly — the CSV is imported manually
- Make any external connections

## What Gets Logged

Each run creates `logs/<vendor>-YYYY-MM-DDTHH-MM-SSET-XXXXXX/` containing:

- **`run.log`** — INFO+ run-wide log (mirrors stdout)
- **`<invoice-stem>.log`** — DEBUG+ per-invoice debug log (one per PDF; flushed aggressively for post-mortem after a crash)
- **`summary.csv`** — one row per invoice with status, key extracted fields, and a path to the per-invoice log
- **`manifest.json`** — machine-readable index of artifacts

## Supported Vendors

- ✓ Badger State Western
- ☐ Scotlyn (planned)
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
├── hbf_shipping/                 # The package
│   ├── cli.py                    # Argument parsing + vendor dispatch
│   ├── pipeline.py               # Vendor-agnostic processing pipeline
│   ├── bill_entry.py             # Shared BillEntry dataclass
│   ├── customer_lookup.py        # Customer validation
│   ├── csv_export.py             # CSV writer + dry-run preview
│   ├── processing_log.py         # Per-invoice summary CSV writer
│   ├── run_logging.py            # Per-run log dir + per-invoice log handler + manifest
│   └── vendors/
│       ├── __init__.py           # VENDORS registry
│       └── badger/
│           ├── parser.py         # PDF text extraction (page 1, all fields incl. consignee)
│           ├── ocr.py            # Page-2 BOL OCR — kept for ad-hoc debugging; not on production path
│           └── rules.py          # Badger business rules + build_bill_entry
├── data/
│   └── hbf-customers.xls         # Customer master list (editable)
├── badger-invoices/              # Drop-zone for invoice PDFs to process
├── quickbooks-imports/           # Generated batch-bills CSVs (one per run)
├── logs/                         # Per-run dirs: run.log, per-invoice .log, summary.csv, manifest.json
├── tests/
│   ├── test_vendor_regression.py # Parametrized end-to-end regression test
│   └── fixtures/<vendor>/        # Test PDFs + .expected.json goldens (gitignored)
├── tools/
│   └── refresh_goldens.py        # (Re)generate goldens for one or all PDFs
├── pytest.ini
├── requirements-dev.txt          # Adds pytest for the test suite
└── venv/                         # Python virtual environment
```

## Questions or Issues?

Review `CLAUDE.md` for detailed workflow documentation and business rules.
