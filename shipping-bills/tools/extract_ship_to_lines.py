#!/usr/bin/env python3
"""
CLI test program for the BOL SHIP TO extractor.

Thin wrapper over hbf_shipping.bol_ship_to.extract_ship_to(). For each PDF,
runs the extractor with the chosen shipper profile and prints the
diagnostic summary; writes annotated PNGs to --out-dir.

Usage:
    python tools/extract_ship_to_lines.py tests/fixtures/badger/*.pdf
    python tools/extract_ship_to_lines.py --profile badger *.pdf
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from hbf_shipping.bol_ship_to import (  # noqa: E402
    extract_ship_to, BolProfile, BADGER_PROFILE, ExtractConfig, DEFAULT_CONFIG,
    ShipToResult,
)


PROFILES: dict[str, BolProfile] = {
    "badger": BADGER_PROFILE,
}


def _result_to_json_dict(r: ShipToResult) -> dict:
    return {
        "pdf_path": str(r.pdf_path),
        "success": r.success,
        "failure_reason": r.failure_reason,
        "consignee_name": r.consignee_name,
        "address": (dict(r.address._asdict()) if r.address else None),
        "csz_line": r.csz_line,
        "raw_lines": list(r.raw_lines),
        "diagnostic_path": (str(r.diagnostic_path) if r.diagnostic_path else None),
        "diagnostics": r.diagnostics,
    }


def _build_config_from_args(args: argparse.Namespace) -> ExtractConfig:
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
    ap.add_argument("--profile", choices=sorted(PROFILES), default="badger",
                    help="Shipper BOL profile (default: badger)")
    ap.add_argument("--json", action="store_true",
                    help="Emit a pretty-printed JSON array of results "
                         "instead of the per-PDF text summary")

    cfg = ap.add_argument_group("config overrides (defaults from ExtractConfig)")
    cfg.add_argument("--dpi", type=int, default=None)
    cfg.add_argument("--bounds-fuzz-threshold", type=int, default=None,
                     help="Fuzz threshold for upper/lower anchor detection")
    cfg.add_argument("--roi-pad-right", type=int, default=None,
                     help="px past divider_x for ROI right edge")
    cfg.add_argument("--roi-pad-top", type=int, default=None)
    cfg.add_argument("--roi-pad-bottom", type=int, default=None)
    cfg.add_argument("--max-lines-above-csz", type=int, default=None)
    cfg.add_argument("--boundary-fuzz", type=int, default=None,
                     help="partial_ratio threshold for boundary phrase match")
    cfg.add_argument("--psm-dup-text-sim", type=int, default=None,
                     help="token_set_ratio threshold for PSM-duplicate dedupe")

    args = ap.parse_args(argv)
    profile = PROFILES[args.profile]
    config = _build_config_from_args(args)

    if args.out_dir.exists() and not args.no_wipe:
        shutil.rmtree(args.out_dir)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.json:
        out: list[dict] = []
        for pdf in args.pdfs:
            try:
                result = extract_ship_to(pdf, profile=profile, config=config,
                                         diagnostic_dir=args.out_dir)
            except Exception as e:
                out.append({
                    "pdf_path": str(pdf),
                    "success": False,
                    "failure_reason": f"exception: {e}",
                })
                continue
            out.append(_result_to_json_dict(result))
        print(json.dumps(out, indent=2))
        return 0

    ok = 0
    for pdf in args.pdfs:
        try:
            result = extract_ship_to(pdf, profile=profile, config=config,
                                     diagnostic_dir=args.out_dir)
        except Exception as e:
            print(f"{pdf}: ERROR {e}")
            continue
        if result.diagnostic_path is not None:
            ok += 1
        print(f"=== {pdf.stem} ===\n{result.diagnostics}")

    print(f"\n{ok}/{len(args.pdfs)} processed (output dir: {args.out_dir}/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
