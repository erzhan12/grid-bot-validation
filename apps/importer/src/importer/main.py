"""Importer CLI entry point (feature 0093).

Usage:
    python -m importer.main --source db --source-url sqlite:///ticker.db \
        --symbols BTCUSDT,ETHUSDT [--start ...] [--end ...] [--validate]

Per symbol: acquire the .importlock sidecar, open/create the per-symbol
output DB, resume-append new source rows (per-batch commit), refresh the
single synthetic recording run row, print the density report, optionally
validate. Symbols fail independently; the process exit code is non-zero if
ANY symbol failed (aggregate, not fail-fast).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from typing import Optional

from importer.config import build_parser
from importer.density import compute_density, log_density_report
from importer.mapping import FallbackCounters, map_row
from importer.output_db import (
    ImportLockHeldError,
    acquire_lock,
    ensure_parents,
    ensure_run_row,
    get_min_ts,
    get_resume_ts,
    insert_batch,
    open_output_db,
    output_db_path,
    release_lock,
)
from importer.source import SourceTransport, make_source
from importer.validate import run_validation

logger = logging.getLogger(__name__)


def setup_logging(debug: bool = False) -> None:
    """Set up logging with console output."""
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    console_formatter = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(console_formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG if debug else logging.INFO)
    root_logger.addHandler(console_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _resolve_window(
    args: argparse.Namespace, source: SourceTransport, symbol: str
) -> Optional[tuple[datetime, datetime]]:
    """Resolve the [start, end] import window (both inclusive).

    Missing bounds default to the source MIN/MAX probe. The HTTP transport
    cannot probe — explicit ``--start``/``--end`` are required there.
    """
    start, end = args.start, args.end
    if start is None or end is None:
        probed = source.probe_range(symbol)
        if probed is None:
            logger.error(
                "%s: cannot default --start/--end (source probe unavailable "
                "or empty) — pass explicit bounds",
                symbol,
            )
            return None
        start = start if start is not None else probed[0]
        end = end if end is not None else probed[1]
    if start > end:
        logger.error("%s: --start %s is after --end %s", symbol, start, end)
        return None
    return start, end


def import_symbol(
    args: argparse.Namespace, source: SourceTransport, symbol: str
) -> bool:
    """Import one symbol into its output DB; returns success."""
    db_path = output_db_path(args.out_dir, symbol, args.tag)
    lock = acquire_lock(db_path)
    try:
        db = open_output_db(db_path)
        ensure_parents(db, symbol)

        window = _resolve_window(args, source, symbol)
        if window is None:
            return False
        start, end = window

        # Prefix guard: resume is append-only. A resolved start earlier than
        # the DB's existing MIN can never be filled by this path — abort
        # rather than silently report coverage that was never imported.
        existing_min = get_min_ts(db, symbol)
        if existing_min is not None and start < existing_min:
            logger.error(
                "%s: resolved start %s predates existing MIN(exchange_ts) %s "
                "in %s — resume is append-only. Use --tag for a separate "
                "full-range file (or delete the DB) instead.",
                symbol,
                start,
                existing_min,
                db_path,
            )
            return False

        resume_from = get_resume_ts(db, symbol)
        # First import has no resume cursor — a literal max(start, None)
        # would raise.
        lower = start if resume_from is None else max(start, resume_from)

        counters = FallbackCounters()
        total_inserted = 0
        last_logged_day = None
        for batch in source.fetch_batches(symbol, lower, end):
            snapshots = []
            for row in batch:
                # Resume skip BEFORE mapping (naive-vs-naive comparison —
                # transports already normalized timestamps to naive UTC).
                if resume_from is not None and row["timestamp"] <= resume_from:
                    continue
                snapshot = map_row(row, counters)
                if snapshot is not None:
                    snapshots.append(snapshot)
            total_inserted += insert_batch(db, snapshots)
            day = batch[-1]["timestamp"].date()
            if day != last_logged_day:
                logger.info(
                    "%s: imported through %s (%d rows total)",
                    symbol,
                    day,
                    total_inserted,
                )
                last_logged_day = day

        fallback_counts = counters.as_dict()
        if any(fallback_counts.values()):
            logger.warning("%s NULL-fallback/skip counts: %s", symbol, fallback_counts)

        run_id = ensure_run_row(
            db, symbol, source_desc=f"{args.source}:{args.source_url}"
        )
        if run_id is None:
            # Zero-row rule: empty range or every row skipped for NULL
            # last_price — no run row (replay would abort on an invalid
            # range), symbol counts as failed.
            logger.error(
                "%s: output DB has no ticker rows — no run row created", symbol
            )
            return False
        logger.info(
            "%s: %d new rows this session; run row %s refreshed from DB-wide "
            "MIN/MAX",
            symbol,
            total_inserted,
            run_id,
        )

        days = compute_density(db, symbol)
        log_density_report(symbol, days)

        if args.validate:
            bounds = (get_min_ts(db, symbol), get_resume_ts(db, symbol))
            return run_validation(
                db,
                db_path,
                symbol,
                args.source,
                args.source_url,
                bounds,
                args.ohlc_threshold,
                args.recorder_db,
            )
        return True
    finally:
        release_lock(lock)


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point; returns non-zero when ANY symbol failed."""
    args = build_parser().parse_args(argv)
    setup_logging(debug=args.debug)

    source = make_source(args.source, args.source_url, args.batch_size)

    failed: list[str] = []
    for symbol in args.symbols:
        try:
            ok = import_symbol(args, source, symbol)
        except ImportLockHeldError as e:
            logger.error("%s: %s", symbol, e)
            ok = False
        except Exception:
            logger.error("%s: import failed", symbol, exc_info=True)
            ok = False
        if not ok:
            failed.append(symbol)

    if failed:
        logger.error("FAILED symbols: %s", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
