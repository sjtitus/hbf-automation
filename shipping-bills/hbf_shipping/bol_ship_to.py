"""
Shipper-agnostic SHIP TO address extractor for vendor BOL (Bill of Lading) images.

Renders page 2 of an invoice PDF, OCRs the SHIP TO block, walks up from the
city/state/zip line to capture address lines, normalizes the result through
the standard USPS pipeline (Pub 28 §354 character cleanup -> scourgify), and
returns a normalized Address ready to consume.

Per-shipper specifics (anchor strings, header fallback target, column-divider
words, boundary phrases that stop the walker) are carried in BolProfile.
BADGER_PROFILE preserves the calibrated behavior we tuned against the 14
Badger fixtures. Future shippers (Scotlyn, MRS) get their own *_PROFILE
constants in this file; the extractor itself is generic.

Public surface:
    extract_ship_to(pdf_path, *, profile=BADGER_PROFILE, config=DEFAULT_CONFIG,
                    diagnostic_dir=None) -> ShipToResult
    ShipToResult, BolProfile, BADGER_PROFILE, ExtractConfig, DEFAULT_CONFIG
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import pytesseract
from rapidfuzz import fuzz

# The OCR primitives currently live under tools/. Adding tools/ to sys.path
# keeps them in their original location so existing tools/ scripts continue
# to work; promotion to a proper package module is a future cleanup.
_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from crop_ship_to import (  # noqa: E402
    render_pdf_page, crop_to_document, _binarize, _ocr_pass,
)
from find_ship_to_bounds import (  # noqa: E402
    ocr_lines_with_sparse, find_anchor_signals, _aggregate, _line_text,
    DEFAULT_FUZZ_THRESHOLD,
)
from hbf_shipping.customer_address_map import (  # noqa: E402
    Address, _normalize_address,
)


# ============================================================
# PER-SHIPPER LAYOUT (BolProfile)
# Strings and regexes that describe a particular shipper's BOL form. The
# extractor reads from a profile instead of module-level constants so the
# same code handles Badger today and other shippers tomorrow.
# ============================================================

DEFAULT_BOUNDARY_PHRASES: tuple[str, ...] = (
    "ship to", "ship from", "carrier name",
    "trailer number", "serial number", "bill of lading number",
    "bar code",
)


@dataclass(frozen=True)
class BolProfile:
    name: str
    # Anchors that flank the SHIP TO block vertically. The walker uses
    # find_anchor_signals (rapidfuzz partial_ratio) to locate these phrases
    # in the OCR'd page-2 image.
    upper_anchor_target: str       # e.g. "bill of lading number"
    upper_anchor_short: str        # short label for the diagnostic summary
    lower_anchor_target: str       # e.g. "highland beef farms"
    lower_anchor_short: str
    # Header fallback: when the upper anchor can't be matched (poor scan),
    # locate the page-header phrase and use its bottom + pad as a proxy for
    # the upper bound. Set header_fallback_target=None to disable.
    header_fallback_target: Optional[str]
    header_fallback_fuzz_threshold: int
    header_fallback_pad_px: int
    # Column 1 / column 2 divider: when the BOL has a two-column layout, we
    # detect it by finding two header words and using their midpoint as the
    # ROI's right edge. Set to None for single-column BOLs.
    divider_header_words: Optional[tuple[str, str]]
    # Phrases that stop the upward walker (form labels that appear above the
    # SHIP TO block).
    boundary_phrases: tuple[str, ...]


BADGER_PROFILE = BolProfile(
    name="badger",
    upper_anchor_target="bill of lading number",
    upper_anchor_short="BOLN",
    lower_anchor_target="highland beef farms",
    lower_anchor_short="HBF",
    header_fallback_target="bill of lading short form",
    header_fallback_fuzz_threshold=75,
    header_fallback_pad_px=30,
    divider_header_words=("SHORT", "FORM"),
    boundary_phrases=DEFAULT_BOUNDARY_PHRASES,
)


# ============================================================
# WALKER / OCR TUNING (ExtractConfig)
# Generic knobs that govern walker behavior, OCR ROI sizing, dedupe
# thresholds, etc. Not shipper-specific.
# ============================================================

@dataclass(frozen=True)
class ExtractConfig:
    # --- Render ---
    dpi: int = 300

    # --- Bounds detection ---
    bounds_fuzz_threshold: int = DEFAULT_FUZZ_THRESHOLD

    # --- ROI (focused OCR window for the SHIP TO area) ---
    # x_right is computed as `divider_x + roi_pad_right`, where divider_x
    # comes from the per-BOL column divider (profile.divider_header_words)
    # and falls back to `w/2`. +60 of gutter pad past the divider gives
    # tesseract enough right-margin context that it doesn't re-segment
    # column-1 lines differently; calibrated empirically (pad=0 dropped
    # 'Vistar Retail West' on 0065560; +60 recovers it without consistently
    # grabbing column-2 text).
    roi_pad_top: int = 50
    roi_pad_bottom: int = 50
    roi_pad_right: int = 60

    # --- PSM-duplicate dedupe (collapse same-row OCR variants) ---
    psm_dup_text_sim: int = 75
    cluster_min_text_len_frac: float = 0.8

    # --- Stride-up walker ---
    max_lines_above_csz: int = 4
    initial_stride_pad: int = 8
    stride_tolerance_min: int = 15
    stride_lock_min: int = 30
    stride_lock_max: int = 60

    # --- Boundary phrase matching (phrases come from BolProfile) ---
    boundary_fuzz: int = 70
    boundary_min_len_frac: float = 0.7

    # --- Address content classifier ---
    min_alnum_for_content: int = 3
    address_word_min_len: int = 5


DEFAULT_CONFIG = ExtractConfig()


# ============================================================
# Diagnostic image annotation styling (cosmetic, not configurable)
# ============================================================
RED = (0, 0, 220)
MID_GREEN = (0, 200, 0)
CSZ_GREEN = (40, 220, 80)
CYAN = (255, 220, 0)
ORANGE = (0, 140, 255)
RECT_THICK = 4
WALK_RECT_THICK = 3
FINAL_RECT_THICK = 5
LINE_THICK = 3
TEXT_FONT = cv2.FONT_HERSHEY_SIMPLEX


# ============================================================
# CSZ pattern + content classifiers
# ============================================================

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

_WORD_RE = re.compile(r"[A-Za-z]+")
_ATTN_RE = re.compile(r"\battn", re.IGNORECASE)
_POBOX_RE = re.compile(r"\bp\.?\s*o\.?\s*box\b", re.IGNORECASE)


def is_address_content(text: str, cfg: ExtractConfig = DEFAULT_CONFIG) -> bool:
    """True if `text` looks like a real address line. Requires AT LEAST
    cfg.min_alnum_for_content alphanumeric chars total, AND at least one of:
        - any digit (street number, suite, zip, phone)
        - 'Attn' (case-insensitive)
        - 'P.O. Box' / 'PO Box' pattern
        - a word of length >= cfg.address_word_min_len that is either
          capitalized (first letter upper, rest lower) OR all-uppercase

    The cap/upper-word rule is the noise filter. Real address lines almost
    always contain a 5+ letter "real" word ('Foods', 'Vaughn', 'TUCSON');
    BOL header decorative band noise has only short cap-words ('Sati',
    'Roca') or all-lowercase clumps ('torent', 'caaik')."""
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


def matches_boundary(text: str, profile: BolProfile,
                     cfg: ExtractConfig = DEFAULT_CONFIG) -> Optional[str]:
    """Return the matched boundary phrase (lowercase) if `text` is one of
    the profile's boundary phrases, else None. Uses partial_ratio with a
    length floor so a one-char OCR fragment doesn't accidentally score 100
    against a multi-word phrase."""
    low = text.lower().strip()
    if not low:
        return None
    for p in profile.boundary_phrases:
        if len(low) < len(p) * cfg.boundary_min_len_frac:
            continue
        if fuzz.partial_ratio(p, low) >= cfg.boundary_fuzz:
            return p
    return None


# ============================================================
# Header anchor (column divider) detection
# ============================================================

def _find_header_word(img: np.ndarray, target: str,
                      threshold: int = 75) -> Optional[dict]:
    """Word-level OCR the top of the page; return the topmost word that
    fuzz-matches `target`. Returns dict with x_left, x_right, y_top, text,
    score; or None if no match."""
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


def find_header_anchors(img: np.ndarray, profile: BolProfile) -> dict:
    """Return {'left': dict|None, 'right': dict|None, 'divider_x': int|None}
    for the column-1/column-2 divider, derived from the two header words in
    profile.divider_header_words. Returns all-None if the profile has no
    divider configured."""
    if profile.divider_header_words is None:
        return {"left": None, "right": None, "divider_x": None}
    left_target, right_target = profile.divider_header_words
    left = _find_header_word(img, left_target)
    right = _find_header_word(img, right_target)
    if left is not None and right is not None:
        divider = (left["x_right"] + right["x_left"]) // 2
    elif right is not None:
        divider = right["x_left"]
    elif left is not None:
        divider = left["x_right"]
    else:
        divider = None
    return {"left": left, "right": right, "divider_x": divider}


def _find_header_fallback(lines: list[dict],
                          target: str,
                          fuzz_threshold: int) -> Optional[tuple[int, int, int]]:
    """When the upper anchor can't be matched, fall back to the page-header
    phrase. Returns (avg_y_bot, avg_score, count) or None.

    Uses y_bot (not y_top) because the synthesized upper bound should be
    BELOW the header, near where the upper anchor would have been on a
    clean scan."""
    min_len = int(len(target) * 0.7)  # header OCR drifts more than discrete anchors
    matches: list[tuple[int, int]] = []
    for ln in lines:
        text = _line_text(ln).lower()
        if len(text) < min_len:
            continue
        score = int(fuzz.partial_ratio(target, text))
        if score >= fuzz_threshold:
            matches.append((ln["y_bot"], score))
    if not matches:
        return None
    avg_y_bot = int(round(sum(y for y, _ in matches) / len(matches)))
    avg_score = int(round(sum(s for _, s in matches) / len(matches)))
    return (avg_y_bot, avg_score, len(matches))


# ============================================================
# OCR
# ============================================================

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
    Two lines are 'the same row' iff their y-ranges overlap AND their text
    token_set_ratio is >= cfg.psm_dup_text_sim.

    Checks ALL existing clusters whose members y-overlap with the candidate
    line, not just the most recent one. Otherwise PSM duplicates of the
    same row split into separate clusters when noise lines (with different
    text) at intermediate y_top values fall between them."""
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
        # tightest bbox -- among versions with equally complete text, the
        # cleanest box wins (avoids PSM 11's habit of inflating y_bot into
        # the next row, which moves the line out of the walker's reach).
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


