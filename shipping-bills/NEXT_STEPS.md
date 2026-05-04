# Next Steps

## Current status (2026-05-03)

- PDF parsing: working for **Badger** (vertical-text page-1 fields) and **Scotlynn** (label-anchored regex on plain text + layout-mode SHIPPER/CONSIGNEE block)
- Page-2 BOL OCR: working, in production, exercised by the regression suite. Per-vendor `BOL_PROFILE` is wired via the vendor contract (Scotlynn uses a profile content-identical to Badger's today, since both ship under the same standard short-form BOL)
- Customer-master validation: working (rules: required fields, triple uniqueness, could-not-extract customer number; soft warnings for duplicate / malformed-but-recovered customer numbers)
- Stage-2 customer matching: working (page-1 + BOL → `match_invoice_customer` → `InvoiceMatchResult` with cross-source policy)
- Pipeline integration: bills CSV + summary CSV + manifest emitted per run
- Regression suite: 17 vendor regressions (14 Badger + 3 Scotlynn) + 65 unit tests, exercises the production stack including BOL OCR

Run `venv/bin/python process_badger.py <invoice-dir>` (or `process_scotlynn.py`) to process every PDF in the directory and write the run artifacts. Add `--dry-run` to suppress the bills CSV; add `--strict-master` to abort on any hard customer-master validation violation.

## Outstanding work

1. **MRS vendor workflow** — same pattern as Scotlynn, in `hbf_shipping/vendors/mrs/`.
2. **Outlook email intake** — auto-fetch invoices so Jessica doesn't drop files manually. Graph API preferred; folder-watch fallback is cheaper.
3. **Distributor invoice handling** — consignee resolves to a distributor (no master row): need SO-number lookup in the external system to resolve the real customer.

## Reference URLs

- Outlook web: https://outlook.office.com/mail
