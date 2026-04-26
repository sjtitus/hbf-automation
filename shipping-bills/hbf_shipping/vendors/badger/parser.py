"""
PDF Parser for Badger State Western invoices.

All fields are extracted from page-1 text. The consignee comes from the
page-1 two-column CONSIGNEE block (parsed via pypdf's
`extraction_mode='layout'` to preserve the column gap between the SHIPPER
column and the CONSIGNEE column).

OCR of the page-2 BOL is intentionally NOT used here — page-2 image
quality is inconsistent across Badger's BOL templates and produces noisy
results that have led to silent wrong matches in the past. The OCR
machinery is preserved in `ocr.py` (and is still importable for ad-hoc
debugging) but the production path stays page-1-only.

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


logger = logging.getLogger(__name__)


_CSZ_RE = re.compile(
    r'^(?P<city>.+?),\s+(?P<state>[A-Za-z]{2})\s+(?P<postcode>\d{5}(?:-\d{4})?)\s*$'
)


class BadgerInvoiceParser:
    """Parses Badger State Western invoice PDFs."""

    def __init__(self, pdf_path):
        self.pdf_path = pdf_path
        self.reader = PdfReader(pdf_path)
        self.first_page_text = self.reader.pages[0].extract_text()
        self._consignee_cache = None

    def parse(self):
        """Extract invoice data. Returns (data, reasons) — dicts keyed by field name.

        data[field]    = extracted value, or None on failure
        reasons[field] = None on success, reason string on failure
        """
        text = self.first_page_text
        logger.debug("page-1 text length=%d chars", len(text))
        extractors = {
            'invoice_number':            self._extract_invoice_number,
            'invoice_date':              self._extract_invoice_date,
            'ship_date':                 self._extract_ship_date,
            'shipper':                   self._extract_shipper,
            'consignee':                 self._extract_consignee,
            'consignee_address_line_1':  self._extract_consignee_address_line_1,
            'consignee_address_line_2':  self._extract_consignee_address_line_2,
            'consignee_city':            self._extract_consignee_city,
            'consignee_state':           self._extract_consignee_state,
            'consignee_postcode':        self._extract_consignee_postcode,
            'so_number':                 self._extract_so_number,
            'total_amount':              self._extract_total_amount,
            'past_due_date':             self._extract_past_due_date,
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
        """Consignee name (right-column row 0)."""
        return self._consignee_block()['name']

    def _extract_consignee_address_line_1(self, text):
        return self._consignee_block()['address_line_1']

    def _extract_consignee_address_line_2(self, text):
        return self._consignee_block()['address_line_2']

    def _extract_consignee_city(self, text):
        return self._consignee_block()['city']

    def _extract_consignee_state(self, text):
        return self._consignee_block()['state']

    def _extract_consignee_postcode(self, text):
        return self._consignee_block()['postcode']

    def _consignee_block(self):
        """Cached parse of the page-1 right-column CONSIGNEE block.

        Returns a dict with keys: name, address_line_1, address_line_2,
        city, state, postcode. Each value is a (value, reason) pair —
        value is None on failure with a reason string, otherwise reason is None.

        Strategy: anchor on the line ending in 'CONSIGNEE'. Walk forward,
        splitting each non-empty line at 3+ whitespace runs and taking the
        rightmost fragment as the consignee column. Stop at the first line
        matching `<city>, <ST> <ZIP>` (the CSZ line). Lines between the
        name (row 0) and the CSZ line are address lines.
        """
        if self._consignee_cache is not None:
            return self._consignee_cache

        def all_failed(reason):
            return {
                k: (None, reason) for k in
                ('name', 'address_line_1', 'address_line_2',
                 'city', 'state', 'postcode')
            }

        layout_text = self.reader.pages[0].extract_text(extraction_mode='layout')
        lines = layout_text.splitlines()

        header_idx = None
        for i, line in enumerate(lines):
            if line.rstrip().endswith('CONSIGNEE'):
                header_idx = i
                break
        if header_idx is None:
            self._consignee_cache = all_failed(
                "no line ending in 'CONSIGNEE' found in layout text")
            return self._consignee_cache

        right_col = []
        csz_match = None
        for line in lines[header_idx + 1:]:
            if not line.strip():
                continue
            parts = re.split(r'\s{3,}', line.strip())
            if not parts or not parts[-1].strip():
                self._consignee_cache = all_failed(
                    f"could not extract right column from {line!r}")
                return self._consignee_cache
            col_text = parts[-1].strip()
            right_col.append(col_text)
            m = _CSZ_RE.match(col_text)
            if m:
                csz_match = m
                break
            if len(right_col) > 6:
                self._consignee_cache = all_failed(
                    "scanned >6 lines past CONSIGNEE without finding city/state/zip")
                return self._consignee_cache

        if csz_match is None:
            self._consignee_cache = all_failed(
                "no city/state/zip line found in consignee block")
            return self._consignee_cache
        if len(right_col) < 2:
            self._consignee_cache = all_failed(
                "consignee block has CSZ but no name line")
            return self._consignee_cache

        # right_col[0] = name; right_col[-1] = CSZ line; the rest = address lines
        name = right_col[0]
        addr_lines = right_col[1:-1]
        line_1 = addr_lines[0] if addr_lines else None
        line_2 = addr_lines[1] if len(addr_lines) > 1 else None

        self._consignee_cache = {
            'name': (name, None),
            'address_line_1': (
                line_1,
                None if line_1 else "no address line found between name and city/state/zip",
            ),
            'address_line_2': (line_2, None),  # legitimately None for short addresses
            'city':     (csz_match.group('city').strip(), None),
            'state':    (csz_match.group('state').upper(), None),
            'postcode': (csz_match.group('postcode'), None),
        }
        return self._consignee_cache

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
