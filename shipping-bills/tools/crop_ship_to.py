#!/usr/bin/env python3
"""
Crop the SHIP TO box out of page 2 of an invoice PDF.

The SHIP TO block on a Bill of Lading is bounded:
    top    -> the "SHIP TO" label
    bottom -> the "THIRD PARTY FREIGHT CHARGES BILL TO" label
    left   -> page left edge
    right  -> page midline (the box occupies the left half)

This tool finds those two label anchors via a single full-page OCR pass
(pytesseract.image_to_data, word-level bounding boxes), applies an optional
small deskew, and writes one PNG per PDF into --out-dir. The output dir is
wiped at the start of every run so eyeballing is uncluttered.

No address extraction, no scoring -- just the crop.

Usage:
    python tools/crop_ship_to.py tests/fixtures/badger/*.pdf
    python tools/crop_ship_to.py --no-deskew --out-dir ship_to_crops *.pdf
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import fitz
import numpy as np
import pytesseract
from rapidfuzz import fuzz


TOP_ANCHOR = "SHIP TO"
BOTTOM_ANCHOR = "THIRD PARTY FREIGHT CHARGES BILL TO"
TOP_DECOY = "SHIP FROM"

BOTTOM_PAD_FRAC = 0.005

# Skip the anchor's own OCR line and lines from the SHIP TO header band when
# scanning for the first address line. Strict text match is more reliable
# than y-distance: dual-PSM duplicates of the anchor can land 10-20px apart,
# and on some BOLs the first address line is only 12px below the anchor (so
# a y-gap filter would either skip the address or include a duplicate).
_HEADER_DUP_RE = re.compile(
    r"\b(?:ship\s*to|smp\s*to|shp\s*to|shb\s*to|ashp\s*to|shir\s*to|sure\s*to"
    r"|carrier\s*name|carver\s*name|carter\s*name|carrer\s*name|crcher\s*minin"
    r"|bdgw|bogw|hogw|hbgw|badger\s*state|had\s*get)\b",
    re.IGNORECASE,
)

# Form-label anchors used by the precise y1/y2 logic. OCR'd labels can
# drift letters: 'carrier' -> 'carter|carver|carrer|carier', 'trailer' ->
# 'traller|traker|tralier', 'scac' -> 'scag'. The patterns below tolerate
# those slips while staying specific enough not to false-match address
# text. `_BADGER_IN_BOX_RE` is an additional same-line constraint for the
# Carrier Name match -- it scopes the hit to the SHIP TO box's right
# column rather than the page-header "Badger State Western" carrier.
_CARRIER_NAME_RE = re.compile(
    # Carrier prefix variants observed in OCR: 'rier'/'ver'/'ter'/'rer'/
    # 'ier'/'tiot' (last seen in 0065555 'Cartiot Nain'). Suffix variants:
    # 'name', 'nane', 'mame' (M for N), 'nain' (broken last char).
    r"car(?:rier|ver|ter|rer|ier|tiot)\s*[mn]a[a-z]{1,3}",
    re.IGNORECASE,
)
_BADGER_IN_BOX_RE = re.compile(
    # BDGW / BOGW / HOGW (B->H drift seen in 0065555); same for BADGER.
    r"\b(?:bdgw|bogw|hogw|hbgw|badger\s*state)\b",
    re.IGNORECASE,
)
_TRAILER_NUMBER_RE = re.compile(
    # Trailer / Traller / Traker / Tralier / Tealer / Tearer / 'raker'
    # (leading T dropped in 0065935 PSM-6 pass).
    r"\b(?:t?railer|traller|traker|tralier|tealer|tearer|raker)\s*num",
    re.IGNORECASE,
)
_SCAC_RE = re.compile(
    # Variants observed across the dataset: SCAC, SCAG (G for C), seac
    # (E for C), SCA: / SCA. (last C dropped). Trailing punctuation
    # required to avoid hitting random sca-prefixed words.
    r"\b(?:scac|scag|seac|sca)\s*[:.]",
    re.IGNORECASE,
)

# Recognizes OCR lines that are real address content. Used to filter out
# OCR noise lines (e.g., '[aa cay mE sca', stamps from the THIRD PARTY
# band) when locating the last address line for bottom-crop fallback.
_ADDRESSY_RE = re.compile(
    r"\(?\d{3}\)?[-.\s]+\d{3}[-.\s]+\d{4}"     # phone (parens optional)
    r"|[A-Za-z][A-Za-z .'\-]+,\s*[A-Z]{2}\s+\d{5}"  # City, ST 12345
    r"|^\s*\d+\s+[A-Za-z]"                     # street: 13777 Air ...
    r"|\bAttn[:\s]"                            # Attn:
    r"|(?:\b[A-Z][A-Za-z]+\s+){2,}[A-Z][A-Za-z]"   # 3+ Title-Case words
)

MIN_HEADER_GAP = 5
SMALL_PAD = 8
DEFAULT_HEADER_PX = 60

MAX_DESKEW_DEGREES = 5.0


@dataclass
class CropResult:
    pdf: Path
    out_path: Optional[Path]
    deskew_angle: float
    top_score: Optional[int]
    bottom_score: Optional[int]
    note: str = ""


def render_pdf_page(pdf_path: Path, page_index: int = 1, dpi: int = 300) -> np.ndarray:
    doc = fitz.open(pdf_path)
    if len(doc) <= page_index:
        raise ValueError(f"{pdf_path} has only {len(doc)} page(s); page 2 not found")
    page = doc[page_index]
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


def crop_to_document(img: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark = th < 245
    mask = dark.astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    pts = cv2.findNonZero(mask)
    if pts is None:
        return img
    x, y, w, h = cv2.boundingRect(pts)
    pad_x, pad_y = int(0.015 * img.shape[1]), int(0.015 * img.shape[0])
    x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
    x2 = min(img.shape[1], x + w + pad_x)
    y2 = min(img.shape[0], y + h + pad_y)
    out = img[y1:y2, x1:x2]
    return out if out.size else img


def find_band_top_after_address(means: np.ndarray, y_start: int, y_end: int,
                                y_after: int = 0,
                                min_gap_rows: int = 12,
                                white_thresh: float = 243.0) -> Optional[int]:
    """Find the THIRD PARTY band's top edge by walking through `means`
    from `y_start` to `y_end` and returning the y of the first non-white
    row after the FIRST contiguous white run of at least `min_gap_rows`
    that starts at y >= y_after.

    Picking the first significant gap (not the largest) keeps us at the
    band immediately below the address. The largest gap could be further
    down the page (between the THIRD PARTY band and the next box), and
    using "largest" pulls the crop past the band into the next section."""
    cur_start: Optional[int] = None
    for y in range(max(0, y_start), min(len(means), y_end)):
        if means[y] >= white_thresh:
            if cur_start is None:
                cur_start = y
        else:
            if cur_start is not None and cur_start >= y_after:
                run_size = y - cur_start
                if run_size >= min_gap_rows:
                    return y
            cur_start = None
    return None


def first_text_row_below(means: np.ndarray, start_y: int, max_y: int,
                         white_thresh: float = 243.0,
                         dark_thresh: float = 240.0) -> Optional[int]:
    """Walk down through `means` from start_y. Once we cross into white
    (mean >= white_thresh), return the next y where mean < dark_thresh --
    that's the first text row below the gap. Returns None if no such
    transition is found within [start_y, max_y)."""
    state = "seek_gap"
    for y in range(max(0, start_y), min(len(means), max_y)):
        m = means[y]
        if state == "seek_gap":
            if m >= white_thresh:
                state = "in_gap"
        else:  # in_gap
            if m < dark_thresh:
                return y
    return None


def last_text_row_above(means: np.ndarray, start_y: int, min_y: int,
                        white_thresh: float = 243.0,
                        dark_thresh: float = 240.0) -> Optional[int]:
    """Walk up through `means` from start_y. Once we cross into white,
    return the next y where mean < dark_thresh -- that's the last text
    row above the gap. Returns None if no such transition is found within
    (min_y, start_y]."""
    state = "seek_gap"
    for y in range(min(len(means) - 1, start_y), max(0, min_y), -1):
        m = means[y]
        if state == "seek_gap":
            if m >= white_thresh:
                state = "in_gap"
        else:
            if m < dark_thresh:
                return y
    return None


def find_address_top_by_density(img: np.ndarray, anchor_y: int, max_y: int,
                                white_thresh: float = 243.0,
                                dark_thresh: float = 240.0) -> Optional[int]:
    """Locate the start of address content below the SHIP TO band by row-mean
    intensity. The band area has dark pixels (mean ~190-235); the gap between
    band and address is near-white (>= 243); the first address line drops
    back below 240 as text re-introduces dark pixels.

    Walks down from `anchor_y`, tracks the run of white-ish rows after the
    band, and returns the row just past the last white row. More reliable
    than OCR y_tops on noisy scans where tesseract groups header and address
    onto a single OCR line and emits the wrong y."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    h, w = gray.shape
    region_means = gray[:, :w // 2].mean(axis=1)
    state = "start"
    last_white = None
    for y in range(anchor_y, min(max_y, h)):
        m = region_means[y]
        if state == "start":
            if m >= white_thresh:
                state = "in_white"
                last_white = y
        else:  # in_white
            if m >= white_thresh:
                last_white = y
            elif m < dark_thresh:
                return (last_white + 1) if last_white is not None else y
    return None


def find_address_bottom_by_density(img: np.ndarray, max_y: int, min_y: int,
                                   white_thresh: float = 243.0,
                                   dark_thresh: float = 240.0) -> Optional[int]:
    """Mirror of `find_address_top_by_density`: walks UPWARD from `max_y`
    looking for the white gap above the THIRD PARTY band. Returns the row
    just before that gap starts (the last text row of the address)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    h, w = gray.shape
    region_means = gray[:, :w // 2].mean(axis=1)
    state = "start"
    last_white = None
    for y in range(min(max_y, h) - 1, max(0, min_y), -1):
        m = region_means[y]
        if state == "start":
            if m >= white_thresh:
                state = "in_white"
                last_white = y
        else:
            if m >= white_thresh:
                last_white = y
            elif m < dark_thresh:
                # last_white is the topmost row of the gap (smallest y).
                # Return it as y2 so img[y1:y2] excludes the gap and includes
                # the last text row at last_white-1.
                return last_white if last_white is not None else y
    return None


def detect_horizontal_lines(img: np.ndarray, min_length_frac: float = 0.25,
                            x_window: tuple[float, float] = (0.0, 0.5)) -> list[int]:
    """Return y-positions of horizontal form lines that span at least
    `min_length_frac` of the page width. Restricted to the x_window slice
    of the page (default left half, where the SHIP TO box lives).

    Approach: invert+Otsu the page, run horizontal-line morphological open
    with a wide kernel, then find rows where line pixels occupy >=
    min_length_frac of x_window. Consecutive rows within 6 px are coalesced
    into one line; the median y of each cluster is returned.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    h, w = gray.shape
    x1, x2 = int(x_window[0] * w), int(x_window[1] * w)
    region = gray[:, x1:x2]
    _, bw = cv2.threshold(region, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    min_w = max(20, int(min_length_frac * (x2 - x1)))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (min_w, 1))
    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN, kernel)
    row_pixel_counts = (horiz > 0).sum(axis=1)
    line_rows = np.where(row_pixel_counts >= min_w)[0]
    if len(line_rows) == 0:
        return []
    groups: list[int] = []
    current = [int(line_rows[0])]
    for y in line_rows[1:]:
        if int(y) - current[-1] <= 6:
            current.append(int(y))
        else:
            groups.append(int(np.median(current)))
            current = [int(y)]
    groups.append(int(np.median(current)))
    return groups


