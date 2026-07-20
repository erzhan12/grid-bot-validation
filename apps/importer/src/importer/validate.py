"""Post-import ``--validate`` checks (feature 0093).

1. OHLC cross-check — rebuild 1-minute OHLC from imported ticks for a
   sampled day and diff vs the source ``klines`` (1m). Buckets are keyed by
   absolute UTC epoch-minute, so a constant whole-hour tz offset produces
   imported bucket keys OUTSIDE the kline day (hard key-set fail),
   independent of any value drift. Minutes with no imported ticks are
   legitimate (the source writes only on lastPrice change).
2. Smoke replay — one 4 h window, gs0.5, fresh grid, last_cross against the
   imported DB via ``python -m replay.main``; metrics must parse non-NaN.
3. Recorder overlap probe — cross-check overlapping last_price series
   against a recorder DB when one is supplied and coverage overlaps
   (informational; skipped with a NOTICE otherwise).
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

import yaml
from sqlalchemy import DateTime as SaDateTime
from sqlalchemy import MetaData, Table, func, select

from grid_db.database import DatabaseFactory
from grid_db.models import TickerSnapshot
from grid_db.settings import DatabaseSettings

from importer.config import to_naive_utc
from importer.fetch_source_db import aware_utc
from importer.fetch_source_http import iso_utc

logger = logging.getLogger(__name__)

# Pinned per-symbol EXCHANGE tick sizes (plan 0093: seed from exchange
# instrument specs, NOT existing YAMLs — the old LTC YAML 0.1 diverged from
# the exchange 0.01 and caused the 0090 startup abort).
TICK_SIZE = {
    "BTCUSDT": Decimal("0.1"),
    "ETHUSDT": Decimal("0.01"),
    "SOLUSDT": Decimal("0.01"),
    "LTCUSDT": Decimal("0.01"),
}

_SMOKE_WINDOW = timedelta(hours=4)
_OVERLAP_WINDOW = timedelta(hours=1)
_EIGHT_DP = Decimal("0.00000001")


def _epoch_minute(ts: datetime) -> int:
    """Absolute UTC minute key for a naive-UTC timestamp."""
    return int(ts.replace(tzinfo=timezone.utc).timestamp()) // 60


@dataclass
class OhlcBucket:
    """One 1-minute OHLC bucket."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass
class OhlcCheckResult:
    """Outcome of the OHLC cross-check."""

    passed: bool
    extra_keys: List[int] = field(default_factory=list)
    compared: int = 0
    matched: int = 0
    mismatched_keys: List[int] = field(default_factory=list)


def rebuild_ohlc(
    ticks: "list[tuple[datetime, Decimal]]",
) -> dict[int, OhlcBucket]:
    """Rebuild per-minute OHLC buckets keyed by absolute UTC epoch-minute.

    ``ticks`` must already be ordered by timestamp ascending (open = first
    tick of the minute, close = last) — ``_load_ticks`` guarantees this.
    """
    buckets: dict[int, OhlcBucket] = {}
    for ts, price in ticks:
        key = _epoch_minute(ts)
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = OhlcBucket(open=price, high=price, low=price, close=price)
        else:
            bucket.high = max(bucket.high, price)
            bucket.low = min(bucket.low, price)
            bucket.close = price
    return buckets


def compare_ohlc(
    imported: dict[int, OhlcBucket],
    klines: dict[int, OhlcBucket],
    tick_size: Decimal,
    threshold: float,
) -> OhlcCheckResult:
    """Diff imported buckets against source klines.

    Hard-fail (tz detector): any imported bucket key absent from the kline
    key set — a whole-hour offset shifts imported keys outside the kline
    range. Kline minutes with no imported bucket are legitimate sparsity.

    Value check, for keys present in both: open/close by exact Decimal
    equality, high/low within 1 tick_size (intra-minute extremes can be
    dropped by source batching). Passes when the matching fraction reaches
    ``threshold``.
    """
    extra = sorted(set(imported) - set(klines))
    if extra:
        return OhlcCheckResult(passed=False, extra_keys=extra)

    common = sorted(set(imported) & set(klines))
    matched = 0
    mismatched: List[int] = []
    for key in common:
        imp, ref = imported[key], klines[key]
        ok = (
            imp.open == ref.open
            and imp.close == ref.close
            and abs(imp.high - ref.high) <= tick_size
            and abs(imp.low - ref.low) <= tick_size
        )
        if ok:
            matched += 1
        else:
            mismatched.append(key)
    fraction = matched / len(common) if common else 1.0
    return OhlcCheckResult(
        passed=fraction >= threshold,
        compared=len(common),
        matched=matched,
        mismatched_keys=mismatched,
    )


