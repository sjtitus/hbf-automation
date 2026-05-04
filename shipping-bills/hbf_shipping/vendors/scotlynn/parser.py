"""
PDF Parser for Scotlynn USA Division invoices.

Most fields come from page-1 plain text (label-anchored regex). The
two-column SHIPPER/CONSIGNEE block uses pypdf's
`extraction_mode='layout'` so the column gap is preserved and the right
column (consignee) and second-from-right column (shipper) can be split
out cleanly.

Page-2 BOL OCR is handled by `hbf_shipping.bol_ship_to` via the
Scotlynn BOL profile (currently identical to Badger's profile because
the carriers use the same standard short-form BOL).

Every _extract_* method returns (value, reason):
    - value  is the extracted value, or None on failure
    - reason is None on success, or a short human-readable string

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


class ScotlynnInvoiceParser:
    """Parses Scotlynn USA Division invoice PDFs."""

    def __init__(self, pdf_path):
        self.pdf_path = pdf_path
        self.reader = PdfReader(pdf_path)
        self.first_page_text = self.reader.pages[0].extract_text()
        self._consignee_cache = None

    def parse(self):
        """Extract invoice data. Returns (data, reasons) — dicts keyed by field name."""
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
        # The first 7-digit token in plain text is the invoice number
        # (it's repeated on the ORDER NUMBER line below — first match wins).
        match = re.search(r'\b(\d{7})\b', text)
        if match:
            return match.group(1), None
        return None, "no 7-digit invoice number found in page-1 text"

    def _extract_invoice_date(self, text):
        # The first MM/DD/YYYY in plain text is the invoice date.
        dates = re.findall(r'\d{2}/\d{2}/\d{4}', text)
        if dates:
            return datetime.strptime(dates[0], '%m/%d/%Y'), None
        return None, "no MM/DD/YYYY date found in text (invoice date is the 1st date)"

    def _extract_ship_date(self, text):
        # In plain text, the ship date appears glued to "HIGHREVA" (HBF's
        # BILL TO code, which is constant for our invoices). Fall back to
        # a 'SHIP DATE' label search if the code shifts in the future.
        match = re.search(r'(\d{2}/\d{2}/\d{4})HIGH', text)
        if match:
            return datetime.strptime(match.group(1), '%m/%d/%Y'), None
        match = re.search(r'SHIP\s*DATE[^\d]*?(\d{2}/\d{2}/\d{4})', text)
        if match:
            return datetime.strptime(match.group(1), '%m/%d/%Y'), None
        return None, "ship date not found (looked for date glued to HBF BILL TO code, then 'SHIP DATE' label)"

    def _extract_shipper(self, text):
        return self._consignee_block()['shipper_name']

    def _extract_consignee(self, text):
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
        """Parse the page-1 SHIPPER/CONSIGNEE two-column block.

        Strategy: anchor on the line ending in 'CONSIGNEE' (Scotlynn glues
        the literal label to the consignee code, so the line ends with
        '<code>CONSIGNEE'). Walk forward, splitting each non-empty line
        at 3+ whitespace runs; the rightmost fragment is the consignee
        column and the second-rightmost is the shipper column. Stop at
        the first line whose consignee fragment matches `<city>, <ST> <ZIP>`.

        Returns dict with keys: name, address_line_1, address_line_2,
        city, state, postcode, shipper_name. Each value is a (value, reason) pair.
        """
        if self._consignee_cache is not None:
            return self._consignee_cache

        def all_failed(reason):
            return {
                k: (None, reason) for k in
                ('name', 'address_line_1', 'address_line_2',
                 'city', 'state', 'postcode', 'shipper_name')
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

        consignee_col = []
        shipper_col = []
        csz_match = None
        for line in lines[header_idx + 1:]:
            if not line.strip():
                continue
            parts = re.split(r'\s{3,}', line.strip())
            if len(parts) < 2:
                continue
            consignee_text = parts[-1].strip()
            shipper_text = parts[-2].strip()
            consignee_col.append(consignee_text)
            shipper_col.append(shipper_text)
            m = _CSZ_RE.match(consignee_text)
            if m:
                csz_match = m
                break
            if len(consignee_col) > 6:
                self._consignee_cache = all_failed(
                    "scanned >6 lines past CONSIGNEE without finding city/state/zip")
                return self._consignee_cache

        if csz_match is None:
            self._consignee_cache = all_failed(
                "no city/state/zip line found in consignee block")
            return self._consignee_cache
        if len(consignee_col) < 2:
            self._consignee_cache = all_failed(
                "consignee block has CSZ but no name line")
            return self._consignee_cache

        name = consignee_col[0]
        addr_lines = consignee_col[1:-1]
        line_1 = addr_lines[0] if addr_lines else None
        line_2 = addr_lines[1] if len(addr_lines) > 1 else None

        # Shipper name lives on the same row as the consignee name (row 0
        # of the block). Second-rightmost split fragment is the shipper
        # column.
        shipper_name = shipper_col[0] if shipper_col else None

        self._consignee_cache = {
            'name': (name, None),
            'address_line_1': (
                line_1,
                None if line_1 else "no address line found between name and city/state/zip",
            ),
            'address_line_2': (line_2, None),
            'city':     (csz_match.group('city').strip(), None),
            'state':    (csz_match.group('state').upper(), None),
            'postcode': (csz_match.group('postcode'), None),
            'shipper_name': (
                shipper_name,
                None if shipper_name else "shipper name not found in CONSIGNEE block",
            ),
        }
        return self._consignee_cache

    def _extract_so_number(self, text):
        """Extract sales order number(s).

        Scotlynn invoices may carry a single SO ('SO-11131') or several
        slash-joined SOs on one BOL line ('SO-11132/11133/11134/11135').
        The 'S0-' digit-zero artifact also occurs (e.g. 'S0-11270') and
        is normalized to letter-O 'SO-' on output. The slash-joined
        suffix is preserved verbatim — the invoice carries one total
        amount with no per-SO breakdown, so the multi-SO list lands as
        a single string in the BillEntry's description/memo.
        """
        match = re.search(r'S[O0]-(\d+(?:/\d+)*)', text)
        if match:
            return f'SO-{match.group(1)}', None
        return None, "no match for 'S[O0]-<digits>(/<digits>)*' (slash-joined multi-SO supported)"

    def _extract_total_amount(self, text):
        # Total appears as '$X,XXX.XX USD' on the bottom data line. Line
        # items in the body lack the '$' prefix, so this is unambiguous.
        match = re.search(r'\$([\d,]+\.\d{2})\s*USD', text)
        if match:
            return float(match.group(1).replace(',', '')), None
        return None, "no '$X.XX USD' total found"

    def _extract_past_due_date(self, text):
        # Past-due date directly precedes the '$X.XX USD' total on the
        # bottom data line.
        match = re.search(r'(\d{2}/\d{2}/\d{4})\s+\$[\d,]+\.\d{2}\s*USD', text)
        if match:
            return datetime.strptime(match.group(1), '%m/%d/%Y'), None
        return None, "no past-due date found preceding '$X.XX USD' total"


def parse_invoice(pdf_path):
    """Parse a Scotlynn invoice PDF. Returns (data, reasons) — dicts keyed by field name."""
    return ScotlynnInvoiceParser(pdf_path).parse()


def extract_invoice_ship_to(pdf_path, invoice_data: dict):
    """Map already-parsed page-1 fields onto the canonical InvoiceExtraction shape."""
    from pathlib import Path
    from hbf_shipping.ship_to import extract_invoice_ship_to as _extract
    return _extract(
        Path(pdf_path),
        name=invoice_data.get('consignee'),
        line_1=invoice_data.get('consignee_address_line_1'),
        line_2=invoice_data.get('consignee_address_line_2'),
        city=invoice_data.get('consignee_city'),
        state=invoice_data.get('consignee_state'),
        postcode=invoice_data.get('consignee_postcode'),
    )
