"""CLI argument parsing and datetime discipline for the importer (feature 0093).

Every datetime entering the pipeline is normalized to naive UTC (convert to
UTC, strip tzinfo). SQLite returns naive datetimes, so any aware value that
survives into a comparison against a stored cursor raises ``TypeError`` —
the same guard replay applies before comparing timestamps.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone


def to_naive_utc(dt: datetime) -> datetime:
    """Convert to UTC and strip tzinfo; naive input passes through unchanged.

    Naive datetimes are assumed UTC already (owner-confirmed source
    semantics).
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 datetime string and return it as naive UTC.

    Accepts the trailing-``Z`` shorthand and explicit offsets
    (``+05:00``); aware input is converted to UTC before tzinfo is
    stripped.
    """
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid ISO-8601 datetime: {value!r}"
        ) from e
    return to_naive_utc(dt)


def positive_int(value: str) -> int:
    """argparse type: strictly positive integer (LIMIT 0/-1 are footguns)."""
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def unit_fraction(value: str) -> float:
    """argparse type: float in (0, 1] (0 would disable the OHLC value gate)."""
    number = float(value)
    if not 0 < number <= 1:
        raise argparse.ArgumentTypeError("must be in (0, 1]")
    return number


def parse_symbols(value: str) -> list[str]:
    """Split a comma-separated symbol list; rejects an empty list."""
    symbols = [s.strip().upper() for s in value.split(",") if s.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("--symbols must name at least one symbol")
    return symbols


def build_parser() -> argparse.ArgumentParser:
    """Build the importer CLI parser (invoked via ``python -m importer.main``)."""
    parser = argparse.ArgumentParser(
        prog="python -m importer.main",
        description=(
            "One-way import: trad_save_history ticker_data -> per-symbol "
            "replay-compatible SQLite DBs (feature 0093). Imported DBs are "
            "for counterfactual A/B / relative ranking only."
        ),
    )
    parser.add_argument(
        "--source",
        required=True,
        choices=("db", "http"),
        help="Source transport: direct SQLAlchemy URL (db) or HTTP API (http).",
    )
    parser.add_argument(
        "--source-url",
        required=True,
        help=(
            "Transport A: SQLAlchemy URL (postgresql://... or sqlite:///path). "
            "Transport B: HTTP API base URL."
        ),
    )
    parser.add_argument(
        "--symbols",
        required=True,
        type=parse_symbols,
        help="Comma-separated symbols to import (e.g. BTCUSDT,ETHUSDT).",
    )
    parser.add_argument(
        "--start",
        type=parse_utc,
        default=None,
        help=(
            "ISO-8601 UTC start (inclusive). Default: source MIN(timestamp) "
            "probe (transport A only; http requires explicit bounds)."
        ),
    )
    parser.add_argument(
        "--end",
        type=parse_utc,
        default=None,
        help=(
            "ISO-8601 UTC end (inclusive). Default: source MAX(timestamp) "
            "probe (transport A only; http requires explicit bounds)."
        ),
    )
    parser.add_argument(
        "--out-dir",
        default="data",
        help="Output directory for imported_<symbol>[_<tag>].db files.",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help=(
            "Optional filename tag for an isolated fresh import "
            "(imported_<symbol>_<tag>.db), e.g. while a sweep reads the "
            "default file."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=positive_int,
        default=10000,
        help="Source fetch batch size (default 10000).",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run post-import validation (OHLC cross-check, smoke replay, "
        "recorder overlap probe).",
    )
    parser.add_argument(
        "--ohlc-threshold",
        type=unit_fraction,
        default=0.99,
        help="Fraction of exactly-matching OHLC buckets required to pass "
        "(default 0.99).",
    )
    parser.add_argument(
        "--recorder-db",
        default=None,
        help="Recorder SQLite path/URL for the --validate overlap probe "
        "(skipped with a NOTICE when omitted or no overlap).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser
