#!/usr/bin/env python3
"""
Read SHIP TO addresses from cropped PNGs (output of crop_ship_to.py).

Local-only: tesseract OCR with multi-preprocessing variants, then parse the
text into {name, street_line_1, street_line_2, city, state, postcode} using
regex + usaddress-scourgify (USPS Pub 28 normalization).

No external API calls. Uses dependencies already in the project venv:
opencv-python, pytesseract, usaddress-scourgify.

Usage:
    python tools/read_ship_to.py                          # ship_to_crops/
    python tools/read_ship_to.py ship_to_crops_loose/     # different dir
    python tools/read_ship_to.py --json ship_to_crops/    # JSON output
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import pytesseract
from scourgify import normalize_address_record
from scourgify.exceptions import AddressNormalizationError


CITY_STATE_ZIP_RE = re.compile(
    r"\b([A-Za-z][A-Za-z .'\-]+?),?\s+"
    r"(A[LKSZR]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|"
    r"M[ADEHINOPST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AIT]|W[AIVY])"
    r"\s+(\d{5}(?:-\d{4})?)\b"
)
PHONE_RE = re.compile(r"\b\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
LABEL_LINE_RE = re.compile(
    r"^\s*(?:ship\s*to|ship\s*from|bill\s*to|attn|attention|"
    r"third\s*party|carrier\s*name|trailer|serial|consignee|"
    r"freight\s*charge)\b",
    re.IGNORECASE,
)
JUNK_LINE_RE = re.compile(r"^[\W_]*$")  # pure punctuation/whitespace


@dataclass
class Extraction:
    file: str
    name: Optional[str]
    street_line_1: Optional[str]
    street_line_2: Optional[str]
    city: Optional[str]
    state: Optional[str]
    postcode: Optional[str]
    raw_ocr: str
    note: str = ""


def preprocess(crop: np.ndarray, variant: str) -> np.ndarray:
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop.copy()
    gray = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    if variant == "otsu":
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        return cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    if variant == "adaptive":
        gray = cv2.medianBlur(gray, 3)
        return cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
        )
    if variant == "light":
        return cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    raise ValueError(variant)


def ocr_variant(image: np.ndarray) -> str:
    return pytesseract.image_to_string(
        image, config="--oem 3 --psm 6 -c preserve_interword_spaces=1"
    )


def score_text(text: str) -> int:
    """Heuristic: prefer OCR that contains a city,state,zip pattern."""
    score = 0
    if CITY_STATE_ZIP_RE.search(text):
        score += 50
    digits = sum(c.isdigit() for c in text)
    alpha = sum(c.isalpha() for c in text)
    if digits >= 5 and alpha >= 10:
        score += 10
    score += min(20, len(text.splitlines()))
    return score


def best_ocr(crop: np.ndarray) -> str:
    candidates = []
    for variant in ("otsu", "adaptive", "light"):
        proc = preprocess(crop, variant)
        text = ocr_variant(proc)
        candidates.append((score_text(text), text))
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def clean_lines(raw: str) -> list[str]:
    out = []
    for ln in raw.splitlines():
        s = ln.strip(" \t|:;_—-•~")
        s = re.sub(r"\s+", " ", s).strip()
        if not s or JUNK_LINE_RE.match(s):
            continue
        if PHONE_RE.fullmatch(s):
            continue
        if LABEL_LINE_RE.match(s):
            continue
        out.append(s)
    return out


def parse_address(raw_text: str) -> tuple[Optional[str], Optional[str], Optional[str],
                                          Optional[str], Optional[str], Optional[str], str]:
    """Returns (name, street_1, street_2, city, state, postcode, note)."""
    lines = clean_lines(raw_text)
    if not lines:
        return None, None, None, None, None, None, "no usable OCR lines"

    # Find the line containing city, state, zip.
    csz_idx = None
    csz_match = None
    for i, ln in enumerate(lines):
        m = CITY_STATE_ZIP_RE.search(ln)
        if m:
            csz_idx = i
            csz_match = m
            break

    if csz_match is None:
        # No city/state/zip recovered. Best-effort: first line as name.
        return lines[0], None, None, None, None, None, "no city/state/zip pattern found"

    city = csz_match.group(1).strip()
    state = csz_match.group(2).upper()
    postcode = csz_match.group(3)

    # Lines above the CSZ line: name(s) + street(s).
    above = lines[:csz_idx]
    if not above:
        return None, None, None, city.upper(), state, postcode, "no lines above city/state/zip"

    # Heuristic: street lines start with a digit (house number) or "PO Box".
    street_idxs = [
        i for i, ln in enumerate(above)
        if re.match(r"^\s*(?:\d|p\.?\s*o\.?\s*box)", ln, re.IGNORECASE)
    ]

    if street_idxs:
        first_street = street_idxs[0]
        name_lines = above[:first_street]
        street_lines = above[first_street:]
    else:
        # No house-number line; assume last line above CSZ is the street.
        name_lines = above[:-1] if len(above) > 1 else []
        street_lines = above[-1:] if above else []

    name = " ".join(name_lines).strip() or None
    street_line_1 = street_lines[0] if street_lines else None
    street_line_2 = street_lines[1] if len(street_lines) > 1 else None

    note = ""
    if street_line_1:
        # Run scourgify for USPS Pub 28 normalization.
        try:
            single = ", ".join([street_line_1] + ([street_line_2] if street_line_2 else [])
                               + [f"{city}, {state} {postcode}"])
            normed = normalize_address_record(single)
            street_line_1 = normed.get("address_line_1") or street_line_1
            street_line_2 = normed.get("address_line_2") or street_line_2
            city = normed.get("city") or city
            state = normed.get("state") or state
            postcode = normed.get("postal_code") or postcode
        except (AddressNormalizationError, Exception) as e:
            note = f"scourgify: {type(e).__name__}"

    return (name, street_line_1, street_line_2,
            (city or "").upper() or None, state, postcode, note)


def extract(png_path: Path) -> Extraction:
    img = cv2.imread(str(png_path))
    if img is None:
        return Extraction(str(png_path), None, None, None, None, None, None,
                          "", "could not load image")
    raw = best_ocr(img)
    name, s1, s2, city, state, zipc, note = parse_address(raw)
    return Extraction(str(png_path), name, s1, s2, city, state, zipc, raw, note)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_dir", nargs="?", type=Path, default=Path("ship_to_crops"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    pngs = sorted(args.input_dir.glob("*.png"))
    if not pngs:
        print(f"no PNGs in {args.input_dir}", file=sys.stderr)
        return 1

    results = [extract(p) for p in pngs]

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2, default=str))
        return 0

    for r in results:
        stem = Path(r.file).stem
        line2 = f" / {r.street_line_2}" if r.street_line_2 else ""
        loc = f"{r.city or '?'}, {r.state or '?'} {r.postcode or '?'}".strip()
        note = f"  [{r.note}]" if r.note else ""
        print(f"{stem}: {r.name or '?'} | {r.street_line_1 or '?'}{line2} | {loc}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
