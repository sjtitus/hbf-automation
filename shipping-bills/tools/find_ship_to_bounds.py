#!/usr/bin/env python3
"""
Visualize the SHIP TO box vertical bounds on page 2 of each invoice PDF.

Step 1 of a new "growing rectangle" cropping approach: locate a y-coordinate
seed inside the SHIP TO band on the BOL.

Anchors are two pieces of fixed form text that flank the SHIP TO box and
survive even on the worst-OCR scans in our fixture set:
    UPPER  -> "Bill of Lading Number"   (sits above the SHIP TO header row)
    LOWER  -> "Highland Beef Farms"     (sits inside the THIRD PARTY box)

Detection uses rapidfuzz.partial_ratio against the target phrase rather
than a regex -- handles arbitrary OCR drift ("Bil of Lading Numbar",
"Higniand Bee Farns", etc.) without enumerating every variant. Threshold
is calibrated to reject the only nearby decoys: the page header
"BILL OF LADING -- SHORT FORM" (partial_ratio ~71) and the body text
"Master bill of lading with attached underlying bills of lading" (~70).

For each anchor, take the mean y_top of every matching OCR line (multi-PSM
duplicates and form re-renders contribute to the average). The midline is
the mean of the two anchor y values.

Output: one PNG per PDF in --out-dir (wiped on every run). The full page-2
image (only crop_to_document's white-margin trim is applied) is annotated
with full-width horizontal lines:
    red   = upper bound  (Bill of Lading Number)
    red   = lower bound  (Highland Beef Farms)
    green = midline

Usage:
    python tools/find_ship_to_bounds.py tests/fixtures/badger/*.pdf
    python tools/find_ship_to_bounds.py --fuzz-threshold 80 *.pdf
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

import cv2
from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).parent))
from crop_ship_to import (  # noqa: E402
    render_pdf_page,
    crop_to_document,
    ocr_lines,
    _binarize,
    _ocr_pass,
)


def ocr_lines_with_sparse(img) -> list[dict]:
    """ocr_lines (PSM 3 + PSM 6) plus a third PSM 11 (sparse text) pass.

    PSM 11 has no global page segmentation -- it just finds scattered text.
    Important here because the full-page PSM 3/6 segmenter sometimes clusters
    the BOLN label with the adjacent bar-code box and drops it (e.g. Vistar
    0065560). PSM 11 recovers it cleanly. We only need this for the bounds
    tool; crop_ship_to.py's shared ocr_lines() stays unchanged."""
    base = ocr_lines(img)
    bin_img = _binarize(img)
    sparse = _ocr_pass(bin_img, psm=11)
    return sorted(base + sparse, key=lambda d: d["y_top"])


RED = (0, 0, 220)
GREEN = (0, 200, 0)
LINE_THICK = 3

UPPER_TARGET = "bill of lading number"
LOWER_TARGET = "highland beef farms"
DEFAULT_FUZZ_THRESHOLD = 85

# Fallback for upper bound when BOLN can't be found. The page-header
# "BILL OF LADING - SHORT FORM - NOT NEGOTIABLE" sits at the very top of
# every BOL and OCRs reliably even when BOLN doesn't (e.g. Tucson 0065555,
# where BOLN drifts to 'Ti of Latliog Naersteet' but the header still
# reads as 'ILL OF LADING SHORT EORM NUF NEGOTIATE'). We anchor on the
# bottom of the header line + a small pad as a proxy for BOLN's y.
HEADER_TARGET = "bill of lading short form"
HEADER_FUZZ_THRESHOLD = 75   # more lenient than BOLN/HBF -- header OCR drifts
HEADER_PAD_PX = 30           # extra below header bottom to approximate BOLN y


def _line_text(ln: dict) -> str:
    return re.sub(r"\s+", " ", ln["text"]).strip()


MIN_LEN_FRAC = 0.9  # candidate must be ~ as long as target (see below)


def find_anchor_signals(lines: list[dict], target: str, label: str,
                        threshold: int) -> list[tuple[str, int, int]]:
    """Return (label, y_top, score) for every OCR line whose
    partial_ratio against `target` meets `threshold`.

    Length floor: partial_ratio is symmetric in the sense that it scores 100
    whenever the SHORTER string is a substring of the longer. We want
    "target is contained in candidate", so when the candidate is shorter
    than the target the question is ill-posed and yields false positives
    (a single OCR fragment 'i' matches both 'bill of lading number' and
    'highland beef farms' with score 100). Require candidate length to
    be at least MIN_LEN_FRAC * len(target) to suppress those."""
    min_len = int(len(target) * MIN_LEN_FRAC)
    out: list[tuple[str, int, int]] = []
    for ln in lines:
        text = _line_text(ln).lower()
        if len(text) < min_len:
            continue
        score = int(fuzz.partial_ratio(target, text))
        if score >= threshold:
            out.append((label, ln["y_top"], score))
    return out