# ============================================================
# CSZ anchor + walker
# ============================================================

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


def walk_up_from_csz(roi_lines: list[dict], csz_line: dict, mid_y: int,
                     profile: BolProfile,
                     cfg: ExtractConfig = DEFAULT_CONFIG
                     ) -> tuple[list[dict], str, Optional[dict]]:
    """Walk up from CSZ one line at a time. Return (address_lines_in_order,
    stop_reason, stop_at_line). Stride is initially CSZ height + pad, then
    locks to the actual observed stride after the first successful hop."""
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
        # only members are short OCR fragments. Without this, a junk 1-3
        # char fragment ('Tr', 'ast', 'oh') that lands near the target y
        # beats the real address line a few px away and stops the walk
        # prematurely.
        content_groups = [
            g for g in groups
            if any(is_address_content(_line_text(ln), cfg) for ln in g)
            and not any(matches_boundary(_line_text(ln), profile, cfg) for ln in g)
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

        b = matches_boundary(text, profile, cfg)
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


# ============================================================
# Diagnostic annotation (PNG)
# ============================================================

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


def _annotate(img: np.ndarray, mid_y: int, lower_y: int, upper_y: int,
              csz: Optional[dict], address_lines: list[dict],
              stop_reason: str, stop_at: Optional[dict],
              header_anchors: Optional[dict] = None) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]

    _draw_full_width_line(out, upper_y, RED)
    _put(out, f"UPPER y={upper_y}", (12, max(22, upper_y - 10)), RED, scale=0.7)
    _draw_full_width_line(out, lower_y, RED)
    _put(out, f"LOWER y={lower_y}", (12, max(22, lower_y - 10)), RED, scale=0.7)
    _draw_full_width_line(out, mid_y, MID_GREEN)
    _put(out, f"MID y={mid_y}", (12, max(22, mid_y - 10)), MID_GREEN, scale=0.7)

    cv2.line(out, (w // 2, 0), (w // 2, h - 1), (180, 180, 180), 1, cv2.LINE_AA)
    _put(out, f"w/2={w//2}", (w // 2 + 6, 30), (140, 140, 140), scale=0.7)

    if header_anchors is not None:
        left = header_anchors.get("left")
        right = header_anchors.get("right")
        divider = header_anchors.get("divider_x")
        BLUE = (220, 100, 0)
        MAGENTA = (220, 0, 220)
        YELLOW_THICK = (0, 220, 255)
        if left is not None:
            sx = left["x_right"]
            cv2.line(out, (sx, 0), (sx, h - 1), BLUE, 2, cv2.LINE_AA)
            _put(out, f"SHORT end x={sx}", (max(12, sx - 280), 90),
                 BLUE, scale=0.7)
        if right is not None:
            fx = right["x_left"]
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

    cv2.rectangle(out,
                  (csz["x_left"] - 6, csz["y_top"] - 6),
                  (csz["x_right"] + 6, csz["y_bot"] + 6),
                  CSZ_GREEN, RECT_THICK)
    csz_label = f"CSZ: {csz['csz_city']}, {csz['csz_state']} {csz['csz_zip']}"
    _put(out, csz_label, (csz["x_right"] + 14, csz["y_bot"]),
         CSZ_GREEN, scale=0.75)

    above = [ln for ln in address_lines if ln is not csz]
    above.sort(key=lambda ln: ln["y_top"], reverse=True)
    for i, ln in enumerate(above, 1):
        cv2.rectangle(out,
                      (ln["x_left"] - 6, ln["y_top"] - 6),
                      (ln["x_right"] + 6, ln["y_bot"] + 6),
                      CYAN, WALK_RECT_THICK)
        label = f"U{i}: {_line_text(ln)[:55]}"
        _put(out, label, (ln["x_right"] + 14, ln["y_bot"]),
             CYAN, scale=0.65)

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

    if stop_at is not None:
        sy = (stop_at["y_top"] + stop_at["y_bot"]) // 2
    else:
        sy = max(20, min(ln["y_top"] for ln in address_lines) - 25)
    _draw_dashed_hline(out, sy, RED, thick=3)
    _put(out, f"STOP: {stop_reason}", (15, max(22, sy - 12)),
         RED, scale=0.7)

    return out


# ============================================================
# USPS Pub 28 §354 character cleanup
# ============================================================

# Whitelist of characters valid in a USPS-style address per Pub 28: letters,
# digits, single space, hyphen (ZIP+4 / primary numbers), slash (rare
# fractional addresses), and pound sign (unit numbers). Anything else --
# the OCR'd inch-mark `"`, stray pipes from column gutters, etc. -- is
# stripped. We don't *delete* tokens; we sanitize characters and let
# scourgify handle abbreviation/parsing of the cleaned string.
_USPS_INVALID_RE = re.compile(r"[^A-Za-z0-9 \-/#]")
_MULTI_SPACE_RE = re.compile(r"\s+")


def _clean_for_usps(s: str) -> str:
    if not s:
        return ""
    s = _USPS_INVALID_RE.sub(" ", s)
    s = _MULTI_SPACE_RE.sub(" ", s)
    return s.strip()


# ============================================================
# Street parsing (locate the street line among captured lines)
# ============================================================

_STREET_DIGIT_RE = re.compile(r"^\s*\d+")
_STREET_POBOX_RE = re.compile(r"^\s*p\.?\s*o\.?\s*box\b", re.IGNORECASE)


def _parse_street_from_address_lines(address_lines: list[dict],
                                     csz_line: dict) -> Optional[str]:
    """From the captured address lines, pick the line that looks like a
    street and return its Pub-28-cleaned text. Heuristic: leading digit
    (street number) or 'P.O. Box' pattern. Excludes the CSZ line. Among
    matches, prefer the one closest to (just above) CSZ."""
    candidates: list[tuple[int, str]] = []
    for ln in address_lines:
        if ln is csz_line:
            continue
        cleaned = _clean_for_usps(_line_text(ln))
        if not cleaned:
            continue
        if _STREET_DIGIT_RE.match(cleaned) or _STREET_POBOX_RE.match(cleaned):
            candidates.append((ln["y_top"], cleaned))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[-1][1]


# ============================================================
# Public API
# ============================================================

@dataclass(frozen=True)
class ShipToResult:
    pdf_path: Path
    success: bool                           # True iff a usable Address was produced
    failure_reason: Optional[str]           # human-readable why-it-failed; None on success
    address: Optional[Address]              # normalized 4-tuple, None on failure
    consignee_name: Optional[str]           # cleaned topmost OCR line above CSZ (facility/company name)
    raw_lines: list[str]                    # captured OCR lines, top-down (CSZ last)
    csz_line: Optional[str]                 # the CSZ row text, e.g. 'Tucson, AZ 85756'
    diagnostic_path: Optional[Path]         # PNG path, only when diagnostic_dir was set
    diagnostics: str                        # multi-line diagnostic dump (walker stop reason, line y-coords, column divider, etc.)


def extract_ship_to(
    pdf_path: Path,
    *,
    profile: BolProfile = BADGER_PROFILE,
    config: ExtractConfig = DEFAULT_CONFIG,
    diagnostic_dir: Optional[Path] = None,
) -> ShipToResult:
    """Extract the SHIP TO address from a vendor BOL on page 2 of `pdf_path`.

    Returns a ShipToResult carrying the normalized Address, the raw captured
    OCR lines for inspection, and (when `diagnostic_dir` is provided) the
    path to an annotated PNG visualizing what the walker found.
    """
    img = render_pdf_page(pdf_path, page_index=1, dpi=config.dpi)
    img = crop_to_document(img)
    h, w = img.shape[:2]

    # Step 1: bounds.
    full_lines = ocr_lines_with_sparse(img)
    upper_sigs = find_anchor_signals(full_lines, profile.upper_anchor_target,
                                     profile.upper_anchor_short,
                                     config.bounds_fuzz_threshold)
    lower_sigs = find_anchor_signals(full_lines, profile.lower_anchor_target,
                                     profile.lower_anchor_short,
                                     config.bounds_fuzz_threshold)
    upper_y = _aggregate(upper_sigs)
    lower_y = _aggregate(lower_sigs)
    if upper_y is None and profile.header_fallback_target is not None:
        header = _find_header_fallback(full_lines,
                                       profile.header_fallback_target,
                                       profile.header_fallback_fuzz_threshold)
        if header is not None:
            upper_y = header[0] + profile.header_fallback_pad_px

    if upper_y is None or lower_y is None:
        upper_t = profile.upper_anchor_target
        lower_t = profile.lower_anchor_target
        if upper_y is None and lower_y is None:
            reason = (
                f"Could not locate the SHIP TO block on the BOL: neither the "
                f"upper anchor ({upper_t!r}) nor the lower anchor "
                f"({lower_t!r}) could be matched in the page-2 OCR output."
            )
        elif upper_y is None:
            reason = (
                f"Could not locate the top of the SHIP TO block: the upper "
                f"anchor ({upper_t!r}) was not matched in OCR. The lower "
                f"anchor ({lower_t!r}) was found at y={lower_y}."
            )
        else:
            reason = (
                f"Could not locate the bottom of the SHIP TO block: the "
                f"lower anchor ({lower_t!r}) was not matched in OCR. The "
                f"upper anchor ({upper_t!r}) was found at y={upper_y}."
            )
        return ShipToResult(
            pdf_path=pdf_path, success=False, failure_reason=reason,
            address=None, consignee_name=None,
            raw_lines=[], csz_line=None,
            diagnostic_path=None,
            diagnostics=f"FAIL: {reason}",
        )

    mid_y = (upper_y + lower_y) // 2

    # Step 2a: column divider via per-BOL header words.
    header_anchors = find_header_anchors(img, profile)
    divider_x = header_anchors.get("divider_x")
    base_right = divider_x if divider_x is not None else (w // 2)

    # Step 2b: focused OCR of the SHIP TO ROI.
    roi_x1 = 0
    roi_x2 = min(w, base_right + config.roi_pad_right)
    roi_y1 = max(0, mid_y - config.roi_pad_top)
    roi_y2 = min(h, lower_y + config.roi_pad_bottom)
    roi_lines = _ocr_roi(img, roi_x1, roi_y1, roi_x2, roi_y2)
    roi_lines = dedupe_psm_duplicates(roi_lines, config)

    # Step 3: CSZ anchor.
    csz = find_csz_line(roi_lines, roi_y1, roi_y2)

    # Step 4: walk up.
    address: list[dict] = []
    stop_reason = "no CSZ found"
    stop_at: Optional[dict] = None
    if csz is not None:
        address, stop_reason, stop_at = walk_up_from_csz(roi_lines, csz, mid_y,
                                                         profile, config)

    # Diagnostic PNG (optional).
    diagnostic_path: Optional[Path] = None
    if diagnostic_dir is not None:
        annotated = _annotate(img, mid_y, lower_y, upper_y, csz,
                              address, stop_reason, stop_at, header_anchors)
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        diagnostic_path = diagnostic_dir / f"{pdf_path.stem}_extract.png"
        cv2.imwrite(str(diagnostic_path), annotated)

    # Anchor diagnostic line, mirroring the original tool's summary.
    left = header_anchors["left"]
    right = header_anchors["right"]
    divider = header_anchors["divider_x"]
    anchor_str = (
        f"  SHORT x_right={left['x_right'] if left else 'NF'}  "
        f"FORM x_left={right['x_left'] if right else 'NF'}  "
        f"DIVIDER={divider} (w/2={w//2}"
        + (f", delta={divider - w//2:+d}" if divider is not None else "")
        + ")"
    )

    if csz is None:
        reason = (
            f"Found the SHIP TO block but no city/state/ZIP line was "
            f"identified inside it (searched y=[{roi_y1}, {roi_y2}]). "
            f"OCR may have garbled the line, or it may sit outside the "
            f"expected vertical range."
        )
        return ShipToResult(
            pdf_path=pdf_path, success=False, failure_reason=reason,
            address=None, consignee_name=None,
            raw_lines=[], csz_line=None,
            diagnostic_path=diagnostic_path,
            diagnostics=f"FAIL: {reason}\n{anchor_str}",
        )

    # Step 5: USPS-normalize.
    street = _parse_street_from_address_lines(address, csz)
    addr: Optional[Address] = None
    if street is not None:
        addr = _normalize_address(street, csz["csz_city"], csz["csz_state"],
                                  csz["csz_zip"])

    raw_lines = [_line_text(ln) for ln in address]
    csz_text = f"{csz['csz_city']}, {csz['csz_state']} {csz['csz_zip']}"

    # Topmost captured line (raw_lines[0]) is conventionally the
    # facility/company name on Badger BOLs; CSZ sits at raw_lines[-1].
    # Only populate when we got at least one line above CSZ.
    consignee_name: Optional[str] = None
    if len(raw_lines) >= 2:
        cleaned = _clean_for_usps(raw_lines[0])
        consignee_name = cleaned or None

    line_strs = [f"  {i+1}. y=[{ln['y_top']:4d}-{ln['y_bot']:4d}]  {_line_text(ln)!r}"
                 for i, ln in enumerate(address)]
    summary = (f"CSZ {csz['csz_city']!r}, {csz['csz_state']} {csz['csz_zip']}  "
               f"({len(address)} address lines, stop: {stop_reason})")
    addr_str = f"\n  ADDRESS  {addr}" if addr is not None else "\n  ADDRESS  None (no street parsed)"

    failure_reason: Optional[str] = None
    if addr is None:
        above_csz = raw_lines[:-1]
        if above_csz:
            failure_reason = (
                f"Found the city/state/ZIP line ({csz_text!r}) but no street "
                f"address line could be identified among the captured lines "
                f"above it: {above_csz}. The walker may have stopped early "
                f"or the OCR did not surface a recognizable street."
            )
        else:
            failure_reason = (
                f"Found the city/state/ZIP line ({csz_text!r}) but no other "
                f"lines were captured above it; cannot extract a street address."
            )

    return ShipToResult(
        pdf_path=pdf_path, success=(addr is not None),
        failure_reason=failure_reason,
        address=addr, consignee_name=consignee_name,
        raw_lines=raw_lines, csz_line=csz_text,
        diagnostic_path=diagnostic_path,
        diagnostics=summary + "\n" + "\n".join(line_strs) + "\n" + anchor_str + addr_str,
    )
