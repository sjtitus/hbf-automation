#!/usr/bin/env python3
"""
Extract SHIP TO address lines from a Badger BOL by anchoring on the
city/state/zip line (CSZ) and walking upward one line at a time.

Pipeline:
  1. Bounds: BOLN (top of page) and HBF (inside THIRD PARTY) via
     find_ship_to_bounds. mid_y = average of the two.
  2. ROI:    img[mid_y - 50 : lower_y + 50, 0 : w/2 + 30].
  3. OCR:    PSM 3 + 6 + 11 (focused on the ROI).
  4. CSZ:    pick the topmost CSZ-matching OCR line in the ROI; among
             overlapping PSM versions of the same physical row, take the
             one with the tightest bounding box.
  5. Walk:   step up from CSZ by ~one line height. At each step pick the
             OCR line nearest the projected target y, accept it if it
             looks like address content, stop on boundary phrase or
             non-content. Lock to actual stride after the first hop.
  6. Draw:   cyan boxes around walked lines (U1, U2, U3 closest-to-CSZ
             outward), orange FINAL CROP rectangle, red dashed STOP marker.

Output: shipto_bounds/<stem>_extract.png + a printed line per PDF.

Usage:
    python tools/extract_ship_to_lines.py tests/fixtures/badger/*.pdf
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import pytesseract
from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).parent))
from crop_ship_to import (  # noqa: E402
    render_pdf_page, crop_to_document, _binarize, _ocr_pass,
)
from find_ship_to_bounds import (  # noqa: E402
    ocr_lines_with_sparse, find_anchor_signals, find_header_fallback,
    _aggregate, _line_text,
    UPPER_TARGET, LOWER_TARGET, DEFAULT_FUZZ_THRESHOLD, HEADER_PAD_PX,
)


# ============================================================
# CONFIGURATION
# All tunable parameters live in ExtractConfig. The CLI exposes the
# most commonly-adjusted knobs; everything else is overridable in code
# via dataclasses.replace(DEFAULT_CONFIG, ...). Annotation styling is
# kept as module constants because it's purely cosmetic.
# ============================================================

DEFAULT_BOUNDARY_PHRASES: tuple[str, ...] = (
    "ship to", "ship from", "carrier name",
    "trailer number", "serial number", "bill of lading number",
    "bar code",
)


@dataclass(frozen=True)
class ExtractConfig:
    # --- Render ---
    dpi: int = 300

    # --- Bounds detection (passed through to find_ship_to_bounds) ---
    bounds_fuzz_threshold: int = DEFAULT_FUZZ_THRESHOLD

    # --- ROI (focused OCR window for the SHIP TO area) ---
    # x_right is computed as `divider_x + roi_pad_right`, where divider_x
    # is the midpoint between the words SHORT and FORM in the page header
    # (per-BOL detection of the column 1 / column 2 boundary). Falls back
    # to `w/2` when the header words can't be found.
    #
    # +60 of gutter pad past divider_x is calibrated empirically: gives
    # tesseract enough right-margin context that it doesn't re-segment
    # column-1 lines differently. At pad=0 'Vistar Retail West' was
    # dropped entirely on 0065560; at +30 it stayed dropped; +60
    # recovers it. The gutter region is whitespace on Badger BOLs at
    # ~60 px wide, so the pad lands at the start of column 2 without
    # consistently grabbing column-2 text.
    roi_pad_top: int = 50
    roi_pad_bottom: int = 50
    roi_pad_right: int = 60

    # --- PSM-duplicate dedupe (collapse same-row OCR variants) ---
    psm_dup_text_sim: int = 75            # token_set_ratio threshold
    cluster_min_text_len_frac: float = 0.8  # within-cluster length filter

    # --- Stride-up walker ---
    max_lines_above_csz: int = 4
    initial_stride_pad: int = 8     # px added to CSZ height for first stride
    stride_tolerance_min: int = 15  # px; min half-window for finding next line
    stride_lock_min: int = 30       # px; lower bound for actual_stride to lock
    stride_lock_max: int = 60       # px; upper bound

    # --- Boundary phrases (stop walking if hit) ---
    boundary_phrases: tuple[str, ...] = DEFAULT_BOUNDARY_PHRASES
    boundary_fuzz: int = 70
    boundary_min_len_frac: float = 0.7

    # --- Address content classifier ---
    min_alnum_for_content: int = 3
    address_word_min_len: int = 5


DEFAULT_CONFIG = ExtractConfig()


# ---------- Annotation colors / thickness (cosmetic, not configurable) ----------
RED = (0, 0, 220)
MID_GREEN = (0, 200, 0)
CSZ_GREEN = (40, 220, 80)
CYAN = (255, 220, 0)
ORANGE = (0, 140, 255)
BLACK = (0, 0, 0)
RECT_THICK = 4
WALK_RECT_THICK = 3
FINAL_RECT_THICK = 5
LINE_THICK = 3
TEXT_FONT = cv2.FONT_HERSHEY_SIMPLEX

# ---------- CSZ pattern (regex constant; not config-tunable) ----------
# <City>, <ST> <ZIP>. City is letters + spaces + dots + apostrophes + hyphens.
# Tolerates leading OCR noise (`|`, `:`, `'`, etc.) via the leading \b.
CSZ_RE = re.compile(
    r"\b([A-Z][A-Za-z .'\-]{0,40}?)"
    r",?\s+"
    r"([A-Z]{2})"
    r"\s+"
    r"(\d{5}(?:-\d{4})?)"
    r"\b"
)

# Helper regexes for is_address_content
_WORD_RE = re.compile(r"[A-Za-z]+")
_ATTN_RE = re.compile(r"\battn", re.IGNORECASE)
_POBOX_RE = re.compile(r"\bp\.?\s*o\.?\s*box\b", re.IGNORECASE)


def is_address_content(text: str, cfg: ExtractConfig = DEFAULT_CONFIG) -> bool:
    """True if `text` looks like a real address line. Requires AT LEAST
    cfg.min_alnum_for_content alphanumeric chars total (rejects 2-char
    digit-bearing fragments like '4a' that were sneaking through the
    'has any digit' rule), AND at least one of:
        - any digit (street number, suite, zip, phone, etc.)
        - 'Attn' (case-insensitive)
        - 'P.O. Box' / 'PO Box' pattern
        - a word of length >= cfg.address_word_min_len that is either
          capitalized (first letter upper, rest lower) OR all-uppercase

    The cap/upper-word rule is the noise filter. Real address lines
    almost always contain a 5+ letter "real" word ('Foods', 'Vaughn',
    'Salinas', 'TUCSON', 'SHERIDAN'). The known noise patterns from the
    BOL header decorative band have only short cap-words ('Sati', 'Roca',
    'Ree') or all-lowercase letter clumps ('torent', 'caaik', 'taken'),
    so they fail the rule cleanly."""
    if sum(c.isalnum() for c in text) < cfg.min_alnum_for_content:
        return False
    if any(c.isdigit() for c in text):
        return True
    if _ATTN_RE.search(text):
        return True
    if _POBOX_RE.search(text):
        return True
    for word in _WORD_RE.findall(text):
        if len(word) >= cfg.address_word_min_len and (word[0].isupper() or word.isupper()):
            return True
    return False


def matches_boundary(text: str, cfg: ExtractConfig = DEFAULT_CONFIG) -> Optional[str]:
    """Return the matched boundary phrase (lowercase) if `text` is one of
    the SHIP TO header / right-column labels, else None. Uses partial_ratio
    with a length floor so a one-char OCR fragment doesn't accidentally
    score 100 against a multi-word phrase."""
    low = text.lower().strip()
    if not low:
        return None
    for p in cfg.boundary_phrases:
        if len(low) < len(p) * cfg.boundary_min_len_frac:
            continue
        if fuzz.partial_ratio(p, low) >= cfg.boundary_fuzz:
            return p
    return None


# ---------- Header word anchor experiment ----------
# Find 'SHORT' x_right and 'FORM' x_left in the page header
# 'BILL OF LADING — SHORT FORM — NOT NEGOTIABLE'. The em-dash between
# SHORT and FORM is the visual divider that may align with the form's
# column-1/column-2 boundary. Reporting both anchors plus their midpoint
# so we can eyeball which (if any) matches the actual column divider.
def _find_header_word(img: np.ndarray, target: str,
                      threshold: int = 75) -> Optional[dict]:
    """Word-level OCR the top of the page; return the topmost word that
    fuzz-matches `target`. Returns dict with x_left, x_right, y_top,
    text, score; or None if no match."""
    h = img.shape[0]
    header_h = min(h, max(300, int(h * 0.10)))
    crop = img[:header_h, :]
    bin_crop = _binarize(crop)
    data = pytesseract.image_to_data(
        bin_crop, output_type=pytesseract.Output.DICT,
        config="--oem 3 --psm 6 -c preserve_interword_spaces=1",
    )
    target_len = len(target)
    candidates: list[dict] = []
    for i in range(len(data["text"])):
        word = data["text"][i].strip()
        if not word or len(word) > target_len + 2:
            continue
        score = fuzz.ratio(word.upper(), target.upper())
        if score >= threshold:
            candidates.append({
                "text": word,
                "x_left": data["left"][i],
                "x_right": data["left"][i] + data["width"][i],
                "y_top": data["top"][i],
                "score": score,
            })
    if not candidates:
        return None
    candidates.sort(key=lambda d: (d["y_top"], -d["score"]))
    return candidates[0]


def find_header_anchors(img: np.ndarray) -> dict:
    """Return {'short': dict|None, 'form': dict|None, 'divider_x': int|None}.
    'divider_x' is the midpoint between SHORT's x_right and FORM's x_left
    when both are found; falls back to FORM's x_left if only FORM is
    available, SHORT's x_right if only SHORT is, else None."""
    short = _find_header_word(img, "SHORT")
    form = _find_header_word(img, "FORM")
    if short is not None and form is not None:
        divider = (short["x_right"] + form["x_left"]) // 2
    elif form is not None:
        divider = form["x_left"]
    elif short is not None:
        divider = short["x_right"]
    else:
        divider = None
    return {"short": short, "form": form, "divider_x": divider}


