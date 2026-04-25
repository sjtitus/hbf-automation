"""
PDF Parser for Badger State Western invoices.
Extracts structured data from Badger invoice PDFs.

Most fields come from page 1 (text-extractable). The consignee (customer)
comes from page 2 via OCR of the BOL SHIP TO block — see bol_ocr.py —
because page 1's consignee is often abbreviated or ambiguous.

Every _extract_* method returns (value, reason):
    - value is the extracted value, or None on failure
    - reason is None on success, or a short human-readable string describing
      what was checked and why no match was found

parse() aggregates these into (data, reasons) — two dicts keyed by field name.
"""

import logging
import re
from datetime import datetime
from pypdf import PdfReader

from .ocr import extract_ship_to_customer


logger = logging.getLogger(__name__)


class BadgerInvoiceParser:
    """Parses Badger State Western invoice PDFs."""

    def __init__(self, pdf_path):
        self.pdf_path = pdf_path
        self.reader = PdfReader(pdf_path)
        self.first_page_text = self.reader.pages[0].extract_text()

    def parse(self):
        """Extract invoice data. Returns (data, reasons) — dicts keyed by field name.

        data[field]    = extracted value, or None on failure
        reasons[field] = None on success, reason string on failure
        """
        text = self.first_page_text
        logger.debug("page-1 text length=%d chars", len(text))
        extractors = {
            'invoice_number': self._extract_invoice_number,
            'invoice_date':   self._extract_invoice_date,
            'ship_date':      self._extract_ship_date,
            'shipper':        self._extract_shipper,
            'consignee':      self._extract_consignee,
            'so_number':      self._extract_so_number,
            'total_amount':   self._extract_total_amount,
            'past_due_date':  self._extract_past_due_date,
        }
        data, reasons = {}, {}
        for field, fn in extractors.items():
            value, reason = fn(text)
            data[field] = value
            reasons[field] = reason
            if value is not None:
                logger.debug("extract %s -> %r", field, value)
            else:
                logger.debug("extract %s FAILED: %s", field, reason)
        return data, reasons

    def _extract_invoice_number(self, text):
        match = re.search(r'INVOICE\s+(\d{7})', text)
        if match:
            return match.group(1), None
        match = re.search(r'\b(\d{7})\b', text)
        if match:
            return match.group(1), None
        return None, "no 7-digit invoice number found (tried 'INVOICE <7-digits>' then any 7-digit token)"

    def _extract_invoice_date(self, text):
        dates = re.findall(r'\d{2}/\d{2}/\d{4}', text)
        if len(dates) >= 1:
            return datetime.strptime(dates[0], '%m/%d/%Y'), None
        return None, "no MM/DD/YYYY date found in text (invoice date is the 1st date)"

    def _extract_ship_date(self, text):
        dates = re.findall(r'\d{2}/\d{2}/\d{4}', text)
        if len(dates) >= 2:
            return datetime.strptime(dates[1], '%m/%d/%Y'), None
        return None, f"fewer than 2 MM/DD/YYYY dates found (ship date is the 2nd date; found {len(dates)})"

    def _extract_shipper(self, text):
        """
        Common shippers: Old Wisconsin Sausage Company, Midwest Refrigerated Services, DairyFood USA
        """
        shippers = [
            'Old Wisconsin Sausage Company',
            'Midwest Refrigerated Services',
            'DairyFood USA',
            'MRS',  # Abbreviation for Midwest Refrigerated Services
        ]
        for shipper in shippers:
            if shipper in text:
                if shipper == 'MRS' and 'Midwest Refrigerated Services' not in text:
                    return 'Midwest Refrigerated Services', None
                return shipper, None
        match = re.search(r'S\s*H\s*I\s*P\s*P\s*E\s*R.*?([A-Z][A-Za-z\s&]+)\n', text, re.DOTALL)
        if match:
            return match.group(1).strip(), None
        return None, f"no known shipper matched (looked for {shippers}); SHIPPER-block fallback also failed"

    def _extract_consignee(self, text):
        """Consignee comes from the page-2 BOL SHIP TO block (OCR).

        The page-1 "CONSIGNEE" field on Badger invoices is often abbreviated
        (e.g. "FCI" instead of "Tucson FCI"), which breaks customer lookup.
        OCR of the BOL on page 2 gives the fully-qualified name that HBF
        uses as the customer.
        """
        return extract_ship_to_customer(self.pdf_path)

    def _extract_so_number(self, text):
        """Extract sales order number — accepts 'SO-' or 'S0-' (OCR artifact).

        These are Sales Orders, so the canonical prefix is letter-O "SO-".
        The returned value is normalized to "SO-<digits>" even if the PDF
        text-stream produced digit-0.
        """
        match = re.search(r'S[O0]-(\d+)', text)
        if match:
            return f'SO-{match.group(1)}', None
        return None, "no match for pattern 'S[O0]-<digits>' in extracted text (tolerant of letter-O vs digit-0)"

    def _extract_total_amount(self, text):
        amounts = re.findall(r'\$?([\d,]+\.\d{2})', text)
        if amounts:
            for amount in reversed(amounts):
                if ',' in amount:
                    return float(amount.replace(',', '')), None
            return float(amounts[-1].replace(',', '')), None
        return None, "no $X.XX amount found in text (expected total near 'PLEASE PAY THIS AMOUNT')"

    def _extract_past_due_date(self, text):
        dates = re.findall(r'\d{2}/\d{2}/\d{4}', text)
        if len(dates) >= 3:
            return datetime.strptime(dates[2], '%m/%d/%Y'), None
        return None, f"fewer than 3 MM/DD/YYYY dates found (past-due date is the 3rd date; found {len(dates)})"


def parse_invoice(pdf_path):
    """Parse a Badger invoice PDF. Returns (data, reasons) — dicts keyed by field name."""
    return BadgerInvoiceParser(pdf_path).parse()