def _aggregate(signals: list[tuple[str, int, int]]) -> Optional[int]:
    if not signals:
        return None
    return int(round(sum(y for _, y, _ in signals) / len(signals)))


def find_header_fallback(lines: list[dict]) -> Optional[tuple[int, int, int]]:
    """When BOLN can't be matched, fall back to the page-header text
    'BILL OF LADING - SHORT FORM - NOT NEGOTIABLE'. Returns
    (avg_y_bot, avg_score, count) or None.

    Uses y_bot (not y_top) because the synthesized upper bound should be
    BELOW the header, near where BOLN would have been on a clean scan."""
    target = HEADER_TARGET
    min_len = int(len(target) * 0.7)  # header OCR drifts more than BOLN/HBF
    matches: list[tuple[int, int]] = []
    for ln in lines:
        text = _line_text(ln).lower()
        if len(text) < min_len:
            continue
        score = int(fuzz.partial_ratio(target, text))
        if score >= HEADER_FUZZ_THRESHOLD:
            matches.append((ln["y_bot"], score))
    if not matches:
        return None
    avg_y_bot = int(round(sum(y for y, _ in matches) / len(matches)))
    avg_score = int(round(sum(s for _, s in matches) / len(matches)))
    return (avg_y_bot, avg_score, len(matches))


def annotate(pdf_path: Path, out_dir: Path, dpi: int,
             fuzz_threshold: int) -> tuple[Path, str]:
    img = render_pdf_page(pdf_path, page_index=1, dpi=dpi)
    img = crop_to_document(img)
    h, w = img.shape[:2]

    lines = ocr_lines_with_sparse(img)
    upper_sigs = find_anchor_signals(lines, UPPER_TARGET, "BOLN", fuzz_threshold)
    lower_sigs = find_anchor_signals(lines, LOWER_TARGET, "HBF", fuzz_threshold)
    upper_y = _aggregate(upper_sigs)
    lower_y = _aggregate(lower_sigs)

    upper_label_extra = ""
    if upper_y is None:
        header = find_header_fallback(lines)
        if header is not None:
            y_bot, score, count = header
            upper_y = y_bot + HEADER_PAD_PX
            upper_label_extra = f"HEADER@y_bot={y_bot}({score})x{count}+pad{HEADER_PAD_PX}"

    mid_y = ((upper_y + lower_y) // 2
             if upper_y is not None and lower_y is not None else None)

    def draw_full_width(y: int, color: tuple[int, int, int], label: str) -> None:
        cv2.line(img, (0, y), (w - 1, y), color, LINE_THICK, cv2.LINE_AA)
        cv2.putText(img, label, (12, max(22, y - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA)

    notes = []
    if upper_y is not None:
        if upper_sigs:
            sigs = ", ".join(f"{n}@{y}({s})" for n, y, s in upper_sigs)
            draw_full_width(upper_y, RED, f"UPPER y={upper_y}  [{sigs}]")
            notes.append(f"upper={upper_y}({len(upper_sigs)})")
        else:
            draw_full_width(upper_y, RED, f"UPPER y={upper_y}  [{upper_label_extra}]")
            notes.append(f"upper={upper_y}(fallback)")
    else:
        notes.append("upper=NONE")

    if lower_y is not None:
        sigs = ", ".join(f"{n}@{y}({s})" for n, y, s in lower_sigs)
        draw_full_width(lower_y, RED, f"LOWER y={lower_y}  [{sigs}]")
        notes.append(f"lower={lower_y}({len(lower_sigs)})")
    else:
        notes.append("lower=NONE")

    if mid_y is not None:
        draw_full_width(mid_y, GREEN, f"MID y={mid_y}")
        notes.append(f"mid={mid_y}")

    out_path = out_dir / f"{pdf_path.stem}_bounds.png"
    cv2.imwrite(str(out_path), img)
    return out_path, "; ".join(notes)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--out-dir", type=Path, default=Path("shipto_bounds"))
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--fuzz-threshold", type=int, default=DEFAULT_FUZZ_THRESHOLD,
                    help=f"partial_ratio threshold for anchor detection "
                         f"(default {DEFAULT_FUZZ_THRESHOLD})")
    args = ap.parse_args(argv)

    if args.out_dir.exists():
        shutil.rmtree(args.out_dir)
    args.out_dir.mkdir(parents=True)

    ok = 0
    for pdf in args.pdfs:
        try:
            out_path, notes = annotate(pdf, args.out_dir, dpi=args.dpi,
                                       fuzz_threshold=args.fuzz_threshold)
        except Exception as e:
            print(f"{pdf}: ERROR {e}")
            continue
        ok += 1
        print(f"{pdf}: OK -> {out_path}  [{notes}]")

    print(f"\n{ok}/{len(args.pdfs)} bound annotations written to {args.out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