# ---------- OCR ----------
def _ocr_roi(img: np.ndarray, x1: int, y1: int, x2: int, y2: int) -> list[dict]:
    """OCR a focused ROI with PSM 3 + 6 + 11; returns line dicts in
    full-image coordinates."""
    crop = img[y1:y2, x1:x2]
    bin_crop = _binarize(crop)
    raw = (_ocr_pass(bin_crop, psm=3)
           + _ocr_pass(bin_crop, psm=6)
           + _ocr_pass(bin_crop, psm=11))
    out = []
    for ln in raw:
        ln["y_top"] += y1
        ln["y_bot"] += y1
        ln["x_left"] += x1
        ln["x_right"] += x1
        out.append(ln)
    return sorted(out, key=lambda d: d["y_top"])


def dedupe_psm_duplicates(lines: list[dict],
                          cfg: ExtractConfig = DEFAULT_CONFIG) -> list[dict]:
    """Collapse OCR lines that are PSM versions of the same physical row.
    Two lines are 'the same row' iff their y-ranges overlap AND their
    text token_set_ratio is >= cfg.psm_dup_text_sim.

    Checks ALL existing clusters whose members y-overlap with the
    candidate line, not just the most recent one. Otherwise PSM
    duplicates of the same row split into separate clusters when noise
    lines (with different text) at intermediate y_top values fall
    between them in the sort order."""
    if not lines:
        return []
    sorted_lines = sorted(lines, key=lambda d: d["y_top"])
    clusters: list[list[dict]] = []
    for ln in sorted_lines:
        text = _line_text(ln).lower()
        joined = False
        for cluster in clusters:
            if not any(ln["y_top"] <= prior["y_bot"] for prior in cluster):
                continue
            if any(fuzz.token_set_ratio(text, _line_text(prior).lower())
                   >= cfg.psm_dup_text_sim for prior in cluster):
                cluster.append(ln)
                joined = True
                break
        if not joined:
            clusters.append([ln])
    out = []
    for cluster in clusters:
        # Two-stage selection. First, drop fragment-like cluster members
        # whose text is much shorter than the cluster's longest text --
        # otherwise a 5-char OCR fragment ('AZ 85') would beat the full
        # CSZ line on tightness. Second, among the survivors, pick the
        # tightest bbox -- among versions with equally complete text,
        # the cleanest box wins (avoids PSM 11's habit of inflating
        # y_bot into the next row, which moves the line out of the
        # walker's reach).
        max_len = max(len(_line_text(d)) for d in cluster)
        viable = [d for d in cluster
                  if len(_line_text(d)) >= max_len * cfg.cluster_min_text_len_frac]
        best = min(viable, key=lambda d: d["y_bot"] - d["y_top"])
        out.append({
            "text": best["text"],
            "y_top": best["y_top"],
            "y_bot": best["y_bot"],
            "x_left": best["x_left"],
            "x_right": best["x_right"],
        })
    return out