def estimate_skew_angle(gray: np.ndarray) -> float:
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges, rho=1, theta=np.pi / 180, threshold=120,
        minLineLength=max(80, gray.shape[1] // 5), maxLineGap=20,
    )
    if lines is None:
        return 0.0
    angles = []
    for x1, y1, x2, y2 in lines[:, 0, :]:
        if x2 - x1 == 0:
            continue
        a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if -15 <= a <= 15:
            angles.append(a)
    return float(np.median(angles)) if angles else 0.0


def rotate_bound(img: np.ndarray, angle: float) -> np.ndarray:
    if abs(angle) < 0.25:
        return img
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(M[0, 0]), abs(M[0, 1])
    new_w, new_h = int(h * sin + w * cos), int(h * cos + w * sin)
    M[0, 2] += new_w / 2 - w / 2
    M[1, 2] += new_h / 2 - h / 2
    return cv2.warpAffine(img, M, (new_w, new_h), borderValue=(255, 255, 255))


def _binarize(img: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, otsu = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return otsu


def _ocr_pass(image: np.ndarray, psm: int) -> list[dict]:
    data = pytesseract.image_to_data(
        image, output_type=pytesseract.Output.DICT,
        config=f"--oem 3 --psm {psm} -c preserve_interword_spaces=1",
    )
    lines: dict[tuple, dict] = {}
    for i in range(len(data["text"])):
        word = data["text"][i]
        if not word or not word.strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        top, height = data["top"][i], data["height"][i]
        left, width = data["left"][i], data["width"][i]
        if key not in lines:
            lines[key] = {"text": word, "y_top": top, "y_bot": top + height,
                          "x_left": left, "x_right": left + width}
        else:
            ln = lines[key]
            ln["text"] += " " + word
            ln["y_top"] = min(ln["y_top"], top)
            ln["y_bot"] = max(ln["y_bot"], top + height)
            ln["x_left"] = min(ln["x_left"], left)
            ln["x_right"] = max(ln["x_right"], left + width)
    return list(lines.values())


def ocr_lines(img: np.ndarray) -> list[dict]:
    """Multi-pass OCR: Otsu binarize, then PSM 3 + PSM 6 (BOL labels surface
    under different page-segmentation assumptions). Returns merged line list
    sorted top-to-bottom."""
    bin_img = _binarize(img)
    merged = _ocr_pass(bin_img, psm=3) + _ocr_pass(bin_img, psm=6)
    return sorted(merged, key=lambda d: d["y_top"])


_WORD_RE = re.compile(r"[a-z0-9]+")


def _line_tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


_SHIPTO_VARIANTS = re.compile(
    # SHIP/SMP/SHP/SHB/SMEP/ASHP/SLIP, optional space, TO/T0/TU
    r"\b[a-z]?(?:sh|sm|sl)[a-z]{0,2}p?\s*t[o0u]\b",
    re.IGNORECASE,
)

_SHIPFROM_VARIANTS = re.compile(
    # SHIP/SHER/SHIR/SRIB/SMP/etc + 'FROM' (or close)
    r"\b[a-z]?(?:sh|sm|sl|sr)[a-z]{0,3}\s*(?:from|prom|rom|fram)\b",
    re.IGNORECASE,
)


def _has_fuzzy_token(toks: set[str], target: str, threshold: int = 80) -> bool:
    return target in toks or any(fuzz.ratio(target, tk) >= threshold for tk in toks)


def find_top_anchor(lines: list[dict]) -> Optional[dict]:
    """Top anchor: the printed 'SHIP TO' label, which gets mangled badly
    (SMP TO, SHB TO, ASHP TO, sito, sure To, SHIR TO, ...).

    Tiered search; first match wins:
      1. SHIP TO-ish regex against the line text          -> score 100
      2. Fuzzy 'carrier' + 'name' tokens (carter, Carver) -> score 85
      3. 'badger state' or 'bdgw' tokens (the carrier
         identity, always on the SHIP TO header row)      -> score 70

    'SHIP FROM' and 'BILL TO' are always rejected as decoys. The carrier
    identity also appears in the page header ("Badger State Western,
    Inc."), so once we know where SHIP FROM is, we require all candidates
    to sit at least 100px below it."""
    ship_from_y = None
    for ln in lines:
        clean = re.sub(r"\s+", " ", ln["text"]).strip()
        if _SHIPFROM_VARIANTS.search(clean):
            if ship_from_y is None or ln["y_top"] < ship_from_y:
                ship_from_y = ln["y_top"]
    min_anchor_y = (ship_from_y + 100) if ship_from_y is not None else 0

    candidates = []
    for ln in lines:
        if ln["y_top"] < min_anchor_y:
            continue
        clean = re.sub(r"\s+", " ", ln["text"]).strip()
        low = clean.lower()
        if "ship from" in low or "smp from" in low or "bill to" in low:
            continue
        toks = _line_tokens(low)
        score = 0
        if _SHIPTO_VARIANTS.search(clean):
            score = 100
        elif _has_fuzzy_token(toks, "carrier", 75) and _has_fuzzy_token(toks, "name", 80):
            score = 85
        elif "badger" in toks and ("state" in toks or "bdgw" in toks or "bogw" in toks):
            score = 70
        elif "bdgw" in toks or "bogw" in toks:
            score = 65
        if score:
            candidates.append((score, ln))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[1]["y_top"], -c[0]))
    score, best = candidates[0]
    return {**best, "score": score}


