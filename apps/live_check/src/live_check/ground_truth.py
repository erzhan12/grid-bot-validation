"""Recorded-row ground-truth queries for live_check.

All reads run against a READ-ONLY session on the live recorder DB. Ground
truth is recorded rows ONLY — never live Bybit REST (``pnl_checker`` is the
wrong tool for historical reconcile).

Per-symbol isolation: both live strats share one ``run_id``, so every
realized/commission/count query here filters by ``symbol`` IN ADDITION to
``run_id`` + window. ``PrivateExecutionRepository.get_by_run_range`` has no
symbol filter and would commingle SOL and LTC — do not use it for sums.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from grid_db import (
    PositionSnapshotRepository,
    PrivateExecution,
    Run,
    TickerSnapshot,
)

from live_check.window import Window, to_naive_utc

_ZERO = Decimal("0")


@dataclass(frozen=True)
class GroundTruth:
    """Recorded ground truth for one strat over one window."""

    sum_realized: Decimal
    sum_commission: Decimal
    net_unrealised: Decimal
    live_exec_count: int  # informational display ONLY — never the pass gate


@dataclass(frozen=True)
class ExecRow:
    """Materialized raw execution row for the --per-fill table.

    Plain dataclass (not the ORM row) so attribute access stays valid after
    the read session closes (cf. feature 0038 DetachedInstanceError).
    """

    exec_id: str
    exchange_ts: datetime
    side: str
    exec_price: Optional[Decimal]
    exec_qty: Optional[Decimal]
    closed_pnl: Optional[Decimal]
    order_link_id: Optional[str]
    # Bybit order id — pairing fallback for NULL order_link_id rows and part
    # of the (client_id, order_id) join key (mirrors LiveTradeLoader).
    order_id: str


def _sum_column(
    session: Session,
    column,
    run_id: str,
    symbol: str,
    start: datetime,
    end: datetime,
) -> Decimal:
    """SUM a nullable PrivateExecution column with NULL→0 coalesce."""
    value = (
        session.query(func.coalesce(func.sum(column), 0))
        .filter(
            PrivateExecution.run_id == run_id,
            PrivateExecution.symbol == symbol,
            PrivateExecution.exchange_ts >= to_naive_utc(start),
            PrivateExecution.exchange_ts <= to_naive_utc(end),
        )
        .scalar()
    )
    # Coalesce already maps NULL→0 at the SQL layer; the Python-side guard
    # covers dialects/drivers that hand back None or a non-Decimal scalar.
    if value is None:
        return _ZERO
    return Decimal(str(value))


def sum_realized(
    session: Session, run_id: str, symbol: str, start: datetime, end: datetime
) -> Decimal:
    """SUM ``closed_pnl`` over the window, scoped by run_id + symbol."""
    return _sum_column(
        session, PrivateExecution.closed_pnl, run_id, symbol, start, end
    )


def sum_commission(
    session: Session, run_id: str, symbol: str, start: datetime, end: datetime
) -> Decimal:
    """SUM ``exec_fee`` over the window, scoped by run_id + symbol."""
    return _sum_column(
        session, PrivateExecution.exec_fee, run_id, symbol, start, end
    )


def net_unrealised_per_pair(
    session: Session,
    run_id: str,
    account_id: str,
    symbol: str,
    at_ts: datetime,
) -> Decimal:
    """NET unrealised PnL per pair (long + short) at-or-before ``at_ts``.

    Hedge mode: only the combined net per pair is meaningful — never quote
    long-leg vs short-leg unrealised separately. Uses the existing
    ``PositionSnapshotRepository.get_latest_before`` per side (``source='live'``)
    — the repo already gives the correct per-side last-at-or-before row.
    """
    repo = PositionSnapshotRepository(session)
    total = _ZERO
    for side in ("Buy", "Sell"):
        snap = repo.get_latest_before(
            run_id=run_id,
            account_id=account_id,
            symbol=symbol,
            side=side,
            at_ts=to_naive_utc(at_ts),
            source="live",
        )
        if snap is not None and snap.unrealised_pnl is not None:
            total += Decimal(str(snap.unrealised_pnl))
    return total


def live_exec_count(
    session: Session, run_id: str, symbol: str, start: datetime, end: datetime
) -> int:
    """COUNT of RAW execution rows over the window (display only).

    Partial fills aggregate multiple raw execs into one ``NormalizedTrade``,
    so this count ≠ ``matched_count`` on a correct run — it must never gate
    the matched verdict.
    """
    return (
        session.query(func.count(PrivateExecution.id))
        .filter(
            PrivateExecution.run_id == run_id,
            PrivateExecution.symbol == symbol,
            PrivateExecution.exchange_ts >= to_naive_utc(start),
            PrivateExecution.exchange_ts <= to_naive_utc(end),
        )
        .scalar()
        or 0
    )


def get_window_executions(
    session: Session, run_id: str, symbol: str, start: datetime, end: datetime
) -> list[ExecRow]:
    """RAW execution rows over the window (symbol-scoped), for --per-fill.

    One item per live execution (partial fills NOT aggregated), ordered
    ``(exchange_ts, exec_id)`` to match the event_follower stream order.
    """
    rows = (
        session.query(PrivateExecution)
        .filter(
            PrivateExecution.run_id == run_id,
            PrivateExecution.symbol == symbol,
            PrivateExecution.exchange_ts >= to_naive_utc(start),
            PrivateExecution.exchange_ts <= to_naive_utc(end),
        )
        .order_by(PrivateExecution.exchange_ts, PrivateExecution.exec_id)
        .all()
    )
    return [
        ExecRow(
            exec_id=r.exec_id,
            exchange_ts=r.exchange_ts,
            side=r.side,
            exec_price=r.exec_price,
            exec_qty=r.exec_qty,
            closed_pnl=r.closed_pnl,
            order_link_id=r.order_link_id,
            order_id=r.order_id,
        )
        for r in rows
    ]


def latest_ticker_ts(session: Session, symbol: str) -> Optional[datetime]:
    """Freshness probe: ``MAX(TickerSnapshot.exchange_ts)`` for the symbol.

    Keyed by SYMBOL, not ``run_id`` (``TickerSnapshot`` has no run_id column).
    Not ``PrivateExecution`` — fill-only streams false-trip the gate during
    quiet-but-healthy periods. Returns None when no ticker rows exist.
    """
    return (
        session.query(func.max(TickerSnapshot.exchange_ts))
        .filter(TickerSnapshot.symbol == symbol)
        .scalar()
    )


def get_run_start(session: Session, run_id: str) -> datetime:
    """``Run.start_ts`` for the pre-0080 run floor guard."""
    run = session.get(Run, run_id)
    if run is None:
        raise ValueError(f"Run '{run_id}' not found in database")
    return run.start_ts


def collect(
    session: Session,
    run_id: str,
    account_id: str,
    symbol: str,
    window: Window,
) -> GroundTruth:
    """Gather all recorded ground truth for one strat over one window."""
    return GroundTruth(
        sum_realized=sum_realized(session, run_id, symbol, window.start, window.end),
        sum_commission=sum_commission(
            session, run_id, symbol, window.start, window.end
        ),
        net_unrealised=net_unrealised_per_pair(
            session, run_id, account_id, symbol, window.end
        ),
        live_exec_count=live_exec_count(
            session, run_id, symbol, window.start, window.end
        ),
    )