# ---------- CSZ anchor ----------
def find_csz_line(lines: list[dict], roi_y_top: int, roi_y_bot: int) -> Optional[dict]:
    """Return the OCR line for the SHIP TO CSZ. Group overlapping CSZ
    candidates as 'same physical row' across PSM passes; pick the
    tightest-height member of the topmost group."""
    candidates: list[tuple[dict, re.Match]] = []
    for ln in lines:
        if ln["y_top"] < roi_y_top or ln["y_top"] > roi_y_bot:
            continue
        m = CSZ_RE.search(_line_text(ln))
        if m:
            candidates.append((ln, m))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0]["y_top"])
    groups: list[list[tuple[dict, re.Match]]] = [[candidates[0]]]
    for ln, m in candidates[1:]:
        last_ln, _ = groups[-1][-1]
        if ln["y_top"] <= last_ln["y_bot"]:
            groups[-1].append((ln, m))
        else:
            groups.append([(ln, m)])
    top_group = groups[0]
    best_ln, best_m = min(top_group,
                          key=lambda c: c[0]["y_bot"] - c[0]["y_top"])
    return {
        **best_ln,
        "csz_city": best_m.group(1).strip(),
        "csz_state": best_m.group(2),
        "csz_zip": best_m.group(3),
    }


# ---------- Stride-up walker ----------
def walk_up_from_csz(roi_lines: list[dict], csz_line: dict, mid_y: int,
                     cfg: ExtractConfig = DEFAULT_CONFIG
                     ) -> tuple[list[dict], str, Optional[dict]]:
    """Walk up from CSZ one line at a time. Return (address_lines_in_order,
    stop_reason, stop_at_line). Stride is initially CSZ height + pad,
    then locks to the actual observed stride after the first successful
    hop."""
    address = [csz_line]
    csz_height = csz_line["y_bot"] - csz_line["y_top"]
    stride = csz_height + cfg.initial_stride_pad
    last_y_top = csz_line["y_top"]
    stop_reason = f"reached max_steps={cfg.max_lines_above_csz}"
    stop_at: Optional[dict] = None

    for _ in range(cfg.max_lines_above_csz):
        target = last_y_top - stride
        if target < mid_y:
            stop_reason = f"target y={target} below mid_y={mid_y}"
            break

        tolerance = max(stride // 2, cfg.stride_tolerance_min)
        candidates = [
            ln for ln in roi_lines
            if ln["y_top"] < last_y_top
            and abs(ln["y_top"] - target) < tolerance
        ]
        if not candidates:
            stop_reason = f"no OCR line within +/-{tolerance}px of target y={target}"
            break

        # Group overlapping candidates (PSM duplicates) by y_bot of the
        # last member of the group.
        candidates.sort(key=lambda ln: ln["y_top"])
        groups: list[list[dict]] = [[candidates[0]]]
        for ln in candidates[1:]:
            if ln["y_top"] <= groups[-1][-1]["y_bot"]:
                groups[-1].append(ln)
            else:
                groups.append([ln])

        # Prefer groups containing real address content over groups whose
        # only members are short OCR fragments. Without this, a junk
        # 1-3 char fragment ('Tr', 'ast', 'oh') that lands near the
        # target y beats the real address line a few px away and stops
        # the walk prematurely.
        content_groups = [
            g for g in groups
            if any(is_address_content(_line_text(ln), cfg) for ln in g)
            and not any(matches_boundary(_line_text(ln), cfg) for ln in g)
        ]
        eligible = content_groups if content_groups else groups
        best_group = min(eligible, key=lambda g: abs(g[0]["y_top"] - target))
        # Within the chosen group, prefer:
        #   1. Members that pass the content check
        #   2. Longer text (catches Sheridan: PSM 6 'SHERIDAN FOC & CAMP'
        #      vs PSM 11 'SHERID.' both at y=629; without this, tightest
        #      bbox wins and the truncated 'SHERID.' is picked)
        #   3. Tightest bbox
        members = sorted(
            best_group,
            key=lambda ln: (not is_address_content(_line_text(ln), cfg),
                            -len(_line_text(ln)),
                            ln["y_bot"] - ln["y_top"]),
        )
        best = members[0]
        text = _line_text(best)

        b = matches_boundary(text, cfg)
        if b:
            stop_reason = f"boundary '{b}' @ {text[:50]!r}"
            stop_at = best
            break
        if not is_address_content(text, cfg):
            stop_reason = f"non-content @ {text[:50]!r}"
            stop_at = best
            break

        address.insert(0, best)
        actual_stride = last_y_top - best["y_top"]
        if cfg.stride_lock_min < actual_stride < cfg.stride_lock_max:
            stride = actual_stride
        last_y_top = best["y_top"]

    return address, stop_reason, stop_at


# ---------- Annotation ----------
def _put(img, text, org, color, scale=0.7, thick=2):
    cv2.putText(img, text, org, TEXT_FONT, scale, color, thick, cv2.LINE_AA)


def _draw_full_width_line(img, y, color, thick=LINE_THICK):
    h, w = img.shape[:2]
    cv2.line(img, (0, y), (w - 1, y), color, thick, cv2.LINE_AA)


def _draw_dashed_hline(img, y, color, thick=2, dash=22, gap=14):
    h, w = img.shape[:2]
    x = 0
    while x < w:
        cv2.line(img, (x, y), (min(w - 1, x + dash), y), color, thick)
        x += dash + gap


def annotate(img: np.ndarray, mid_y: int, lower_y: int, upper_y: int,
             csz: Optional[dict], address_lines: list[dict],
             stop_reason: str, stop_at: Optional[dict],
             header_anchors: Optional[dict] = None) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]

    # Bound lines for context.
    _draw_full_width_line(out, upper_y, RED)
    _put(out, f"UPPER y={upper_y}", (12, max(22, upper_y - 10)), RED, scale=0.7)
    _draw_full_width_line(out, lower_y, RED)
    _put(out, f"LOWER y={lower_y}", (12, max(22, lower_y - 10)), RED, scale=0.7)
    _draw_full_width_line(out, mid_y, MID_GREEN)
    _put(out, f"MID y={mid_y}", (12, max(22, mid_y - 10)), MID_GREEN, scale=0.7)

    # Reference: dotted line at exactly w/2 for visual comparison.
    cv2.line(out, (w // 2, 0), (w // 2, h - 1), (180, 180, 180), 1, cv2.LINE_AA)
    _put(out, f"w/2={w//2}", (w // 2 + 6, 30), (140, 140, 140), scale=0.7)

    # Header-word anchor vertical lines (experiment).
    #   blue  = end of SHORT (x_right)
    #   magenta = start of FORM (x_left)
    #   yellow = midpoint between them (candidate column-1 boundary)
    if header_anchors is not None:
        short = header_anchors.get("short")
        form = header_anchors.get("form")
        divider = header_anchors.get("divider_x")
        BLUE = (220, 100, 0)
        MAGENTA = (220, 0, 220)
        YELLOW_THICK = (0, 220, 255)
        if short is not None:
            sx = short["x_right"]
            cv2.line(out, (sx, 0), (sx, h - 1), BLUE, 2, cv2.LINE_AA)
            _put(out, f"SHORT end x={sx}", (max(12, sx - 280), 90),
                 BLUE, scale=0.7)
        if form is not None:
            fx = form["x_left"]
            cv2.line(out, (fx, 0), (fx, h - 1), MAGENTA, 2, cv2.LINE_AA)
            _put(out, f"FORM start x={fx}", (fx + 8, 90), MAGENTA, scale=0.7)
        if divider is not None:
            cv2.line(out, (divider, 0), (divider, h - 1), YELLOW_THICK, 4, cv2.LINE_AA)
            delta = divider - (w // 2)
            sign = "+" if delta >= 0 else ""
            _put(out, f"DIVIDER x={divider} ({sign}{delta} from w/2)",
                 (max(12, divider - 480), 130), YELLOW_THICK, scale=0.85)

    if csz is None:
        return out

    # CSZ box (green)
    cv2.rectangle(out,
                  (csz["x_left"] - 6, csz["y_top"] - 6),
                  (csz["x_right"] + 6, csz["y_bot"] + 6),
                  CSZ_GREEN, RECT_THICK)
    csz_label = f"CSZ: {csz['csz_city']}, {csz['csz_state']} {csz['csz_zip']}"
    _put(out, csz_label, (csz["x_right"] + 14, csz["y_bot"]),
         CSZ_GREEN, scale=0.75)

    # Walked lines (cyan), numbered U1, U2, U3 closest-to-CSZ outward.
    above = [ln for ln in address_lines if ln is not csz]
    above.sort(key=lambda ln: ln["y_top"], reverse=True)  # closest-to-CSZ first
    for i, ln in enumerate(above, 1):
        cv2.rectangle(out,
                      (ln["x_left"] - 6, ln["y_top"] - 6),
                      (ln["x_right"] + 6, ln["y_bot"] + 6),
                      CYAN, WALK_RECT_THICK)
        label = f"U{i}: {_line_text(ln)[:55]}"
        _put(out, label, (ln["x_right"] + 14, ln["y_bot"]),
             CYAN, scale=0.65)

    # Final orange box around all captured address lines. Clip x_right to
    # the column divider (when known) so the box doesn't overshoot into
    # column 2 when an OCR line accidentally swept up trailing fragments
    # like the 't' in 'VICTORVILLE FCI 1 t' (a column-2 'Trailer' bleed).
    if address_lines:
        ux1 = min(ln["x_left"] for ln in address_lines) - 12
        ux2 = max(ln["x_right"] for ln in address_lines) + 12
        if header_anchors is not None and header_anchors.get("divider_x") is not None:
            ux2 = min(ux2, header_anchors["divider_x"])
        uy1 = min(ln["y_top"] for ln in address_lines) - 12
        uy2 = max(ln["y_bot"] for ln in address_lines) + 12
        cv2.rectangle(out, (ux1, uy1), (ux2, uy2), ORANGE, FINAL_RECT_THICK)
        _put(out, f"FINAL CROP {ux2-ux1}x{uy2-uy1}px",
             (ux1, max(28, uy1 - 14)), ORANGE, scale=0.85)

    # Stop marker.
    if stop_at is not None:
        sy = (stop_at["y_top"] + stop_at["y_bot"]) // 2
    else:
        # Walker exhausted steps without a specific stop line; mark just
        # above the topmost included line.
        sy = max(20, min(ln["y_top"] for ln in address_lines) - 25)
    _draw_dashed_hline(out, sy, RED, thick=3)
    _put(out, f"STOP: {stop_reason}", (15, max(22, sy - 12)),
         RED, scale=0.7)

    return out


# ---------- Pipeline ----------
def process(pdf_path: Path, out_dir: Path,
            cfg: ExtractConfig = DEFAULT_CONFIG) -> tuple[Optional[Path], str]:
    img = render_pdf_page(pdf_path, page_index=1, dpi=cfg.dpi)
    img = crop_to_document(img)
    h, w = img.shape[:2]

    # Step 1: bounds.
    full_lines = ocr_lines_with_sparse(img)
    upper_sigs = find_anchor_signals(full_lines, UPPER_TARGET, "BOLN",
                                     cfg.bounds_fuzz_threshold)
    lower_sigs = find_anchor_signals(full_lines, LOWER_TARGET, "HBF",
                                     cfg.bounds_fuzz_threshold)
    upper_y = _aggregate(upper_sigs)
    lower_y = _aggregate(lower_sigs)
    if upper_y is None:
        header = find_header_fallback(full_lines)
        if header is not None:
            upper_y = header[0] + HEADER_PAD_PX
    if upper_y is None or lower_y is None:
        return None, f"FAIL: bounds incomplete upper={upper_y} lower={lower_y}"
    mid_y = (upper_y + lower_y) // 2

    # Step 2a: detect column 1 / column 2 divider via the page header
    # words SHORT and FORM. We do this BEFORE the ROI OCR so we can use
    # the per-BOL divider as the ROI's right edge.
    header_anchors = find_header_anchors(img)
    divider_x = header_anchors.get("divider_x")
    base_right = divider_x if divider_x is not None else (w // 2)

    # Step 2b: focused OCR of the SHIP TO ROI.
    roi_x1 = 0
    roi_x2 = min(w, base_right + cfg.roi_pad_right)
    roi_y1 = max(0, mid_y - cfg.roi_pad_top)
    roi_y2 = min(h, lower_y + cfg.roi_pad_bottom)
    roi_lines = _ocr_roi(img, roi_x1, roi_y1, roi_x2, roi_y2)
    roi_lines = dedupe_psm_duplicates(roi_lines, cfg)

    # Step 3: CSZ anchor.
    csz = find_csz_line(roi_lines, roi_y1, roi_y2)

    # Step 4: walk up.
    address: list[dict] = []
    stop_reason = "no CSZ found"
    stop_at: Optional[dict] = None
    if csz is not None:
        address, stop_reason, stop_at = walk_up_from_csz(roi_lines, csz, mid_y, cfg)

    annotated = annotate(img, mid_y, lower_y, upper_y, csz,
                         address, stop_reason, stop_at, header_anchors)
    out_path = out_dir / f"{pdf_path.stem}_extract.png"
    cv2.imwrite(str(out_path), annotated)

    short = header_anchors["short"]
    form = header_anchors["form"]
    divider = header_anchors["divider_x"]
    anchor_str = (
        f"  SHORT x_right={short['x_right'] if short else 'NF'}  "
        f"FORM x_left={form['x_left'] if form else 'NF'}  "
        f"DIVIDER={divider} (w/2={w//2}"
        + (f", delta={divider - w//2:+d}" if divider is not None else "")
        + ")"
    )

    if csz is None:
        return out_path, f"NO CSZ found in ROI y=[{roi_y1},{roi_y2}]\n{anchor_str}"

    line_strs = [f"  {i+1}. y=[{ln['y_top']:4d}-{ln['y_bot']:4d}]  {_line_text(ln)!r}"
                 for i, ln in enumerate(address)]
    summary = (f"CSZ {csz['csz_city']!r}, {csz['csz_state']} {csz['csz_zip']}  "
               f"({len(address)} address lines, stop: {stop_reason})")
    return out_path, summary + "\n" + "\n".join(line_strs) + "\n" + anchor_str


def _build_config_from_args(args: argparse.Namespace) -> ExtractConfig:
    """Build an ExtractConfig from CLI overrides, falling back to defaults
    for anything not specified."""
    overrides = {
        k: v for k, v in {
            "dpi": args.dpi,
            "bounds_fuzz_threshold": args.bounds_fuzz_threshold,
            "roi_pad_right": args.roi_pad_right,
            "roi_pad_top": args.roi_pad_top,
            "roi_pad_bottom": args.roi_pad_bottom,
            "max_lines_above_csz": args.max_lines_above_csz,
            "boundary_fuzz": args.boundary_fuzz,
            "psm_dup_text_sim": args.psm_dup_text_sim,
        }.items() if v is not None
    }
    return replace(DEFAULT_CONFIG, **overrides) if overrides else DEFAULT_CONFIG


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--out-dir", type=Path, default=Path("shipto_bounds"))
    ap.add_argument("--no-wipe", action="store_true",
                    help="Do not wipe out-dir at start (default wipes)")

    # Config overrides. All default to None -> falls back to DEFAULT_CONFIG.
    cfg = ap.add_argument_group("config overrides (defaults from ExtractConfig)")
    cfg.add_argument("--dpi", type=int, default=None)
    cfg.add_argument("--bounds-fuzz-threshold", type=int, default=None,
                     help="Fuzz threshold for BOLN/HBF anchor detection")
    cfg.add_argument("--roi-pad-right", type=int, default=None,
                     help="px past w/2 for ROI right edge (negative = inside col 1)")
    cfg.add_argument("--roi-pad-top", type=int, default=None)
    cfg.add_argument("--roi-pad-bottom", type=int, default=None)
    cfg.add_argument("--max-lines-above-csz", type=int, default=None)
    cfg.add_argument("--boundary-fuzz", type=int, default=None,
                     help="partial_ratio threshold for boundary phrase match")
    cfg.add_argument("--psm-dup-text-sim", type=int, default=None,
                     help="token_set_ratio threshold for PSM-duplicate dedupe")
    args = ap.parse_args(argv)

    config = _build_config_from_args(args)

    if args.out_dir.exists() and not args.no_wipe:
        shutil.rmtree(args.out_dir)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    ok = 0
    for pdf in args.pdfs:
        try:
            out_path, note = process(pdf, args.out_dir, config)
        except Exception as e:
            print(f"{pdf}: ERROR {e}")
            continue
        if out_path is not None:
            ok += 1
        print(f"=== {pdf.stem} ===\n{note}")
    print(f"\n{ok}/{len(args.pdfs)} processed (output dir: {args.out_dir}/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
