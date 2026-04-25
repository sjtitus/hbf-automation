# Next Steps

## Current status (2026-04-19)

- PDF parsing: working (Badger vertical-text format)
- Customer validation: working (358 customers)
- Business rules: working (bill date, due date, category mapping)
- CSV export for QB batch-bills import: working

Run `python3 process_badger.py <invoice-dir>` to process every PDF in the directory and write an aggregated CSV to `quickbooks-imports/`. Add `--dry-run` to preview only (no CSV written).

## Outstanding work

1. **Outlook email intake** — auto-fetch invoices so Jessica doesn't drop files manually. Graph API preferred; folder-watch fallback is cheaper.
2. **Distributor invoice handling** — consignee not in `data/hbf-customers.xls` currently logs + skips. Need SO-number lookup in the external system to resolve the real customer.
3. **Scotlyn vendor workflow** — implement `hbf_shipping/vendors/scotlyn/` per the vendor contract (see CLAUDE.md), then register in `hbf_shipping/vendors/__init__.py` and add a `process_scotlyn.py` shim.
4. **MRS vendor workflow** — same pattern as Scotlyn, in `hbf_shipping/vendors/mrs/`.

## Reference URLs

- Outlook web: https://outlook.office.com/mail
