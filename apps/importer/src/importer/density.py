"""Per-day density report: tick counts, >60 s gaps, LOW-DENSITY flag.

Feature 0093. The source writes only on lastPrice change, so imported data
is sparser than the recorder's ~4-5 ticks/s. last_cross overfill
sensitivity rises with sparsity — LOW-DENSITY days carry a WARNING so
sweeps on that data are qualified.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from typing import Iterator, List, Optional

from grid_db.database import DatabaseFactory
from grid_db.models import TickerSnapshot

logger = logging.getLogger(__name__)

GAP_THRESHOLD_S = 60.0
LOW_DENSITY_TICKS_PER_S = 0.5
_PAGE_SIZE = 50000


@dataclass
class DayDensity:
    """Density stats for one UTC day."""

    day: date
    tick_count: int = 0
    gaps: List[tuple[datetime, datetime]] = field(default_factory=list)
    covered_seconds: float = 0.0

    @property
    def ticks_per_second(self) -> float:
        if self.covered_seconds <= 0:
            return 0.0
        return self.tick_count / self.covered_seconds

    @property
    def low_density(self) -> bool:
        return self.ticks_per_second < LOW_DENSITY_TICKS_PER_S


def _iter_exchange_ts(db: DatabaseFactory, symbol: str) -> Iterator[datetime]:
    """Stream exchange_ts ascending, keyset-paginated (unique per symbol)."""
    with db.get_session() as session:
        cursor: Optional[datetime] = None
        while True:
            query = session.query(TickerSnapshot.exchange_ts).filter(
                TickerSnapshot.symbol == symbol
            )
            if cursor is not None:
                query = query.filter(TickerSnapshot.exchange_ts > cursor)
            rows = (
                query.order_by(TickerSnapshot.exchange_ts)
                .limit(_PAGE_SIZE)
                .all()
            )
            if not rows:
                return
            for (ts,) in rows:
                yield ts
            cursor = rows[-1][0]


def compute_density(db: DatabaseFactory, symbol: str) -> List[DayDensity]:
    """Per-UTC-day tick counts, >60 s gaps (keyed to the gap's start day)."""
    days: dict[date, DayDensity] = {}
    prev: Optional[datetime] = None
    first: Optional[datetime] = None
    last: Optional[datetime] = None

    for ts in _iter_exchange_ts(db, symbol):
        if first is None:
            first = ts
        last = ts
        day = ts.date()
        entry = days.get(day)
        if entry is None:
            entry = days[day] = DayDensity(day=day)
        entry.tick_count += 1
        if prev is not None and (ts - prev).total_seconds() > GAP_THRESHOLD_S:
            days.setdefault(prev.date(), DayDensity(day=prev.date())).gaps.append(
                (prev, ts)
            )
        prev = ts

    if first is None:
        return []

    # Partial first/last days: rate denominator is the covered span within
    # the day, not a full 86400 s.
    for entry in days.values():
        day_start = datetime.combine(entry.day, dt_time.min)
        day_end = day_start + timedelta(days=1)
        span_start = max(day_start, first)
        span_end = min(day_end, last)
        entry.covered_seconds = max((span_end - span_start).total_seconds(), 0.0)

    return [days[d] for d in sorted(days)]


def log_density_report(symbol: str, days: List[DayDensity]) -> bool:
    """Log the per-day report; returns True when any day is LOW-DENSITY."""
    any_low = False
    for entry in days:
        flag = " LOW-DENSITY" if entry.low_density else ""
        logger.info(
            "%s %s: %d ticks, %.2f ticks/s, %d gaps >%.0fs%s",
            symbol,
            entry.day.isoformat(),
            entry.tick_count,
            entry.ticks_per_second,
            len(entry.gaps),
            GAP_THRESHOLD_S,
            flag,
        )
        for gap_start, gap_end in entry.gaps:
            logger.info(
                "  gap %.0fs: %s -> %s",
                (gap_end - gap_start).total_seconds(),
                gap_start,
                gap_end,
            )
        if entry.low_density:
            any_low = True
    if any_low:
        logger.warning(
            "%s has LOW-DENSITY days (< %.1f ticks/s): last_cross overfill "
            "sensitivity rises with sparsity — qualify any sweep on this "
            "data. Never resume-append into this DB while a sweep reads it "
            "(use --tag for an isolated file).",
            symbol,
            LOW_DENSITY_TICKS_PER_S,
        )
    return any_low
