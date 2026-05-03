"""
Vendor-agnostic invoice processing pipeline (stage 2).

Per invoice:
    parse PDF
        → page-1 InvoiceExtraction (ShipTo)
        → page-2 BolExtraction     (ShipTo)
        → customer match against the customer master
        → log outcome

The customer-master validation runs once at startup; with
`strict_master=True`, hard violations abort. The validation report is
written to `<run_dir>/customer_master_validation.log`.

Per-invoice non-trivial match details (multi-row 4-tuple matches,
BOL-vs-page-1 disagreements, hard fails) go to the per-invoice log
at `<run_dir>/<invoice-stem>.log`.

The vendor module must provide:
    parse_invoice(pdf_path) -> (invoice_data: dict, reasons: dict)
    extract_invoice_ship_to(pdf_path, invoice_data) -> InvoiceExtraction
    SHIPPING_COMPANY: str
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from .bol_ship_to import extract_ship_to
from .customer_address_map import (
    InvoiceMatchResult,
    MatchMethod,
    _format_address_summary,
    format_match_log,
    load_master,
    match_invoice_customer,
)
from .run_logging import invoice_logger


logger = logging.getLogger(__name__)


# Match methods that constitute "non-trivial" cases worth logging in
# detail (multi-row name disambig used; cross-source disagreement;
# match denied; hard fail). Trivial cases (agree/bol_only/inv_only with
# UNIQUE on the resolving side) get a one-line note only.
_NONTRIVIAL_METHODS = frozenset({
    MatchMethod.BOL_WINS_DISAGREEMENT,
    MatchMethod.HARD_FAIL,
    MatchMethod.DENIED,
})


@dataclass(frozen=True)
class InvoiceOutcome:
    """One row of the batch summary."""
    pdf_path: Path
    inv: object                  # InvoiceExtraction or None
    bol: object                  # BolExtraction or None
    match: Optional[InvoiceMatchResult]


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


class Pipeline:
    """Stage 2 pipeline: extract ShipTos, match each invoice to a customer.

    Customer-master is loaded + validated at startup. Per-invoice work
    runs page-1 + BOL extraction, then `match_invoice_customer` to
    resolve the customer.
    """

    def __init__(self, vendor, run_id: str, run_dir: Path,
                 strict_master: bool = False):
        self.vendor = vendor
        self.run_id = run_id
        self.run_dir = run_dir
        self.diagnostic_dir = run_dir / 'shipto_diagnostics'
        self.outcomes: list[InvoiceOutcome] = []
        self.batch_started: str | None = None
        self.batch_ended: str | None = None

        # Load + validate the customer master at startup. Validation log
        # lands in run_dir; strict mode propagates via MasterValidationError.
        self.master = load_master(strict=strict_master, log_dir=run_dir)

    def process_invoice(self, pdf_path) -> bool:
        """Run extraction + customer matching on one PDF.

        Returns True on a successful match (`match.customer_name` is
        not None and method is not HARD_FAIL/DENIED).
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
                self.outcomes.append(InvoiceOutcome(pdf_path, None, None, None))
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

            self._log_extraction_summary(inv, bol)

            # Run the matcher.
            match = match_invoice_customer(inv, bol, self.master)
            self._log_match_outcome(match)

            self.outcomes.append(InvoiceOutcome(pdf_path, inv, bol, match))
            return match.customer_name is not None

    def _log_extraction_summary(self, inv, bol):
        """One-screen summary of what came out of the two extractors."""
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

    def _log_match_outcome(self, match: InvoiceMatchResult):
        """Always log the headline (method + severity + the four
        disambiguating identifiers when there's a matched entry). Log
        the full source breakdown (per-source method, name matrix,
        rejected-row diagnostic) only for non-trivial cases —
        disagreements, hard fails, denied, or any disambig/ambiguous
        per-source outcome. The disambiguating headline applies to
        BOTH trivial and non-trivial cases — distributor matches like
        Gold Star Foods need customer_id + master_row in the log
        regardless of whether name disambig was used."""
        logger.info("\n--- CUSTOMER MATCH ---")

        # Always: method + severity headline.
        logger.info(
            f"  result: method={match.method}  severity={match.severity}"
        )

        # Always (when there's a matched entry, success or DENIED):
        # the four disambiguating identifiers.
        e = match.matched_entry
        if e is not None:
            heading = 'rejected' if match.method == MatchMethod.DENIED else 'customer'
            logger.info(f"  {heading}:    {e.customer_name!r}")
            logger.info(f"  customer_id: {e.customer_number!r}")
            logger.info(f"  master row:  {e.row}")
            logger.info(f"  address:     {_format_address_summary(e.address)}")

        if match.fail_reason:
            logger.info(f"  fail_reason: {match.fail_reason}")

        # Detail dump (per-source state + name matrix) only when the
        # case is non-trivial — disagreements, hard fails, denied, or
        # either source needed name disambig / went ambiguous.
        nontrivial = (
            match.method in _NONTRIVIAL_METHODS
            or match.bol.method == MatchMethod.DISAMBIGUATED
            or match.inv.method == MatchMethod.DISAMBIGUATED
            or match.bol.method == MatchMethod.AMBIGUOUS
            or match.inv.method == MatchMethod.AMBIGUOUS
        )
        if nontrivial:
            # format_match_log renders the headline AND per-source
            # detail. We've already logged the headline above, so
            # include format_match_log's output verbatim — the small
            # duplication of headline lines is acceptable since
            # format_match_log is also called from non-pipeline contexts.
            for line in format_match_log(match).splitlines():
                logger.info(f"  {line}")

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
        if not self.outcomes:
            return

        logger.info(f"\n{'='*100}")
        logger.info("STAGE-2 INVOICE PROCESSING — BATCH SUMMARY")
        logger.info(f"{'='*100}")

        header = (
            f"{'Invoice':<34}  {'PG1':<4}  {'BOL':<4}  "
            f"{'Match Method':<24}  {'Sev':<6}  Customer"
        )
        logger.info(header)
        logger.info('-' * len(header))

        n = len(self.outcomes)
        n_pg1_ok = n_bol_ok = n_resolved = n_severe = 0
        for o in self.outcomes:
            pg1_ok = bool(o.inv and o.inv.success)
            bol_ok = bool(o.bol and o.bol.success)
            n_pg1_ok += int(pg1_ok)
            n_bol_ok += int(bol_ok)

            method = o.match.method if o.match else '<no-match-run>'
            severity = o.match.severity if o.match else '-'
            customer = o.match.customer_name if o.match and o.match.customer_name else '-'
            if o.match and o.match.customer_name is not None:
                n_resolved += 1
            if o.match and o.match.severity == 'severe':
                n_severe += 1

            logger.info(
                f"{o.pdf_path.name:<34}  "
                f"{'OK' if pg1_ok else 'FAIL':<4}  "
                f"{'OK' if bol_ok else 'FAIL':<4}  "
                f"{method:<24}  "
                f"{severity:<6}  "
                f"{customer}"
            )

        logger.info('-' * len(header))
        logger.info(
            f"Totals: {n} invoices  |  page-1 OK: {n_pg1_ok}/{n}  |  "
            f"BOL OK: {n_bol_ok}/{n}  |  resolved: {n_resolved}/{n}  |  "
            f"severe: {n_severe}/{n}"
        )
        logger.info(f"BOL diagnostic PNGs: {self.diagnostic_dir}/")
        logger.info(f"{'='*100}\n")
