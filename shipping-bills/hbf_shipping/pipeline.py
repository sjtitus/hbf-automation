"""
Vendor-agnostic invoice-processing pipeline.

For every vendor the per-invoice steps are the same:
    parse PDF → validate required fields → look up customer →
    build BillEntry → collect.

Vendor-specific behavior comes from the vendor module passed in, which must
provide:
    parse_invoice(pdf_path) -> (invoice_data: dict, reasons: dict)
    build_bill_entry(invoice_data, customer_name) -> BillEntry
    REQUIRED_FIELDS: tuple[str, ...]
    SHIPPING_COMPANY: str
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .bill_entry import BillEntry
from .csv_export import format_bills_preview, write_bills_csv
from .customer_address_map import (
    load_address_to_customers,
    lookup_with_name_fallback,
)
from .processing_log import write_processing_log
from .run_logging import invoice_logger, write_manifest


logger = logging.getLogger(__name__)


CSV_OUT_DIR = Path('quickbooks-imports')


class Pipeline:
    """Runs the parse → validate → build → collect pipeline for one vendor."""

    def __init__(self, vendor, run_id: str, run_dir: Path, dry_run: bool = False):
        self.vendor = vendor
        self.run_id = run_id
        self.run_dir = run_dir
        self.dry_run = dry_run
        self.address_map = load_address_to_customers()
        self.collected_entries: list[BillEntry] = []
        self.processing_log_rows: list[dict] = []
        self.invoice_log_paths: list[Path] = []
        self.batch_started: str | None = None
        self.batch_ended: str | None = None
        self.batch_totals: dict = {'total': 0, 'succeeded': 0, 'failed': 0}

    def process_invoice(self, pdf_path) -> bool:
        """Process a single invoice PDF. Returns True on success.

        Always appends exactly one row to self.processing_log_rows before
        returning, capturing start/end time, status, the per-invoice log
        path, and best-effort field values populated up to the failure point.
        """
        pdf_path = Path(pdf_path)
        start = datetime.now().isoformat(timespec='seconds')
        current_step = 'parse_pdf'

        # Best-effort field collection — _record() reads from this dict so
        # even partially-extracted invoices contribute as much detail as we
        # have to the summary CSV row.
        ctx: dict = {
            'bill_number': None,
            'so_number': None,
            'consignee': None,
            'total_amount': None,
            'pdf_address': '',
            'cm_method': '',
            'cm_score': '',
            'cm_count': 0,
            'cm_matched': '',
            'cm_near_miss': '',
            'cm_near_miss_score': '',
            'name_method': '',
            'name_score': '',
        }

        with invoice_logger(self.run_dir, pdf_path.stem) as log_path:
            self.invoice_log_paths.append(log_path)

            def _record(status, fail_step='N/A', fail_message='N/A', fail_detail='N/A'):
                self.processing_log_rows.append({
                    'Run ID': self.run_id,
                    'Shipping Company': self.vendor.SHIPPING_COMPANY,
                    'Invoice File': pdf_path.name,
                    'Processing Start': start,
                    'Processing End': datetime.now().isoformat(timespec='seconds'),
                    'Status': status,
                    'Bill Number': ctx['bill_number'] or 'N/A',
                    'SO Number': ctx['so_number'] or 'N/A',
                    'Consignee': ctx['consignee'] or 'N/A',
                    'CustomerMatch: PDF Address': ctx['pdf_address'] or 'N/A',
                    'CustomerMatch: Method': ctx['cm_method'] or 'N/A',
                    'CustomerMatch: Score': ctx['cm_score'] if ctx['cm_score'] != '' else 'N/A',
                    'CustomerMatch: Count': ctx['cm_count'],
                    'CustomerMatch: Matched': ctx['cm_matched'] or 'N/A',
                    'CustomerMatch: Near Miss': ctx['cm_near_miss'] or 'N/A',
                    'CustomerMatch: Near Miss Score': (
                        ctx['cm_near_miss_score']
                        if ctx['cm_near_miss_score'] != '' else 'N/A'
                    ),
                    'NameMatch: Method': ctx['name_method'] or 'N/A',
                    'NameMatch: Score': (
                        ctx['name_score'] if ctx['name_score'] != '' else 'N/A'
                    ),
                    'Total Amount': (
                        f"{ctx['total_amount']:.2f}"
                        if ctx['total_amount'] is not None else 'N/A'
                    ),
                    'Log File': str(log_path),
                    'Fail Step': fail_step,
                    'Fail Message': fail_message,
                    'Fail Detail': fail_detail,
                })

            logger.info(f"\n{'='*70}")
            logger.info(f"Processing invoice: {pdf_path.name}")
            logger.info(f"{'='*70}")

            try:
                current_step = 'parse_pdf'
                logger.info("Step 1: Parsing PDF...")
                invoice_data, reasons = self.vendor.parse_invoice(str(pdf_path))

                # Capture whatever the parser produced — even partial — so
                # the summary CSV gets best-effort values on a downstream fail.
                ctx['bill_number'] = invoice_data.get('invoice_number')
                ctx['so_number'] = invoice_data.get('so_number')
                ctx['consignee'] = invoice_data.get('consignee')
                ctx['total_amount'] = invoice_data.get('total_amount')

                al1   = invoice_data.get('consignee_address_line_1')
                city  = invoice_data.get('consignee_city')
                state = invoice_data.get('consignee_state')
                pc    = invoice_data.get('consignee_postcode')
                if al1 and city and state and pc:
                    ctx['pdf_address'] = f"{al1}, {city}, {state} {pc}"

                current_step = 'validate_fields'
                missing = self._validate(invoice_data)
                if missing is not None:
                    message = f"Missing required field: {missing}"
                    detail = reasons.get(missing) or 'N/A'
                    logger.error(message)
                    logger.error(f"  reason: {detail}")
                    logger.error("Failed to extract required invoice data")
                    _record('FAIL', current_step, message, detail)
                    return False

                logger.info(f"  ✓ Invoice #: {invoice_data['invoice_number']}")
                logger.info(f"  ✓ Consignee: {invoice_data['consignee']}")
                logger.info(f"  ✓ Amount: ${invoice_data['total_amount']:,.2f}")

                current_step = 'customer_lookup'
                logger.info("\nStep 2: Looking up customer by address (with name fallback)...")
                result = lookup_with_name_fallback(
                    self.address_map,
                    invoice_data['consignee'],
                    al1, city, state, pc,
                )
                n = len(result.pairs)
                ctx['cm_method']    = result.cm_method
                ctx['cm_score']     = result.addr_score
                ctx['cm_count']     = n
                ctx['name_method']  = result.name_method
                ctx['name_score']   = (
                    result.name_score if result.name_method != 'n/a' else ''
                )
                logger.info(
                    f"  lookup → cm_method={result.cm_method} addr_score={result.addr_score} "
                    f"count={n} name_method={result.name_method} name_score={result.name_score}"
                )

                if result.cm_method == 'no_match':
                    near_miss_name = result.pairs[0][1].name if result.pairs else ''
                    near_miss_score = (
                        result.name_score if result.name_method == 'tried_failed'
                        and result.name_score >= result.addr_score
                        else result.addr_score
                    )
                    ctx['cm_near_miss'] = near_miss_name
                    ctx['cm_near_miss_score'] = near_miss_score
                    message = (
                        f"No customer match (best near-miss: "
                        f"{near_miss_name or 'none'} at score {near_miss_score})"
                    )
                    logger.warning(f"  {message}")
                    _record('FAIL', current_step, message)
                    return False

                # Match (or unresolved multi-match) — populate the matched column.
                matched_names = [p[1].name for p in result.pairs]
                ctx['cm_matched'] = ' | '.join(matched_names)
                logger.info(f"  matched: {ctx['cm_matched']}")

                if result.cm_method == 'multi_match_unresolved':
                    detail = (
                        "name disambiguation skipped (generic consignee)"
                        if result.name_method == 'n/a'
                        else f"name disambiguation tried but failed (best score {result.name_score})"
                    )
                    message = f"Multiple customers matched ({n}) — {detail}"
                    logger.warning(f"  {message}")
                    _record('FAIL', current_step, message)
                    return False

                # Exactly one match — proceed to bill entry.
                customer_name = result.pairs[0][1].name

                current_step = 'build_bill_entry'
                logger.info("\nStep 3: Preparing bill entry...")
                entry = self.vendor.build_bill_entry(invoice_data, customer_name)

                self.collected_entries.append(entry)
                logger.info("✓ Invoice ready")
                logger.info(f"{'='*70}\n")
                _record('SUCCESS')
                return True

            except Exception as e:
                logger.error(f"Error processing invoice: {e}", exc_info=True)
                _record('FAIL', current_step, str(e))
                return False

    def _validate(self, data: dict) -> str | None:
        for field in self.vendor.REQUIRED_FIELDS:
            if data.get(field) is None:
                return field
        return None

    def flush(self, vendor_slug: str) -> Path | None:
        """Print the CSV preview; write the summary CSV (always), the bills
        CSV (unless --dry-run), and a manifest.json index in the run dir.
        Returns the bills CSV path, or None.
        """
        if not self.processing_log_rows:
            return None

        summary_csv = self.run_dir / 'summary.csv'
        write_processing_log(self.processing_log_rows, summary_csv)
        logger.info(f"\n✓ Wrote processing log ({len(self.processing_log_rows)} row(s)) to: {summary_csv}")

        bills_csv: Path | None = None
        if self.collected_entries:
            entry_dicts = [e.to_dict() for e in self.collected_entries]

            preview = format_bills_preview(entry_dicts)
            banner = f"CSV PREVIEW — {len(entry_dicts)} bill(s)"
            logger.info(f"\n{'='*70}\n{banner}\n{'='*70}\n{preview}\n{'='*70}")

            if not self.dry_run:
                bills_csv = CSV_OUT_DIR / f'bills-{self.run_id}.csv'
                write_bills_csv(entry_dicts, bills_csv)
                logger.info(f"\n✓ Wrote {len(entry_dicts)} bill(s) to: {bills_csv}")

        self._write_manifest(vendor_slug, bills_csv)
        return bills_csv

    def _write_manifest(self, vendor_slug: str, bills_csv: Path | None):
        """Write manifest.json into the run dir as the canonical artifact index."""
        def _rel_to_run_dir(p: Path | None) -> str | None:
            if p is None:
                return None
            try:
                return str(Path('..') / '..' / p)
            except ValueError:
                return str(p)

        payload = {
            'run_id': self.run_id,
            'vendor': vendor_slug,
            'started': self.batch_started,
            'ended': self.batch_ended,
            'totals': self.batch_totals,
            'dry_run': self.dry_run,
            'artifacts': {
                'run_log': 'run.log',
                'invoice_logs': sorted(p.name for p in self.invoice_log_paths),
                'summary_csv': 'summary.csv',
                'bills_csv': _rel_to_run_dir(bills_csv),
            },
        }
        path = write_manifest(self.run_dir, payload)
        logger.info(f"✓ Wrote manifest to: {path}")

    def process_batch(self, folder_path) -> dict:
        """Process all PDFs in a folder."""
        folder = Path(folder_path)
        self.batch_started = datetime.now().isoformat(timespec='seconds')

        if not folder.exists() or not folder.is_dir():
            logger.error(f"Folder not found: {folder}")
            self.batch_ended = datetime.now().isoformat(timespec='seconds')
            return {'total': 0, 'processed': 0, 'failed': 0}

        pdf_files = list(folder.glob('*.pdf'))

        if not pdf_files:
            logger.warning(f"No PDF files found in {folder}")
            self.batch_ended = datetime.now().isoformat(timespec='seconds')
            return {'total': 0, 'processed': 0, 'failed': 0}

        logger.info(f"\nFound {len(pdf_files)} PDF file(s) to process\n")

        processed = 0
        failed = 0

        for pdf_file in pdf_files:
            if self.process_invoice(str(pdf_file)):
                processed += 1
            else:
                failed += 1

        self.batch_ended = datetime.now().isoformat(timespec='seconds')
        self.batch_totals = {
            'total': len(pdf_files),
            'succeeded': processed,
            'failed': failed,
        }

        logger.info(f"\n{'='*70}")
        logger.info("BATCH PROCESSING SUMMARY")
        logger.info(f"{'='*70}")
        logger.info(f"Total files:      {len(pdf_files)}")
        logger.info(f"Processed:        {processed}")
        logger.info(f"Failed:           {failed}")
        logger.info(f"{'='*70}\n")

        return {
            'total': len(pdf_files),
            'processed': processed,
            'failed': failed,
        }
