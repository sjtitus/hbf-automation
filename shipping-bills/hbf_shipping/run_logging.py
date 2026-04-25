"""
Per-run logging configuration.

A "run" is one invocation of the pipeline. For each run we create a
self-contained directory:

    logs/<run-id>/
        run.log              # INFO+ run-wide log (mirrors stdout)
        <invoice-stem>.log   # DEBUG+ per-invoice log, opened only while that
                             # invoice is being processed
        summary.csv          # one row per invoice (status, fields, log path)
        manifest.json        # machine-readable index of artifacts

run-id format:
    <vendor>-YYYY-MM-DDTHH-MM-SSET-XXXXXX
    e.g. badger-2026-04-25T09-30-45ET-a3f9c1

The timestamp is wall-clock US Eastern (zoneinfo handles EDT/EST). The
literal "ET" suffix makes the timezone unambiguous for humans glancing at
`ls logs/`. The 6-hex random tail makes same-second collisions effectively
impossible (~16M values per vendor per second). The format sorts
lexicographically into chronological order.

Per-invoice files are written through a flushing handler so partial output
survives a mid-run crash. The shape is intentionally cloud-friendly: the
run directory IS the artifact bundle for one run.
"""

from __future__ import annotations

import json
import logging
import secrets
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo


LOG_ROOT = Path('logs')

_ET = ZoneInfo('America/New_York')

_RUN_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'
_INVOICE_FORMAT = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'

# Third-party loggers that emit copious DEBUG noise (image-format internals,
# tesseract subprocess args, PDF parsing). Silenced so per-invoice DEBUG logs
# stay focused on our own decision points.
_NOISY_LOGGERS = ('PIL', 'pypdf', 'pytesseract')


def _generate_run_id(vendor_slug: str) -> str:
    """Build the run identifier — see module docstring for the format."""
    now = datetime.now(_ET)
    stamp = now.strftime('%Y-%m-%dT%H-%M-%S')
    suffix = secrets.token_hex(3)
    return f'{vendor_slug}-{stamp}ET-{suffix}'


class _FlushFileHandler(logging.FileHandler):
    """FileHandler that flushes after every record.

    Default FileHandler buffers stdio writes; a crash mid-invoice can lose
    the most recent records. The cost of flushing every emit is negligible
    at our volume and the safety win is large.
    """

    def emit(self, record):
        super().emit(record)
        self.flush()


def setup_run(vendor_slug: str) -> tuple[str, Path]:
    """Configure the root logger and create the run directory.

    Returns (run_id, run_dir). Adds two handlers to the root logger:
      * StreamHandler(stdout) at INFO
      * _FlushFileHandler(<run_dir>/run.log) at INFO
    The root logger itself is set to DEBUG so per-invoice handlers (added
    later by `invoice_logger`) can capture richer detail without being
    filtered upstream.
    """
    run_id = _generate_run_id(vendor_slug)
    run_dir = LOG_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_RUN_FORMAT)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    stream_h = logging.StreamHandler(sys.stdout)
    stream_h.setLevel(logging.INFO)
    stream_h.setFormatter(formatter)
    root.addHandler(stream_h)

    run_log_h = _FlushFileHandler(run_dir / 'run.log')
    run_log_h.setLevel(logging.INFO)
    run_log_h.setFormatter(formatter)
    root.addHandler(run_log_h)

    return run_id, run_dir


@contextmanager
def invoice_logger(run_dir: Path, invoice_stem: str) -> Iterator[Path]:
    """Attach a DEBUG-level _FlushFileHandler scoped to one invoice.

    Yields the log file's path so the caller can record it in the summary
    CSV. The handler is removed (and its file closed) on exit.
    """
    log_path = run_dir / f'{invoice_stem}.log'
    handler = _FlushFileHandler(log_path)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_INVOICE_FORMAT))

    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield log_path
    finally:
        handler.close()
        root.removeHandler(handler)


def write_manifest(run_dir: Path, payload: dict) -> Path:
    """Dump manifest.json into the run dir. Returns the path."""
    path = run_dir / 'manifest.json'
    path.write_text(json.dumps(payload, indent=2))
    return path
