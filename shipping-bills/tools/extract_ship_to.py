#!/usr/bin/env python3
"""
Extract the SHIP TO address from page 2 of invoice/BOL PDFs.

Approach:
  1. Render page 2 at high DPI.
  2. Find/crop the document/form area.
  3. Generate plausible SHIP TO candidate crops in the upper-left BOL area.
  4. OCR each crop using multiple preprocessing variants.
  5. Score results using address-like heuristics.
  6. Return the best candidate and optionally save debug crops.

Install:
  pip install pymupdf opencv-python pytesseract pillow
  # plus the system tesseract binary:
  # macOS:   brew install tesseract
  # Ubuntu:  sudo apt-get install tesseract-ocr

Usage:
  python extract_ship_to.py Invoice0065555.pdf --debug-dir debug
  python extract_ship_to.py *.pdf --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

import cv2
import fitz  # PyMuPDF
import numpy as np
import pytesseract


CITY_STATE_ZIP_RE = re.compile(
    r"\b[A-Za-z][A-Za-z .'-]+,?\s+(?:A[LKSZR]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEHINOPST]|N[CDEHJMVY]|O[HKR]|P[A]|RI|S[CD]|T[NX]|UT|V[AIT]|W[AIVY])\s+\d{5}(?:-\d{4})?\b",
    re.IGNORECASE,
)
PHONE_RE = re.compile(r"\b\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
STREET_HINT_RE = re.compile(
    r"\b(road|rd\.?|street|st\.?|avenue|ave\.?|expressway|expy|lane|ln\.?|drive|dr\.?|blvd|boulevard|highway|hwy|wilmot|air|po box|p\.o\.)\b",
    re.IGNORECASE,
)
BAD_HEADER_RE = re.compile(
    r"\b(ship\s*to|ship\s*from|third\s*party|freight|charges|bill\s*of\s*lading|carrier|trailer|serial|scac|barcode|bar\s*code|date)\b",
    re.IGNORECASE,
)


@dataclass
class ExtractionResult:
    pdf: str
    address: str
    confidence: float
    crop_box: tuple[int, int, int, int]  # x1, y1, x2, y2 in normalized image pixels
    raw_text: str
    warning: Optional[str] = None


def render_pdf_page(pdf_path: Path, page_index: int = 1, dpi: int = 350) -> np.ndarray:
    doc = fitz.open(pdf_path)
    if len(doc) <= page_index:
        raise ValueError(f"{pdf_path} has only {len(doc)} page(s); page 2 not found")
    page = doc[page_index]
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    # PyMuPDF gives RGB; OpenCV expects BGR/gray. Convert to BGR for consistency.
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


def crop_to_document(img: np.ndarray) -> np.ndarray:
    """Crop away wide white borders/noise using dark-pixel bounding box."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark = th < 245

    # Ignore tiny specks by dilating dark pixels into coherent regions.
    mask = dark.astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 25))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

    pts = cv2.findNonZero(mask)
    if pts is None:
        return img
    x, y, w, h = cv2.boundingRect(pts)

    # Add small margin, but keep in-bounds.
    pad_x, pad_y = int(0.015 * img.shape[1]), int(0.015 * img.shape[0])
    x1 = max(0, x - pad_x)
    y1 = max(0, y - pad_y)
    x2 = min(img.shape[1], x + w + pad_x)
    y2 = min(img.shape[0], y + h + pad_y)

    cropped = img[y1:y2, x1:x2]
    return cropped if cropped.size else img


def estimate_skew_angle(gray: np.ndarray) -> float:
    """Estimate small skew angle from long horizontal form lines."""
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180,
        threshold=120,
        minLineLength=max(80, gray.shape[1] // 5),
        maxLineGap=20,
    )
    if lines is None:
        return 0.0

    angles = []
    for line in lines[:, 0, :]:
        x1, y1, x2, y2 = line
        dx = x2 - x1
        dy = y2 - y1
        if dx == 0:
            continue
        angle = np.degrees(np.arctan2(dy, dx))
        # Keep near-horizontal lines only.
        if -15 <= angle <= 15:
            angles.append(angle)
    if not angles:
        return 0.0
    return float(np.median(angles))


def rotate_bound(img: np.ndarray, angle_degrees: float) -> np.ndarray:
    if abs(angle_degrees) < 0.25:
        return img
    h, w = img.shape[:2]
    center = (w / 2, h / 2)
    M = cv2.getRotationMatrix2D(center, angle_degrees, 1.0)
    cos = abs(M[0, 0])
    sin = abs(M[0, 1])
    new_w = int((h * sin) + (w * cos))
    new_h = int((h * cos) + (w * sin))
    M[0, 2] += (new_w / 2) - center[0]
    M[1, 2] += (new_h / 2) - center[1]
    return cv2.warpAffine(img, M, (new_w, new_h), borderValue=(255, 255, 255))


def normalize_page(img: np.ndarray) -> np.ndarray:
    img = crop_to_document(img)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    angle = estimate_skew_angle(gray)
    img = rotate_bound(img, angle)
    img = crop_to_document(img)
    return img


def preprocess_for_ocr(crop: np.ndarray, variant: str) -> np.ndarray:
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop.copy()

    # Upscale first. Small scanned text benefits a lot.
    gray = cv2.resize(gray, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)

    if variant == "otsu":
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        return cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    if variant == "adaptive":
        gray = cv2.medianBlur(gray, 3)
        return cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
        )

    if variant == "light":
        # Gentle cleanup; sometimes preserves weak characters better than hard thresholding.
        gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
        return gray

    raise ValueError(variant)


