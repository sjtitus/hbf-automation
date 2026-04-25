"""
OCR-based extraction of the SHIP TO customer from a Badger BOL PDF.

Background: Badger invoice PDFs have a text-extractable page 1 (invoice
header) and an image-only page 2+ (scanned Bill of Lading). The page-1
consignee is often abbreviated or ambiguous (e.g. just "FCI"), while
the page-2 SHIP TO block contains the full customer name used by HBF.

Public entry point: extract_ship_to_customer(pdf_path) -> (value, reason).
Returns the first SHIP TO text line (customer name) or None plus a short
human-readable reason on failure — same (value, reason) convention used
by pdf_parser._extract_*.

Requires tesseract on PATH (brew install tesseract) plus Python deps
pytesseract and pillow.
"""

from __future__ import annotations

import logging
import re
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps
from pypdf import PdfReader
import pytesseract


logger = logging.getLogger(__name__)


# BOL form landmarks. Tolerant to OCR noise (missing letters, O→0, etc.).
_SHIP_TO_RE   = re.compile(r'S\W{0,2}H\W{0,2}I\W{0,2}[BP]\W{0,2}T\W{0,2}O', re.IGNORECASE)
_SHIP_FROM_RE = re.compile(r'SHIP\W*FROM', re.IGNORECASE)
_HBF_RE       = re.compile(r'highland\b.*\bfarms?\b', re.IGNORECASE)
_SHIPPERS_RE  = re.compile(
    r'(Midwest Refrigerated|OLD WISCONSIN|Old Wisconsin|DairyFood|\bMRS\b|'
    r'Warehouse|Progress|Weeden|Sheboygan|Madison,\s*WI)',
    re.IGNORECASE,
)

# Filters: lines that look like things OTHER than a customer name.
_STREET_RE       = re.compile(r'^\s*\d{2,}\s+\w')
_PHONE_RE        = re.compile(r'\d{3}[\)\.\-\s]\s*\d{3}[\.\-\s]?\s*\d{4}')
_CITYSTATEZIP_RE = re.compile(r'[,\s]+[A-Za-z]{2,3}\s+\d{5}')
_ATTN_RE         = re.compile(r'^\s*at+[a-z]?[:;.]', re.IGNORECASE)
_NEG_TOKENS_RE   = re.compile(
    r'\b(BILL|LADING|BAR\s*CODE|SPACE|Carrier|Pickup|appt|Trailer|Serial|'
    r'SCAC|Email|Special|Instructions|FREIGHT|CHARGES|CONSIGNEE|Customer|'
    r'Order|ATTN|Hours|Call|receiving|dock|Delivery|Deliveries|Prepaid|'
    r'Collect)\b',
    re.IGNORECASE,
)

# Left-column crop: SHIP FROM / SHIP TO / Third Party Bill To all live in
# the left half of the BOL. Right side is Carrier/Trailer info, which we
# actively want to exclude to avoid column crossover in OCR output.
# Vertical extent is generous (0..1500) so we catch SHIP TO content for
# BOLs where the SHIP FROM block runs longer than usual; downstream code
# uses the SHIP TO label as an anchor so noise above/below is filtered out.
_LEFT_COLUMN_CROP = (0, 0, 1280, 1500)

# Tokens that mark the end of the SHIP TO block — anything below these
# is third-party bill-to, freight-charges, special instructions, etc.,
# which is not part of the consignee.
_SHIP_TO_END_RE = re.compile(
    r'(FREIGHT|THIRD\s*PARTY|BILL\s*TO|Highland\s*Beef|Special\s*Instructions)',
    re.IGNORECASE,
)


def extract_ship_to_customer(pdf_path: str | Path) -> tuple[str | None, str | None]:
    """Extract the first line of the SHIP TO block from page 2 of the BOL.

    Returns (customer_name, reason). customer_name is None on failure.
    reason is None on success, or a short explanatory string on failure.
    """
    pdf_path = Path(pdf_path)
    try:
        image = _load_bol_image(pdf_path)
    except _BolImageError as e:
        logger.debug("BOL image load failed: %s", e)
        return None, str(e)

    text = _ocr_left_column(image)
    lines = [_clean_line(ln) for ln in text.splitlines()]
    logger.debug("OCR raw text length=%d chars; %d cleaned lines", len(text), len(lines))
    for i, ln in enumerate(lines[:30]):
        logger.debug("ocr[%02d] %r", i, ln[:120])

    name = _pick_customer_line(lines)
    if name is None:
        logger.debug("no SHIP TO anchor resolved to a customer-name line")
        return None, "OCR ran but no SHIP TO anchor (label, shipper-block, or 'Highland Beef Farms') resolved to a customer-name line"
    logger.debug("ship_to customer -> %r", name)
    return name, None


def extract_ship_to_block(pdf_path: str | Path) -> tuple[list[str] | None, str | None]:
    """Return all cleaned non-empty OCR lines from the wide left-column crop.

    No SHIP TO localization is attempted here — the caller (parser's hybrid
    consignee logic) does a token-overlap check against the page-1 address
    to decide whether the OCR is "about the same place" as page-1. That's
    more robust than guessing where the SHIP TO block starts/ends in noisy
    OCR output.

    Returns (lines, reason); lines is None on image-load failure.
    """
    pdf_path = Path(pdf_path)
    try:
        image = _load_bol_image(pdf_path)
    except _BolImageError as e:
        logger.debug("BOL image load failed: %s", e)
        return None, str(e)

    text = _ocr_left_column(image)
    lines = [_clean_line(ln) for ln in text.splitlines() if _clean_line(ln)]
    logger.debug("ship_to_block: %d cleaned lines from full crop", len(lines))
    return lines, None