def _coerce_kline_ts(value) -> datetime:
    """Coerce a source kline start value (datetime / ISO string / epoch) to naive UTC."""
    if isinstance(value, datetime):
        return to_naive_utc(value)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
        return to_naive_utc(datetime.fromisoformat(normalized))
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e12 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
    raise ValueError(f"unsupported kline timestamp type: {type(value)!r}")


def _fetch_klines_db(
    source_url: str, symbol: str, start: datetime, end: datetime
) -> dict[int, OhlcBucket]:
    """Fetch 1m klines from the source DB (transport A).

    Reflects the ``klines`` table (issue #232 source schema); expects
    ``symbol``, a start-time column, and open/high/low/close. Filters an
    ``interval`` column to 1m values when present.
    """
    db = DatabaseFactory(DatabaseSettings(database_url=source_url, read_only=True))
    with db.get_readonly_session() as session:
        table = Table("klines", MetaData(), autoload_with=db.engine)
        cols = table.c
        ts_col = next(
            (cols[name] for name in ("start_time", "timestamp", "open_time")
             if name in cols),
            None,
        )
        if ts_col is None:
            raise ValueError("klines table has no recognizable start-time column")
        query = select(
            ts_col, cols["open"], cols["high"], cols["low"], cols["close"]
        ).where(cols["symbol"] == symbol)
        if "interval" in cols:
            query = query.where(cols["interval"].in_(("1", "1m")))
        # Push the day window into SQL when the start-time column is a real
        # datetime — fetching a symbol's full kline history for one sampled
        # day is needlessly heavy. Epoch-integer columns fall back to the
        # Python-side range filter below.
        if isinstance(ts_col.type, SaDateTime):
            query = query.where(
                ts_col >= aware_utc(start), ts_col <= aware_utc(end)
            )
        rows = session.execute(query).all()

    result: dict[int, OhlcBucket] = {}
    for ts_value, o, h, lo, c in rows:
        ts = _coerce_kline_ts(ts_value)
        if not (start <= ts <= end):
            continue
        result[_epoch_minute(ts)] = OhlcBucket(
            open=Decimal(str(o)).quantize(_EIGHT_DP),
            high=Decimal(str(h)).quantize(_EIGHT_DP),
            low=Decimal(str(lo)).quantize(_EIGHT_DP),
            close=Decimal(str(c)).quantize(_EIGHT_DP),
        )
    return result


def _fetch_klines_http(
    base_url: str, symbol: str, start: datetime, end: datetime
) -> dict[int, OhlcBucket]:
    """Fetch 1m klines via ``GET {base}/klines?symbol&start&end`` (transport B)."""
    import requests

    response = requests.get(
        f"{base_url.rstrip('/')}/klines",
        params={
            # Same Z-suffixed ISO format the ticker transport sends.
            "symbol": symbol,
            "start": iso_utc(start),
            "end": iso_utc(end),
        },
        timeout=30,
    )
    response.raise_for_status()
    result: dict[int, OhlcBucket] = {}
    for row in response.json().get("rows") or []:
        ts = _coerce_kline_ts(row["start_time"])
        if not (start <= ts <= end):
            continue
        result[_epoch_minute(ts)] = OhlcBucket(
            open=Decimal(str(row["open"])).quantize(_EIGHT_DP),
            high=Decimal(str(row["high"])).quantize(_EIGHT_DP),
            low=Decimal(str(row["low"])).quantize(_EIGHT_DP),
            close=Decimal(str(row["close"])).quantize(_EIGHT_DP),
        )
    return result


def _pick_sample_day(db: DatabaseFactory, symbol: str, midpoint: datetime):
    """Pick the tick-bearing UTC day nearest the range midpoint.

    The midpoint calendar day itself can be empty (collector outage) —
    sampling the nearest NON-EMPTY day keeps ``--validate`` meaningful
    instead of hard-failing on a gap. Returns None when the symbol has no
    ticks at all.
    """
    with db.get_session() as session:
        day_rows = (
            session.query(func.date(TickerSnapshot.exchange_ts))
            .filter(TickerSnapshot.symbol == symbol)
            .distinct()
            .all()
        )
    days = sorted(
        datetime.strptime(value, "%Y-%m-%d").date() for (value,) in day_rows
    )
    if not days:
        return None
    target = midpoint.date()
    return min(days, key=lambda d: abs(d - target))


