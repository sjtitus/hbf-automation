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

from .bill_entry import BillEntry
from .bol_ship_to import extract_ship_to
from .consignee_discrepancy import (
    compare_consignee_to_master,
    write_discrepancies_csv,
)
from .csv_export import format_bills_preview, write_bills_csv
from .customer_address_map import (
    InvoiceMatchResult,
    MatchMethod,
    _format_address_summary,
    format_match_log,
    load_master,
    match_invoice_customer,
)
from .processing_log import build_summary_row, write_processing_log
from .run_logging import invoice_logger, write_manifest


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


_FAIL_STEP_PARSE       = 'parse_pdf'
_FAIL_STEP_VALIDATE    = 'validate_fields'
_FAIL_STEP_EXTRACT     = 'extract_ship_to'
_FAIL_STEP_MATCH       = 'match_customer'
_FAIL_STEP_BILL        = 'build_bill_entry'


@dataclass(frozen=True)
class InvoiceOutcome:
    """One row of the batch summary. Fully describes one invoice's
    journey through the pipeline — what was parsed, what was matched,
    where (if anywhere) it failed, and the resulting BillEntry on
    success. Consumed by `Pipeline.finalize` to emit the QB CSV +
    summary CSV + manifest."""
    pdf_path: Path
    invoice_data: Optional[dict]              # vendor.parse_invoice output, if it ran
    inv: object                               # InvoiceExtraction or None
    bol: object                               # BolExtraction or None
    match: Optional[InvoiceMatchResult]
    log_path: Path                            # per-invoice .log path
    processing_start: str                     # ISO8601
    processing_end: str                       # ISO8601
    fail_step: Optional[str]                  # one of _FAIL_STEP_* or None
    fail_message: Optional[str]
    fail_detail: Optional[str]
    bill_entry: Optional[BillEntry]           # populated only on full-pipeline success


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
                 vendor_slug: str,
                 strict_master: bool = False):
        self.vendor = vendor
        self.vendor_slug = vendor_slug
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
        """Run extraction + customer matching + bill-entry build on one
        PDF. Returns True iff every stage succeeded (bill_entry is
        populated). Always appends an `InvoiceOutcome` to `self.outcomes`
        capturing where (if anywhere) the invoice fell off the path."""
        pdf_path = Path(pdf_path)

        invoice_data: Optional[dict] = None
        inv = None
        bol = None
        match: Optional[InvoiceMatchResult] = None
        bill_entry: Optional[BillEntry] = None
        fail_step: Optional[str] = None
        fail_message: Optional[str] = None
        fail_detail: Optional[str] = None

        processing_start = datetime.now().isoformat(timespec='seconds')

        with invoice_logger(self.run_dir, pdf_path.stem) as log_path:
            logger.info(f"\n{'='*70}")
            logger.info(f"Processing invoice: {pdf_path.name}")
            logger.info(f"{'='*70}")

            # Stage 1: parse PDF
            try:
                invoice_data, reasons = self.vendor.parse_invoice(str(pdf_path))
            except Exception as e:
                logger.error(f"parse_invoice raised: {e}", exc_info=True)
                fail_step = _FAIL_STEP_PARSE
                fail_message = f"parse_invoice raised {type(e).__name__}"
                fail_detail = str(e)

            # Stage 2: validate required fields
            if fail_step is None:
                missing_field, missing_reason = self._first_missing_required(
                    invoice_data, reasons,
                )
                if missing_field is not None:
                    fail_step = _FAIL_STEP_VALIDATE
                    fail_message = f"required field missing: {missing_field}"
                    fail_detail = missing_reason or f"{missing_field} not extracted"
                    logger.error(
                        f"validate_fields failed: {fail_message} "
                        f"({fail_detail})"
                    )

            # Stage 3: extract ship-to (page-1 + BOL). Both are
            # best-effort; the matcher tolerates None on either side.
            # An exception inside an extractor is logged but does NOT
            # short-circuit the pipeline — the matcher will see None.
            if fail_step is None:
                try:
                    inv = self.vendor.extract_invoice_ship_to(pdf_path, invoice_data)
                except Exception as e:
                    logger.error(f"extract_invoice_ship_to raised: {e}", exc_info=True)
                try:
                    bol = extract_ship_to(
                        pdf_path,
                        profile=self.vendor.BOL_PROFILE,
                        diagnostic_dir=self.diagnostic_dir,
                    )
                except Exception as e:
                    logger.error(f"extract_ship_to (BOL) raised: {e}", exc_info=True)

                self._log_extraction_summary(inv, bol)

            # Stage 4: match against customer master
            if fail_step is None:
                match = match_invoice_customer(inv, bol, self.master)
                self._log_match_outcome(match)
                if not match.success:
                    fail_step = _FAIL_STEP_MATCH
                    fail_message = (
                        f"customer match failed: method={match.method}"
                    )
                    fail_detail = match.fail_reason or ''

            # Stage 5: build bill entry
            if fail_step is None:
                try:
                    bill_entry = self.vendor.build_bill_entry(
                        invoice_data, match.customer_name,
                    )
                except Exception as e:
                    logger.error(f"build_bill_entry raised: {e}", exc_info=True)
                    fail_step = _FAIL_STEP_BILL
                    fail_message = f"build_bill_entry raised {type(e).__name__}"
                    fail_detail = str(e)

        processing_end = datetime.now().isoformat(timespec='seconds')

        outcome = InvoiceOutcome(
            pdf_path=pdf_path,
            invoice_data=invoice_data,
            inv=inv, bol=bol,
            match=match,
            log_path=log_path,
            processing_start=processing_start,
            processing_end=processing_end,
            fail_step=fail_step,
            fail_message=fail_message,
            fail_detail=fail_detail,
            bill_entry=bill_entry,
        )
        self.outcomes.append(outcome)
        return bill_entry is not None

    def _first_missing_required(self, invoice_data, reasons):
        """Return `(field_name, reason)` for the first REQUIRED_FIELDS
        entry that's missing in `invoice_data`, or `(None, None)` if
        all are present. The `reasons` dict from `vendor.parse_invoice`
        carries the extractor's specific failure reason per field."""
        for field in self.vendor.REQUIRED_FIELDS:
            if invoice_data.get(field) is None:
                return field, (reasons or {}).get(field)
        return None, None

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

    def finalize(self, *, dry_run: bool) -> dict[str, Path]:
        """Emit run artifacts. Always writes summary.csv + manifest.json.
        Writes the QuickBooks bills CSV unless `dry_run` is set, in
        which case the bill-entry preview is logged instead.

        Returns a dict mapping artifact key → absolute path."""
        artifacts: dict[str, Path] = {}

        bill_dicts = [
            o.bill_entry.to_dict() for o in self.outcomes
            if o.bill_entry is not None
        ]

        # Bills CSV: only when at least one bill built and not dry-run
        if bill_dicts and not dry_run:
            bills_path = (
                Path('quickbooks-imports') / f'bills-{self.run_id}.csv'
            )
            write_bills_csv(bill_dicts, bills_path)
            artifacts['bills_csv'] = bills_path.resolve()
            logger.info(f"\nBills CSV: {bills_path}  ({len(bill_dicts)} entries)")
        elif dry_run and bill_dicts:
            logger.info("\n--- DRY RUN: bills preview (no CSV written) ---")
            logger.info(format_bills_preview(bill_dicts))
        elif not bill_dicts:
            logger.info("\nNo successful bill entries — bills CSV skipped.")

        # Summary CSV: always
        summary_path = self.run_dir / 'summary.csv'
        rows = [
            build_summary_row(o, self.run_id, self.vendor.SHIPPING_COMPANY)
            for o in self.outcomes
        ]
        write_processing_log(rows, summary_path)
        artifacts['summary_csv'] = summary_path.resolve()

        # Consignee discrepancies CSV: always (header-only when nothing
        # differs). Diagnostic/audit artifact; written even on dry-run.
        discrepancies = [
            d for d in (compare_consignee_to_master(o) for o in self.outcomes)
            if d is not None
        ]
        discrepancies_path = self.run_dir / 'consignee_discrepancies.csv'
        write_discrepancies_csv(
            discrepancies, discrepancies_path,
            run_id=self.run_id,
            shipping_company=self.vendor.SHIPPING_COMPANY,
        )
        artifacts['consignee_discrepancies_csv'] = discrepancies_path.resolve()
        logger.info(
            f"Consignee discrepancies: {len(discrepancies)} "
            f"(CSV: {discrepancies_path})"
        )

        # Manifest: always
        manifest_path = write_manifest(self.run_dir, self._manifest_payload(artifacts))
        artifacts['manifest'] = manifest_path.resolve()

        return artifacts

    def _manifest_payload(self, artifacts: dict[str, Path]) -> dict:
        n = len(self.outcomes)
        n_succeeded = sum(1 for o in self.outcomes if o.bill_entry is not None)
        n_failed = n - n_succeeded
        return {
            'run_id': self.run_id,
            'vendor': self.vendor_slug,
            'started': self.batch_started,
            'ended': self.batch_ended,
            'totals': {
                'total': n,
                'succeeded': n_succeeded,
                'failed': n_failed,
            },
            'artifacts': {
                'run_log': str((self.run_dir / 'run.log').resolve()),
                'summary_csv': str(artifacts.get('summary_csv', '')),
                'consignee_discrepancies_csv': str(
                    artifacts.get('consignee_discrepancies_csv', '')
                ),
                'validation_log': str(
                    (self.run_dir / 'customer_master_validation.log').resolve()
                ),
                'bills_csv': (
                    str(artifacts['bills_csv']) if 'bills_csv' in artifacts else None
                ),
                'invoice_logs': [
                    str(o.log_path.resolve()) for o in self.outcomes
                ],
            },
        }