class _BolImageError(Exception):
    """Raised when we cannot obtain a BOL image from the PDF."""


def _load_bol_image(pdf_path: Path) -> Image.Image:
    """Extract the BOL bitmap from page 2 of the PDF.

    Badger PDFs embed the BOL as a single image XObject on page 2.
    If page 2 is missing, has no image, or Pillow can't decode it,
    raise _BolImageError with a diagnostic.
    """
    try:
        reader = PdfReader(str(pdf_path))
    except Exception as e:
        raise _BolImageError(f"could not open PDF: {e}") from e

    if len(reader.pages) < 2:
        raise _BolImageError(f"PDF has {len(reader.pages)} page(s); need page 2 for BOL")

    page = reader.pages[1]
    try:
        images = list(page.images)
    except Exception as e:
        raise _BolImageError(f"pypdf could not enumerate page-2 images: {e}") from e

    if not images:
        raise _BolImageError("page 2 has no embedded images (BOL expected as a bitmap)")

    try:
        return Image.open(BytesIO(images[0].data))
    except Exception as e:
        raise _BolImageError(f"Pillow could not decode page-2 image: {e}") from e


def _ocr_left_column(image: Image.Image) -> str:
    """Crop the SHIP TO column, upscale + autocontrast, OCR with PSM 6."""
    crop = image.crop(_LEFT_COLUMN_CROP)
    crop = crop.resize((crop.width * 2, crop.height * 2), Image.LANCZOS)
    crop = ImageOps.autocontrast(crop.convert('L'))
    return pytesseract.image_to_string(crop, config='--psm 6')


def _clean_line(line: str) -> str:
    """Strip form-chrome: column-separator '|' crossover, stray punctuation."""
    s = line
    if '|' in s:
        parts = [p.strip() for p in s.split('|') if p.strip()]
        s = parts[0] if parts else ''
    return s.strip().strip('_').strip()


def _looks_like_company(line: str) -> bool:
    """Filter: is this a plausible customer/company name line?"""
    s = line.strip()
    if not s:
        return False
    if len(re.findall(r'[A-Za-z]', s)) < 3:
        return False
    # Real customer names are short — 1 to 5 tokens. Long sentences with
    # many tokens are almost always OCR garbage (mangled label rows or
    # special-instructions blurbs); reject them outright.
    tokens = s.split()
    if len(tokens) > 6:
        return False
    # Reject lines with too many short fragments (≤2 chars) — typical of
    # OCR noise like "SUE SARS Ae oT SED SHIR TO eS".
    if sum(1 for t in tokens if len(t) <= 2) > 1:
        return False
    if _STREET_RE.match(s):         return False
    if _PHONE_RE.search(s):         return False
    if _CITYSTATEZIP_RE.search(s):  return False
    if _ATTN_RE.search(s):          return False
    if _SHIP_TO_RE.search(s):       return False
    if _SHIP_FROM_RE.search(s):     return False
    if _HBF_RE.search(s):           return False
    if _SHIPPERS_RE.search(s):      return False
    if _NEG_TOKENS_RE.search(s):    return False
    return True


def _pick_customer_line(lines: list[str]) -> str | None:
    """Anchor-heuristic: pick the customer line from cleaned OCR lines.

    Tries three anchors in order:
      1. SHIP TO label  → next company-looking line
      2. Shipper block  → first company-looking line after shipper's phone/appt
      3. HBF (Third Party Bill To) → walk backward to previous company line
    """
    # A. SHIP TO label
    for i, ln in enumerate(lines):
        if _SHIP_TO_RE.search(ln) and not _SHIP_FROM_RE.search(ln):
            logger.debug("anchor=ship_to_label at line %d: %r", i, ln[:80])
            for j in range(i + 1, min(i + 6, len(lines))):
                if _looks_like_company(lines[j]):
                    return lines[j]

    # B. Walk forward past shipper block
    shipper_i = next((i for i, ln in enumerate(lines) if _SHIPPERS_RE.search(ln)), None)
    if shipper_i is not None:
        logger.debug("anchor=shipper_block at line %d: %r", shipper_i, lines[shipper_i][:80])
        end = None
        for k in range(shipper_i, min(shipper_i + 10, len(lines))):
            if _PHONE_RE.search(lines[k]) or re.search(r'\bappt\b|Pickup', lines[k], re.IGNORECASE):
                end = k
                break
        if end is not None:
            for j in range(end + 1, min(end + 8, len(lines))):
                if _looks_like_company(lines[j]):
                    return lines[j]

    # C. Walk backward from Highland Beef Farms
    hbf_idx = next((i for i, ln in enumerate(lines) if _HBF_RE.search(ln)), None)
    if hbf_idx is not None:
        logger.debug("anchor=hbf_backward at line %d: %r", hbf_idx, lines[hbf_idx][:80])
        for j in range(hbf_idx - 1, -1, -1):
            if _looks_like_company(lines[j]):
                return lines[j]

    return None


if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        print("usage: python3 bol_ocr.py <badger-invoice.pdf> [<more.pdf> ...]")
        sys.exit(2)

    for p in sys.argv[1:]:
        name, reason = extract_ship_to_customer(p)
        if name is not None:
            print(f"{p}: {name}")
        else:
            print(f"{p}: <None> — {reason}")