def _load_ticks(
    db: DatabaseFactory, symbol: str, start: datetime, end: datetime
) -> List[tuple[datetime, Decimal]]:
    """Load (exchange_ts, last_price) ticks in [start, end] ordered by ts."""
    with db.get_session() as session:
        rows = (
            session.query(TickerSnapshot.exchange_ts, TickerSnapshot.last_price)
            .filter(
                TickerSnapshot.symbol == symbol,
                TickerSnapshot.exchange_ts >= start,
                TickerSnapshot.exchange_ts <= end,
            )
            .order_by(TickerSnapshot.exchange_ts)
            .all()
        )
    return [(ts, Decimal(str(price)).quantize(_EIGHT_DP)) for ts, price in rows]


def check_ohlc(
    db: DatabaseFactory,
    symbol: str,
    source_kind: str,
    source_url: str,
    bounds: tuple[datetime, datetime],
    threshold: float,
) -> bool:
    """OHLC cross-check on a sampled day (the middle day of the import)."""
    tick_size = TICK_SIZE.get(symbol)
    if tick_size is None:
        logger.error(
            "no pinned tick_size for %s — extend importer.validate.TICK_SIZE "
            "from the EXCHANGE instrument spec",
            symbol,
        )
        return False

    min_ts, max_ts = bounds
    sample_day = _pick_sample_day(db, symbol, min_ts + (max_ts - min_ts) / 2)
    if sample_day is None:
        logger.error("OHLC check: no imported ticks for %s", symbol)
        return False
    day_start = datetime.combine(sample_day, dt_time.min)
    day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)

    ticks = _load_ticks(db, symbol, day_start, day_end)
    if not ticks:
        logger.error("OHLC check: no imported ticks on sampled day %s", sample_day)
        return False
    imported = rebuild_ohlc(ticks)

    try:
        if source_kind == "db":
            klines = _fetch_klines_db(source_url, symbol, day_start, day_end)
        else:
            klines = _fetch_klines_http(source_url, symbol, day_start, day_end)
    except Exception as e:
        logger.error("OHLC check: failed to fetch source klines: %s", e)
        return False
    if not klines:
        logger.error(
            "OHLC check: source has no 1m klines for %s on %s", symbol, sample_day
        )
        return False

    result = compare_ohlc(imported, klines, tick_size, threshold)
    if result.extra_keys:
        logger.error(
            "OHLC check FAILED (key-set mismatch — likely tz offset): %d "
            "imported bucket keys absent from source klines, e.g. %s",
            len(result.extra_keys),
            result.extra_keys[:5],
        )
        return False
    logger.info(
        "OHLC check %s: %d/%d buckets exact on %s (threshold %.2f)",
        "PASSED" if result.passed else "FAILED",
        result.matched,
        result.compared,
        sample_day,
        threshold,
    )
    if result.mismatched_keys:
        logger.info("  mismatching buckets: %s", result.mismatched_keys[:20])
    return result.passed


