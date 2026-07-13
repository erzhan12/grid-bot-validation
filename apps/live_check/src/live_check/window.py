"""Window / lag computation and datetime guards for live_check.

All datetimes used in queries or comparisons are normalized to NAIVE UTC —
SQLite stores timestamps tz-stripped, and an aware-vs-naive comparison or
subtraction raises ``TypeError`` (cf. ``comparator/loader.py:26``,
``replay/engine.py`` tz handling).
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

# Feature 0080 merged 2026-06-17: orderLinkId hashes are salted by strat_id.
# event_follower matches by link_id, so replaying PRE-0080 data collapses
# matching (954→44). Windows must never start before this cutoff.
POST_0080_CUTOFF = datetime(2026, 6, 17, 23, 7, 0)

_MIN_STALENESS_THRESHOLD = timedelta(minutes=5)

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")

_DURATION_UNITS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}


def parse_duration(text: str) -> timedelta:
    """Parse a duration like ``10m``, ``4h``, ``30s``, ``1d``.

    Args:
        text: Duration string — integer count plus one unit suffix.

    Returns:
        Equivalent timedelta.

    Raises:
        ValueError: On unparseable input.
    """
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise ValueError(
            f"Invalid duration {text!r}; expected forms like '30s', '2m', '4h', '1d'"
        )
    count, unit = match.groups()
    return int(count) * _DURATION_UNITS[unit]


def to_naive_utc(dt: datetime) -> datetime:
    """Convert any datetime to naive UTC (aware → UTC then strip tzinfo)."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@dataclass(frozen=True)
class Window:
    """Rolling comparison window, naive-UTC bounds."""

    start: datetime
    end: datetime


def compute_window(
    last: timedelta,
    lag: timedelta,
    now: Optional[datetime] = None,
) -> Window:
    """Compute the rolling window ``[now − lag − last, now − lag]``.

    The lag lets recorder writes settle so the check never races
    un-flushed data.

    Args:
        last: Window length.
        lag: End lag behind now.
        now: Injectable current time (tests); defaults to UTC now.

    Returns:
        Window with naive-UTC start/end.
    """
    now = to_naive_utc(now if now is not None else datetime.now(timezone.utc))
    end = now - lag
    return Window(start=end - last, end=end)


def check_post_0080_floors(window_start: datetime, run_start: datetime) -> None:
    """Enforce both post-0080 floors on the window start (pitfall #1).

    Args:
        window_start: Rolling window start.
        run_start: ``Run.start_ts`` of the configured recorder run.

    Raises:
        ValueError: When the window starts before the run start or before
            the absolute 0080 merge cutoff.
    """
    start = to_naive_utc(window_start)
    run_floor = to_naive_utc(run_start)
    if start < run_floor:
        raise ValueError(
            f"Window start {start.isoformat()} precedes the configured run's "
            f"start_ts {run_floor.isoformat()}. Replaying data outside the "
            "recorded run gives no ground truth to reconcile against."
        )
    if start < POST_0080_CUTOFF:
        raise ValueError(
            f"Window start {start.isoformat()} precedes the 0080 orderLinkId "
            f"salting cutoff {POST_0080_CUTOFF.isoformat()}Z. event_follower "
            "matches by link_id, so PRE-0080 data collapses matching "
            "(954→44 matched) and any reconcile would be silently bogus."
        )


def staleness_threshold(
    lag: timedelta, override: Optional[timedelta] = None
) -> timedelta:
    """Freshness gate trip point: ``max(2 * lag, 5 minutes)`` unless overridden."""
    if override is not None:
        return override
    return max(2 * lag, _MIN_STALENESS_THRESHOLD)


def freshness_skip_reason(
    latest_ticker_ts: Optional[datetime],
    lag: timedelta,
    threshold: timedelta,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Return a SKIP reason when recorded ticker data is stale, else None.

    Probes ``TickerSnapshot`` recency (per symbol) — NOT ``PrivateExecution``,
    which is fill-only and would false-trip during quiet-but-healthy periods.

    Args:
        latest_ticker_ts: ``MAX(TickerSnapshot.exchange_ts)`` for the symbol,
            or None when the DB has no ticker rows for it.
        lag: Configured window lag.
        threshold: Staleness threshold (see :func:`staleness_threshold`).
        now: Injectable current time (tests); defaults to UTC now.

    Returns:
        Human-readable skip reason, or None when data is fresh.
    """
    if latest_ticker_ts is None:
        return "no ticker data"
    now = to_naive_utc(now if now is not None else datetime.now(timezone.utc))
    age = now - lag - to_naive_utc(latest_ticker_ts)
    if age > threshold:
        return f"ticker data stale by {age} (threshold {threshold})"
    return None
