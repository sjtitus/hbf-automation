"""
Vendor-agnostic invoice ShipTo extraction pipeline (stage 1).

Per invoice:
    parse PDF → page-1 InvoiceExtraction → page-2 BolExtraction → log summary

Stage 1 stops here: no customer lookup, no BillEntry construction, no CSV.
The next stage will rebuild matching/CSV against the new canonical ShipTo
shape produced here.

The vendor module must provide:
    parse_invoice(pdf_path) -> (invoice_data: dict, reasons: dict)
    extract_invoice_ship_to(pdf_path, invoice_data) -> InvoiceExtraction
    SHIPPING_COMPANY: str
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from .bol_ship_to import extract_ship_to
from .customer_address_map import load_address_to_customers
from .run_logging import invoice_logger


logger = logging.getLogger(__name__)


def _fmt_addr(addr) -> str:
    """One-line dump of a NormalizedAddress (or 'None' if missing)."""
    if addr is None:
        return 'None'
    parts = [
        f"street={addr.street!r}",
        f"line_2={addr.line_2!r}",
        f"city={addr.city!r}",
        f"state={addr.state!r}",
        f"postcode={addr.postcode!r}",
    ]
    return '  '.join(parts)


def _agree_addr(a, b) -> str:
    """Compare two NormalizedAddresses for downstream-matching purposes.
    Currently strict equality on all 5 fields; reports which fields
    differ on a mismatch. Both-None returns 'both missing'.
    """
    if a is None and b is None:
        return 'both missing'
    if a is None:
        return 'page-1 missing'
    if b is None:
        return 'BOL missing'
    if a == b:
        return 'EXACT'
    diffs = []
    for f in ('street', 'line_2', 'city', 'state', 'postcode'):
        if getattr(a, f) != getattr(b, f):
            diffs.append(f"{f}: {getattr(a, f)!r} != {getattr(b, f)!r}")
    return 'DIFFER (' + '; '.join(diffs) + ')'


def _agree_name(a: str | None, b: str | None) -> str:
    if not a and not b:
        return 'both missing'
    if not a:
        return 'page-1 missing'
    if not b:
        return 'BOL missing'
    if a == b:
        return 'EXACT'
    if a.lower().strip() == b.lower().strip():
        return 'case-insensitive match'
    return f'DIFFER ({a!r} vs {b!r})'


class Pipeline:
    """Stage 1: parse + extract ShipTo (page-1 and BOL) + log summary.

    Also runs customer-master validation at startup. With
    `strict_master=True`, any hard-rule violation aborts via
    `MasterValidationError`. The validation report is always written to
    `<run_dir>/customer_master_validation.log`.
    """

    def __init__(self, vendor, run_id: str, run_dir: Path,
                 strict_master: bool = False):
        self.vendor = vendor
        self.run_id = run_id
        self.run_dir = run_dir
        self.diagnostic_dir = run_dir / 'shipto_diagnostics'
        self.results: list[tuple[Path, object, object]] = []
        self.batch_started: str | None = None
        self.batch_ended: str | None = None

        # Load + validate the customer master at startup. Validation log
        # lands in run_dir; strict mode propagates via MasterValidationError.
        self.address_map = load_address_to_customers(
            strict=strict_master, log_dir=run_dir,
        )

    def process_invoice(self, pdf_path) -> bool:
        """Run page-1 + BOL ShipTo extraction on one PDF. Logs a summary
        block. Returns True if both extractions produced a usable ShipTo
        (success=True), False otherwise.
        """
        pdf_path = Path(pdf_path)

        with invoice_logger(self.run_dir, pdf_path.stem):
            logger.info(f"\n{'='*70}")
            logger.info(f"Processing invoice: {pdf_path.name}")
            logger.info(f"{'='*70}")

            try:
                invoice_data, _reasons = self.vendor.parse_invoice(str(pdf_path))
            except Exception as e:
                logger.error(f"parse_invoice raised: {e}", exc_info=True)
                self.results.append((pdf_path, None, None))
                return False

            inv = None
            try:
                inv = self.vendor.extract_invoice_ship_to(pdf_path, invoice_data)
            except Exception as e:
                logger.error(f"extract_invoice_ship_to raised: {e}", exc_info=True)

            bol = None
            try:
                bol = extract_ship_to(pdf_path, diagnostic_dir=self.diagnostic_dir)
            except Exception as e:
                logger.error(f"extract_ship_to (BOL) raised: {e}", exc_info=True)

            self.results.append((pdf_path, inv, bol))
            self._log_per_invoice_summary(pdf_path, inv, bol)

            return bool(inv and inv.success and bol and bol.success)

    def _log_per_invoice_summary(self, pdf_path: Path, inv, bol):
        logger.info("\n--- PAGE-1 (Invoice) ---")
        if inv is None:
            logger.info("  <extractor raised; see traceback above>")
        else:
            logger.info(f"  success:         {inv.success}")
            if inv.failure_reason:
                logger.info(f"  failure_reason:  {inv.failure_reason}")
            logger.info(f"  name:            {inv.ship_to.name!r}")
            logger.info(f"  name_candidates: {inv.ship_to.name_candidates}")
            logger.info(f"  address:         {_fmt_addr(inv.ship_to.address)}")

        logger.info("\n--- BOL (page-2) ---")
        if bol is None:
            logger.info("  <extractor raised; see traceback above>")
        else:
            logger.info(f"  success:         {bol.success}")
            if bol.failure_reason:
                logger.info(f"  failure_reason:  {bol.failure_reason}")
            logger.info(f"  name:            {bol.ship_to.name!r}")
            logger.info(f"  name_candidates: {bol.ship_to.name_candidates}")
            logger.info(f"  address:         {_fmt_addr(bol.ship_to.address)}")
            if bol.diagnostic_path:
                logger.info(f"  diagnostic_png:  {bol.diagnostic_path}")
            logger.info(f"  raw_lines:       {bol.raw_lines}")

        if inv is not None and bol is not None:
            logger.info("\n--- AGREE ---")
            logger.info(f"  address: {_agree_addr(inv.ship_to.address, bol.ship_to.address)}")
            logger.info(f"  name:    {_agree_name(inv.ship_to.name, bol.ship_to.name)}")

    def process_batch(self, folder_path) -> dict:
        folder = Path(folder_path)
        self.batch_started = datetime.now().isoformat(timespec='seconds')

        if not folder.exists() or not folder.is_dir():
            logger.error(f"Folder not found: {folder}")
            self.batch_ended = datetime.now().isoformat(timespec='seconds')
            return {'total': 0, 'succeeded': 0, 'failed': 0}

        pdf_files = sorted(folder.glob('*.pdf'))
        if not pdf_files:
            logger.warning(f"No PDF files found in {folder}")
            self.batch_ended = datetime.now().isoformat(timespec='seconds')
            return {'total': 0, 'succeeded': 0, 'failed': 0}

        logger.info(f"\nFound {len(pdf_files)} PDF file(s) to process\n")

        succeeded = failed = 0
        for pdf in pdf_files:
            if self.process_invoice(pdf):
                succeeded += 1
            else:
                failed += 1

        self.batch_ended = datetime.now().isoformat(timespec='seconds')
        return {
            'total': len(pdf_files),
            'succeeded': succeeded,
            'failed': failed,
        }

    def report(self) -> None:
        """Print a final cross-invoice summary table to the run log / stdout."""
        if not self.results:
            return

        logger.info(f"\n{'='*78}")
        logger.info("STAGE-1 SHIP TO EXTRACTION — BATCH SUMMARY")
        logger.info(f"{'='*78}")

        header = f"{'Invoice':<32}  {'PG1':<4}  {'BOL':<4}  {'Addr Agree':<8}  {'Name Agree':<8}"
        logger.info(header)
        logger.info('-' * len(header))

        n_inv_ok = n_bol_ok = n_addr_agree = n_name_agree = 0
        for pdf_path, inv, bol in self.results:
            inv_ok = bool(inv and inv.success)
            bol_ok = bool(bol and bol.success)
            n_inv_ok += int(inv_ok)
            n_bol_ok += int(bol_ok)

            inv_addr = inv.ship_to.address if inv else None
            bol_addr = bol.ship_to.address if bol else None
            inv_name = inv.ship_to.name if inv else None
            bol_name = bol.ship_to.name if bol else None

            addr_agree = (inv_addr is not None and bol_addr is not None
                          and inv_addr == bol_addr)
            name_agree = bool(inv_name and bol_name
                              and inv_name.lower().strip()
                              == bol_name.lower().strip())
            n_addr_agree += int(addr_agree)
            n_name_agree += int(name_agree)

            logger.info(
                f"{pdf_path.name:<32}  "
                f"{'OK' if inv_ok else 'FAIL':<4}  "
                f"{'OK' if bol_ok else 'FAIL':<4}  "
                f"{'YES' if addr_agree else 'no':<8}  "
                f"{'YES' if name_agree else 'no':<8}"
            )

        n = len(self.results)
        logger.info('-' * len(header))
        logger.info(f"Totals: {n} invoices  |  page-1 OK: {n_inv_ok}/{n}  |  "
                    f"BOL OK: {n_bol_ok}/{n}  |  addr agree: {n_addr_agree}/{n}  |  "
                    f"name agree: {n_name_agree}/{n}")
        logger.info(f"BOL diagnostic PNGs: {self.diagnostic_dir}/")
        logger.info(f"{'='*78}\n")