def smoke_replay(
    db_path: Path, symbol: str, bounds: tuple[datetime, datetime]
) -> bool:
    """One 4 h fresh-grid last_cross replay against the imported DB.

    Replay exposes no console script — invoked via ``python -m
    replay.main``. The rendered config pins the per-symbol exchange
    tick_size so replay's instrument-info path needs no live fetch.
    """
    tick_size = TICK_SIZE.get(symbol)
    if tick_size is None:
        logger.error("no pinned tick_size for %s — smoke replay skipped", symbol)
        return False

    min_ts, max_ts = bounds
    window_start = max(min_ts, max_ts - _SMOKE_WINDOW)
    template_path = Path(__file__).resolve().parents[2] / "conf" / "smoke_replay.yaml"
    with open(template_path) as f:
        config = yaml.safe_load(f)

    config["database_url"] = f"sqlite:///{db_path}"
    config["symbol"] = symbol
    config["start_ts"] = window_start.isoformat()
    config["end_ts"] = max_ts.isoformat()
    config["strategy"]["tick_size"] = str(tick_size)

    with tempfile.TemporaryDirectory(prefix="importer_smoke_") as tmp_dir:
        config["output_dir"] = str(Path(tmp_dir) / "results")
        rendered = Path(tmp_dir) / "smoke_replay.yaml"
        rendered.write_text(yaml.safe_dump(config))
        proc = subprocess.run(
            [sys.executable, "-m", "replay.main", "--config", str(rendered)],
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        logger.error(
            "smoke replay FAILED (exit %d):\n%s",
            proc.returncode,
            (proc.stderr or proc.stdout)[-2000:],
        )
        return False
    # "Metrics parse and are non-NaN": a zero exit alone is not enough —
    # require the summary's Net PnL line to exist and parse to a finite
    # float, plus a blanket NaN scan over the remaining metrics.
    match = re.search(r"Net PnL:\s*(\S+)", proc.stdout)
    if match is None:
        logger.error("smoke replay FAILED: no 'Net PnL' metric in replay output")
        return False
    try:
        net_pnl = float(match.group(1))
    except ValueError:
        logger.error(
            "smoke replay FAILED: unparsable Net PnL %r", match.group(1)
        )
        return False
    if not math.isfinite(net_pnl) or "nan" in proc.stdout.lower():
        logger.error("smoke replay FAILED: non-finite metric in replay summary")
        return False
    logger.info(
        "smoke replay PASSED (%s -> %s, Net PnL %.2f)",
        window_start,
        max_ts,
        net_pnl,
    )
    return True


def recorder_overlap_probe(
    db: DatabaseFactory,
    symbol: str,
    bounds: tuple[datetime, datetime],
    recorder_db: Optional[str],
) -> bool:
    """Cross-check 1 h of overlapping last_price series vs a recorder DB.

    Informational fidelity probe of the collector itself: reports max abs
    diff (imported tick vs recorder last_price at-or-before it) and row
    counts. Skipped with a NOTICE when no recorder DB is supplied or
    coverage does not overlap. Never gates the import.
    """
    if recorder_db is None:
        logger.info(
            "NOTICE: recorder overlap probe skipped (no --recorder-db supplied)"
        )
        return True
    url = (
        recorder_db
        if recorder_db.startswith(("sqlite:", "postgresql:"))
        else f"sqlite:///{recorder_db}"
    )
    rec = DatabaseFactory(DatabaseSettings(database_url=url, read_only=True))
    with rec.get_readonly_session() as session:
        rec_min = (
            session.query(TickerSnapshot.exchange_ts)
            .filter(TickerSnapshot.symbol == symbol)
            .order_by(TickerSnapshot.exchange_ts)
            .first()
        )
        rec_max = (
            session.query(TickerSnapshot.exchange_ts)
            .filter(TickerSnapshot.symbol == symbol)
            .order_by(TickerSnapshot.exchange_ts.desc())
            .first()
        )
        if rec_min is None or rec_max is None:
            logger.info(
                "NOTICE: recorder overlap probe skipped (recorder has no %s)",
                symbol,
            )
            return True
        overlap_start = max(bounds[0], rec_min[0])
        overlap_end = min(bounds[1], rec_max[0])
        if overlap_start >= overlap_end:
            logger.info("NOTICE: recorder overlap probe skipped (no overlap)")
            return True
        window_start = max(overlap_start, overlap_end - _OVERLAP_WINDOW)
        rec_rows = (
            session.query(TickerSnapshot.exchange_ts, TickerSnapshot.last_price)
            .filter(
                TickerSnapshot.symbol == symbol,
                TickerSnapshot.exchange_ts >= window_start,
                TickerSnapshot.exchange_ts <= overlap_end,
            )
            .order_by(TickerSnapshot.exchange_ts)
            .all()
        )

    imported_rows = _load_ticks(db, symbol, window_start, overlap_end)
    if not rec_rows or not imported_rows:
        logger.info("NOTICE: recorder overlap probe skipped (empty window)")
        return True

    max_diff = Decimal("0")
    idx = -1
    for ts, price in imported_rows:
        while idx + 1 < len(rec_rows) and rec_rows[idx + 1][0] <= ts:
            idx += 1
        if idx < 0:
            continue
        diff = abs(price - Decimal(str(rec_rows[idx][1])))
        max_diff = max(max_diff, diff)
    logger.info(
        "recorder overlap probe (%s -> %s): max abs last_price diff %s, "
        "imported rows %d vs recorder rows %d",
        window_start,
        overlap_end,
        max_diff,
        len(imported_rows),
        len(rec_rows),
    )
    return True


def run_validation(
    db: DatabaseFactory,
    db_path: Path,
    symbol: str,
    source_kind: str,
    source_url: str,
    bounds: tuple[datetime, datetime],
    ohlc_threshold: float,
    recorder_db: Optional[str],
) -> bool:
    """Run the three ``--validate`` checks for one imported symbol.

    The OHLC cross-check and smoke replay gate the result; the recorder
    overlap probe is informational only.

    Returns:
        True only when both gating checks pass.
    """
    ohlc_ok = check_ohlc(
        db, symbol, source_kind, source_url, bounds, ohlc_threshold
    )
    smoke_ok = smoke_replay(db_path, symbol, bounds)
    recorder_overlap_probe(db, symbol, bounds, recorder_db)
    return ohlc_ok and smoke_ok