def find_bottom_anchor(lines: list[dict]) -> Optional[dict]:
    """Bottom anchor: line with at least 3 of the 6 target tokens
    {third, party, freight, charges, bill, to}. Allow per-token fuzz (ratio
    >= 80) so OCR drift like 'biel' for 'bill' or '@reight' for 'freight'
    still counts."""
    targets = ("third", "party", "freight", "charges", "bill", "to")
    best, best_count = None, 2  # need >= 3 hits
    for ln in lines:
        clean = re.sub(r"\s+", " ", ln["text"]).strip().lower()
        toks = _line_tokens(clean)
        hits = sum(
            1 for t in targets
            if t in toks or any(fuzz.ratio(t, tk) >= 80 for tk in toks)
        )
        if hits > best_count:
            best, best_count = ln, hits
    if best is None:
        return None
    return {**best, "score": int(best_count * 100 // len(targets))}


def crop_ship_to(pdf_path: Path, out_dir: Path, deskew: bool, dpi: int) -> CropResult:
    img = render_pdf_page(pdf_path, page_index=1, dpi=dpi)
    img = crop_to_document(img)

    angle = 0.0
    if deskew:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        raw = estimate_skew_angle(gray)
        angle = raw if abs(raw) <= MAX_DESKEW_DEGREES else 0.0
        if abs(angle) >= 0.25:
            img = rotate_bound(img, angle)
            img = crop_to_document(img)

    h, w = img.shape[:2]
    lines = ocr_lines(img)

    top = find_top_anchor(lines)
    bot = find_bottom_anchor(lines)

    notes = [f"deskew={angle:.2f}deg"]
    if top is None:
        # Last-resort: anchor off SHIP FROM (which OCRs more reliably as a
        # longer phrase) and offset down by ~10% of page height -- empirically
        # the SHIP TO header sits ~0.10*h below SHIP FROM across all BOLs.
        for ln in lines:
            clean = re.sub(r"\s+", " ", ln["text"]).strip()
            if _SHIPFROM_VARIANTS.search(clean):
                # 0.12 lands inside the SHIP TO band on average (the band
                # itself, not the gap above it). 0.10 was leaving the
                # synthetic anchor in the gap *above* the band, which made
                # density-gap detection lock onto the wrong gap.
                synth_y = ln["y_top"] + int(0.12 * h)
                top = {"y_top": synth_y, "y_bot": synth_y, "score": 50}
                notes.append("top anchor: SHIP FROM offset fallback")
                break
    if top is None:
        notes.append("top 'SHIP TO' not found")
    bot_via_fallback = False
    if bot is None and top is not None:
        # SHIP TO box on the BOL is empirically ~0.13-0.14 of page height tall.
        # When the bottom label OCRs unrecoverably, fall back to a geometric
        # offset so we still get a usable (slightly-tall) crop.
        bot_via_fallback = True
        synth_y = min(h, top["y_top"] + int(0.15 * h))
        bot = {"y_top": synth_y, "y_bot": synth_y, "score": 0}
        notes.append("bottom anchor: geometric fallback")
    elif bot is None:
        notes.append("bottom 'THIRD PARTY...' not found")

    if top is None or bot is None:
        return CropResult(pdf_path, None, angle,
                          top["score"] if top else None,
                          bot["score"] if bot else None,
                          "; ".join(notes))

    anchor_top_y = top["y_top"]
    anchor_bot_y = bot["y_top"]

    # Row-mean intensity profile of the left half (where the SHIP TO column
    # lives). Used by both density walks below.
    gray_full = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    left_means = gray_full[:, : w // 2].mean(axis=1)

    # Form-label OCR positions (Carrier Name + BADGER STATE same line for
    # the SHIP TO box header; Trailer Number for the first address row;
    # SCAC: for the THIRD PARTY band header).
    y_carrier = y_trailer = y_scac = None
    for ln in lines:
        txt = ln["text"]
        if y_carrier is None and _CARRIER_NAME_RE.search(txt) \
                and _BADGER_IN_BOX_RE.search(txt):
            y_carrier = ln["y_top"]
        if y_trailer is None and _TRAILER_NUMBER_RE.search(txt):
            y_trailer = ln["y_top"]
        if y_scac is None and _SCAC_RE.search(txt):
            y_scac = ln["y_top"]

    # y1 fallback ladder (most precise first):
    #   (1) Both Carrier+Trailer found: midpoint + density walk down.
    #   (2) Only Carrier found: walk down from Carrier (it's inside the
    #       band), through the gap, to first text.
    #   (3) Only Trailer found: Trailer is on the first address row, so
    #       y1 = trailer_y - SMALL_PAD.
    #   (4) Neither: first non-dup OCR line below the band anchor.
    #   (5) No OCR line either: fixed offset.
    # Density walk returns the first row where mean drops sharply (< 240),
    # which is typically 1-3 rows BELOW the actual letter tops. Subtract a
    # small pad so the first address line isn't clipped at the top.
    Y1_LIFT = 10

    y1: Optional[int] = None
    if y_carrier is not None and y_trailer is not None and y_trailer > y_carrier:
        midpoint = (y_carrier + y_trailer) // 2
        y1_dens = first_text_row_below(left_means, midpoint, midpoint + 200)
        if y1_dens is not None:
            y1 = max(0, y1_dens - Y1_LIFT)
            notes.append("y1 via Carrier/Trailer midpoint")
    if y1 is None and y_carrier is not None:
        y1_dens = first_text_row_below(left_means, y_carrier, y_carrier + 250)
        if y1_dens is not None:
            y1 = max(0, y1_dens - Y1_LIFT)
            notes.append("y1 via Carrier + density walk")
    if y1 is None and y_trailer is not None:
        y1 = max(0, y_trailer - SMALL_PAD)
        notes.append("y1 via Trailer y_top")
    if y1 is None:
        # Last-resort: first non-duplicate OCR line below the band anchor.
        next_ocr_y = None
        for ln in lines:
            if ln["y_top"] <= anchor_top_y + MIN_HEADER_GAP:
                continue
            if _HEADER_DUP_RE.search(ln["text"]):
                continue
            if next_ocr_y is None or ln["y_top"] < next_ocr_y:
                next_ocr_y = ln["y_top"]
        if next_ocr_y is not None:
            y1 = max(0, next_ocr_y - SMALL_PAD)
            notes.append("y1 via OCR fallback")
        else:
            y1 = max(0, anchor_top_y + DEFAULT_HEADER_PX)
            notes.append("y1 via fixed-offset fallback")

    # y2: SCAC y_top + density walk up. The walk returns the last text
    # row of the address; +1 to include it in the crop slice.
    # Symmetric pad below last text row: density walk returns the last
    # row where mean is text-y, but descenders (g, p, y) extend below.
    Y2_DROP = 6

    y2: Optional[int] = None
    if y_scac is not None and y_scac > y1:
        y2_dens = last_text_row_above(left_means, y_scac, max(y1, y_scac - 250))
        if y2_dens is not None:
            y2 = min(h, y2_dens + Y2_DROP)
            notes.append("y2 via SCAC + density walk")
    if y2 is None and bot_via_fallback:
        # No SCAC and no real THIRD PARTY label: locate the gap above the
        # band as the largest white run *after the last OCR address line*.
        # Use y_top (more reliable than y_bot, which OCR may inflate when
        # it groups an address line with adjacent band noise). Bound the
        # search to ~0.12*h below the top anchor -- the SHIP TO box height
        # is empirically <0.13*h, so anything below that is THIRD PARTY
        # content (often mangled past my noise regex).
        OCR_LINE_HEIGHT = 22
        max_addr_y_top = anchor_top_y + int(0.12 * h)
        last_addr_y_top = y1
        for ln in lines:
            if ln["y_top"] < y1 or ln["y_top"] > max_addr_y_top:
                continue
            if re.search(r"\b(?:third|party|freight|charges|scac|highland\s*beef)\b",
                         ln["text"], re.IGNORECASE):
                continue
            if not _ADDRESSY_RE.search(ln["text"]):
                continue
            if ln["y_top"] > last_addr_y_top:
                last_addr_y_top = ln["y_top"]
        last_addr_y_end = last_addr_y_top + OCR_LINE_HEIGHT
        band_top = find_band_top_after_address(left_means, y1,
                                               min(h, anchor_bot_y + 100),
                                               y_after=last_addr_y_end)
        if band_top is not None and band_top > y1:
            y2 = band_top
            notes.append("y2 via largest-gap-after-address")
    if y2 is None:
        # Final fallback: pad above the band anchor.
        y2 = min(h, anchor_bot_y - int(BOTTOM_PAD_FRAC * h))
        notes.append("y2 via anchor pad fallback")

    x1, x2 = 0, w // 2

    if y2 <= y1:
        notes.append(f"anchors out of order y1={y1} y2={y2}")
        return CropResult(pdf_path, None, angle, top["score"], bot["score"],
                          "; ".join(notes))

    crop = img[y1:y2, x1:x2]
    out_path = out_dir / f"{pdf_path.stem}_shipto.png"
    cv2.imwrite(str(out_path), crop)
    notes.append(f"top_score={top['score']} bot_score={bot['score']}")
    return CropResult(pdf_path, out_path, angle, top["score"], bot["score"],
                      "; ".join(notes))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--out-dir", type=Path, default=Path("ship_to_crops"))
    ap.add_argument("--no-deskew", action="store_true")
    ap.add_argument("--dpi", type=int, default=300)
    args = ap.parse_args(argv)

    if args.out_dir.exists():
        shutil.rmtree(args.out_dir)
    args.out_dir.mkdir(parents=True)

    ok = 0
    for pdf in args.pdfs:
        try:
            res = crop_ship_to(pdf, args.out_dir, deskew=not args.no_deskew, dpi=args.dpi)
        except Exception as e:
            print(f"{pdf}: ERROR {e}")
            continue
        if res.out_path:
            ok += 1
            print(f"{pdf}: OK -> {res.out_path}  [{res.note}]")
        else:
            print(f"{pdf}: FAIL  [{res.note}]")

    print(f"\n{ok}/{len(args.pdfs)} crops written to {args.out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
