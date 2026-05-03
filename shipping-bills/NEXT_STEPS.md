# Next Steps

## Current status (2026-05-03)

- PDF parsing: working (Badger vertical-text format, page-1 fields)
- Page-2 BOL OCR: working, in production, exercised by the regression suite
- Customer-master validation: working (rules: required fields, triple uniqueness, could-not-extract customer number; soft warnings for duplicate / malformed-but-recovered customer numbers)
- Stage-2 customer matching: working (page-1 + BOL → `match_invoice_customer` → `InvoiceMatchResult` with cross-source policy)
- Pipeline integration: bills CSV + summary CSV + manifest emitted per run
- Regression suite: 79 tests, exercises the production stack including BOL OCR

Run `venv/bin/python process_badger.py <invoice-dir>` to process every PDF in the directory and write the run artifacts. Add `--dry-run` to suppress the bills CSV; add `--strict-master` to abort on any hard customer-master validation violation.

## Outstanding work

1. **Scotlyn vendor workflow** — implement `hbf_shipping/vendors/scotlyn/` per the vendor contract (see CLAUDE.md). Likely needs a vendor-specific parser, page-1 ShipTo extractor, and possibly a BOL profile (the existing `bol_ship_to.py` may need to accept a vendor profile if Scotlyn BOLs differ in layout). Register in `hbf_shipping/vendors/__init__.py` and add a `process_scotlyn.py` shim.
2. **MRS vendor workflow** — same pattern as Scotlyn, in `hbf_shipping/vendors/mrs/`.
3. **Outlook email intake** — auto-fetch invoices so Jessica doesn't drop files manually. Graph API preferred; folder-watch fallback is cheaper.
4. **Distributor invoice handling** — consignee resolves to a distributor (no master row): need SO-number lookup in the external system to resolve the real customer.

## Reference URLs

- Outlook web: https://outlook.office.com/mail