def ocr_text(crop: np.ndarray) -> str:
    texts = []
    for variant in ("otsu", "adaptive", "light"):
        proc = preprocess_for_ocr(crop, variant)
        txt = pytesseract.image_to_string(
            proc,
            config="--oem 3 --psm 6 -c preserve_interword_spaces=1",
        )
        texts.append(txt)
    # Return the OCR output that scores best as an address.
    return max(texts, key=lambda t: score_address(clean_address_text(t))[0])


def clean_address_text(raw: str) -> str:
    lines = []
    for line in raw.splitlines():
        line = line.strip(" \t|:;,_—-•")
        line = re.sub(r"\s+", " ", line)
        if not line:
            continue
        if BAD_HEADER_RE.search(line):
            continue
        if PHONE_RE.search(line):
            continue
        # Drop pure punctuation/noise lines.
        if len(re.sub(r"[^A-Za-z0-9]", "", line)) < 2:
            continue
        lines.append(line)

    # Keep the likely address block, stopping after city/state/zip.
    kept = []
    for line in lines:
        kept.append(line)
        if CITY_STATE_ZIP_RE.search(line) or ZIP_RE.search(line):
            break

    return "\n".join(kept).strip()


def score_address(address: str) -> tuple[float, list[str]]:
    lines = [l.strip() for l in address.splitlines() if l.strip()]
    score = 0.0
    reasons = []

    if 3 <= len(lines) <= 6:
        score += 25
        reasons.append("reasonable_line_count")
    elif len(lines) >= 2:
        score += 10

    if any(STREET_HINT_RE.search(l) for l in lines):
        score += 25
        reasons.append("street_hint")

    if any(CITY_STATE_ZIP_RE.search(l) for l in lines):
        score += 35
        reasons.append("city_state_zip")
    elif any(ZIP_RE.search(l) for l in lines):
        score += 15
        reasons.append("zip")

    alpha_chars = sum(c.isalpha() for c in address)
    digit_chars = sum(c.isdigit() for c in address)
    if alpha_chars >= 15 and digit_chars >= 4:
        score += 10
        reasons.append("text_balance")

    if BAD_HEADER_RE.search(address):
        score -= 30
        reasons.append("header_noise")

    if len(address) < 20:
        score -= 25
        reasons.append("too_short")

    return max(0.0, min(100.0, score)), reasons


def candidate_boxes(img: np.ndarray) -> Iterable[tuple[int, int, int, int]]:
    """
    Produce plausible SHIP TO address block crops.

    These are deliberately overlapping. The scorer chooses the crop whose OCR looks
    most like a real address. This is much more tolerant of shifting/skew than a
    single fixed crop.
    """
    h, w = img.shape[:2]

    # SHIP TO is in the upper-left quadrant, below SHIP FROM. The normalized crop
    # includes a range large enough for shifted scans.
    x_ranges = [(0.04, 0.45), (0.06, 0.50), (0.02, 0.42)]
    y_ranges = [
        (0.15, 0.29),
        (0.17, 0.32),
        (0.18, 0.35),
        (0.20, 0.34),
        (0.13, 0.31),
    ]

    seen = set()
    for xr in x_ranges:
        for yr in y_ranges:
            x1, x2 = int(xr[0] * w), int(xr[1] * w)
            y1, y2 = int(yr[0] * h), int(yr[1] * h)
            box = (x1, y1, x2, y2)
            if box not in seen:
                seen.add(box)
                yield box


def extract_ship_to(pdf_path: Path, debug_dir: Optional[Path] = None) -> ExtractionResult:
    img = render_pdf_page(pdf_path, page_index=1, dpi=350)
    norm = normalize_page(img)

    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(debug_dir / f"{pdf_path.stem}_normalized.png"), norm)

    best = None
    for idx, box in enumerate(candidate_boxes(norm)):
        x1, y1, x2, y2 = box
        crop = norm[y1:y2, x1:x2]
        if crop.size == 0:
            continue

        raw = ocr_text(crop)
        addr = clean_address_text(raw)
        conf, reasons = score_address(addr)

        if debug_dir:
            cv2.imwrite(str(debug_dir / f"{pdf_path.stem}_cand_{idx:02d}_{int(conf):03d}.png"), crop)
            (debug_dir / f"{pdf_path.stem}_cand_{idx:02d}_{int(conf):03d}.txt").write_text(
                f"CONF={conf}\nREASONS={reasons}\nRAW:\n{raw}\n\nCLEAN:\n{addr}\n",
                encoding="utf-8",
            )

        rec = (conf, addr, raw, box)
        if best is None or rec[0] > best[0]:
            best = rec

    if best is None:
        return ExtractionResult(str(pdf_path), "", 0.0, (0, 0, 0, 0), "", "no candidate crops")

    conf, addr, raw, box = best
    warning = None
    if conf < 60:
        warning = "low confidence; inspect debug crop or add this layout to calibration set"

    return ExtractionResult(str(pdf_path), addr, conf, box, raw, warning)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--debug-dir", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    results = []
    for pdf in args.pdfs:
        try:
            result = extract_ship_to(pdf, args.debug_dir)
        except Exception as e:
            result = ExtractionResult(str(pdf), "", 0.0, (0, 0, 0, 0), "", f"ERROR: {e}")
        results.append(result)

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        for r in results:
            print("=" * 80)
            print(r.pdf)
            print(f"confidence: {r.confidence:.1f}")
            if r.warning:
                print(f"warning: {r.warning}")
            print(r.address)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
