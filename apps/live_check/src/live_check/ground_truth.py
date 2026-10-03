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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

from grid_db import (
    OrderRepository,
    PositionSnapshot,
    PositionSnapshotRepository,
    PrivateExecution,
    PrivateStreamGap,
    PrivateStreamGapRepository,
    PrivateStreamSession,
    PrivateStreamSessionRepository,
    RecordedDataQualityError,
    Run,
    TickerSnapshot,
    WalletSnapshotRepository,
)

from replay.config import SeedConfig

from live_check.window import Window, to_naive_utc

_ZERO = Decimal("0")
# The wallet coin replay seeds from: live-check's runner never overrides
# SeedConfig.wallet_coin (--shared seeds from MultiSeedConfig.wallet_coin, a
# separate field with the same "USDT" default), so the gate follows it.
_WALLET_SEED_COIN = SeedConfig.model_fields["wallet_coin"].default

_LEG_NAME = {"Buy": "long", "Sell": "short"}
# Hedge-mode leg of an order: side + reduce_only (same map as replay's
# snapshot_loader._DIRECTION_BY_SIDE_REDUCE). Buy opens / Sell reduce-only
# closes the long leg; Sell opens / Buy reduce-only closes the short leg.
_LEG_BY_SIDE_REDUCE = {
    ("Buy", False): "Buy",
    ("Sell", True): "Buy",
    ("Sell", False): "Sell",
    ("Buy", True): "Sell",
}
_BOTH_LEGS = frozenset({"Buy", "Sell"})
# 0110 B2a startup placeholders that do not prove a leg's state.
_UNFIT_SYNTHETIC = frozenset({"rest_failure", "malformed", "empty_response"})


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


def unknown_pnl_exec_ids(
    session: Session, run_id: str, symbol: str, start: datetime, end: datetime
) -> list[str]:
    """``exec_id``s in the window whose ``closed_pnl`` is NULL (unknown).

    A NULL is not zero PnL: REST-backfilled executions carry no per-execution
    PnL. Any such row makes the window's realized sum undefined.
    """
    rows = (
        session.query(PrivateExecution.exec_id)
        .filter(
            PrivateExecution.run_id == run_id,
            PrivateExecution.symbol == symbol,
            PrivateExecution.exchange_ts >= to_naive_utc(start),
            PrivateExecution.exchange_ts <= to_naive_utc(end),
            PrivateExecution.closed_pnl.is_(None),
        )
        .order_by(PrivateExecution.exchange_ts, PrivateExecution.exec_id)
        .all()
    )
    return [row[0] for row in rows]


def unknown_pnl_reason(exec_ids: list[str]) -> str:
    """Human-readable SKIP/error reason for unknown-PnL executions."""
    return (
        f"unknown closed_pnl on {len(exec_ids)} execution(s) "
        f"(first: {exec_ids[0]})"
    )


def sum_realized(
    session: Session, run_id: str, symbol: str, start: datetime, end: datetime
) -> Decimal:
    """SUM ``closed_pnl`` over the window, scoped by run_id + symbol.

    Raises:
        RecordedDataQualityError: Any execution in scope has NULL
            ``closed_pnl`` — SQL ``SUM`` would silently drop it.
    """
    # One aggregate statement so a NULL row cannot land between the
    # NULL check and the SUM (SUM silently skips NULLs).
    total, unknown_count = (
        session.query(
            func.coalesce(func.sum(PrivateExecution.closed_pnl), 0),
            func.count() - func.count(PrivateExecution.closed_pnl),
        )
        .filter(
            PrivateExecution.run_id == run_id,
            PrivateExecution.symbol == symbol,
            PrivateExecution.exchange_ts >= to_naive_utc(start),
            PrivateExecution.exchange_ts <= to_naive_utc(end),
        )
        .one()
    )
    if unknown_count:
        raise RecordedDataQualityError(
            f"unknown closed_pnl on {unknown_count} execution(s)"
        )
    return Decimal(str(total)) if total is not None else _ZERO


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
    """NET unrealised PnL per pair (long + short) from the end anchors.

    Hedge mode: only the combined net per pair is meaningful — never quote
    long-leg vs short-leg unrealised separately. Reads each leg's end anchor
    (:func:`end_anchors`, latest row received at-or-before ``at_ts``); a
    flat leg is a known 0.

    Raises:
        RecordedDataQualityError: A leg has no row, or an open leg has a NULL
            ``unrealised_pnl`` (0110 B2c: never a partial sum).
    """
    total = _ZERO
    anchors = end_anchors(session, run_id, account_id, symbol, at_ts)
    for side, row in anchors.items():
        leg = _LEG_NAME[side]
        if row is None:
            raise RecordedDataQualityError(
                f"no {leg} position row for {symbol} at or before "
                f"{to_naive_utc(at_ts)}; net unrealised is unknown"
            )
        if row.size == 0:
            continue
        if row.unrealised_pnl is None:
            raise RecordedDataQualityError(
                f"{leg} position for {symbol} is open (size {row.size}) with "
                "unknown unrealised_pnl"
            )
        total += Decimal(str(row.unrealised_pnl))
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
    """Gather all recorded ground truth for one strat over one window.

    Raises:
        RecordedDataQualityError: NULL closed_pnl in the window
            (via :func:`sum_realized`).
    """
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


PRE_0110_REASON = "recorder has no private-stream coverage (pre-0110)"


def _coverage_tables_present(session: Session) -> bool:
    """Whether the recorder DB has the 0110 coverage tables (never creates)."""
    inspector = inspect(session.get_bind())
    return all(
        inspector.has_table(model.__tablename__)
        for model in (PrivateStreamSession, PrivateStreamGap)
    )


def _coverage_start(
    session: Session,
    run_id: str,
    account_id: str,
    symbol: str,
    window: Window,
    connected_at: Optional[datetime],
    anchors: dict[str, Optional[PositionSnapshot]],
) -> datetime:
    """Earliest time the verdict depends on: window start, a seed row or an
    end anchor.

    Replay seeds from the latest live position rows (Buy/Sell) and wallet
    row at-or-before ``window.start`` (same lookups as
    ``replay.snapshot_loader``), so a gap after a seed row changes the seed.
    Anchored on ``local_ts`` (the recorder clock coverage is measured on): a
    push is a full snapshot as of receipt, while its ``exchange_ts`` (Bybit
    ``updatedTime``) can predate the recorder start for a quiet leg. A seed
    row before the run's first ``connected_at`` can only be a push received
    while the recorder waited for its subscription acks (it stamps the
    session after them), so its anchor is clamped to ``connected_at``. The
    end anchors (0110 B2c) join for the same reason: the verdict reads them.
    """
    times = [
        to_naive_utc(row.local_ts) for row in anchors.values() if row is not None
    ]
    positions = PositionSnapshotRepository(session)
    for side in ("Buy", "Sell"):
        row = positions.get_latest_before(
            run_id=run_id,
            account_id=account_id,
            symbol=symbol,
            side=side,
            at_ts=window.start,
            source="live",
        )
        if row is not None:
            times.append(to_naive_utc(row.local_ts))
    wallet = WalletSnapshotRepository(session).get_latest_before(
        run_id, account_id, _WALLET_SEED_COIN, window.start
    )
    if wallet is not None:
        times.append(to_naive_utc(wallet.local_ts))
    if connected_at is not None:
        floor = to_naive_utc(connected_at)
        times = [max(t, floor) for t in times]
    return min([window.start, *times])


def private_coverage_skip_reason(
    session: Session,
    run_id: str,
    account_id: str,
    symbol: str,
    window: Window,
    anchors: Optional[dict[str, Optional[PositionSnapshot]]] = None,
) -> Optional[str]:
    """SKIP reason unless the private stream covered the verdict's interval.

    Feature 0110 B2b. The interval runs from the earliest of ``window.start``
    and replay's seed rows to ``window.end``. It is covered when one recorder
    session connected by its start and checkpointed by its end, and no gap
    row of this run/account/symbol overlaps it — a recovered gap still SKIPs,
    because orders and positions are not backfilled. Gaps are checked before
    the session so the reason names the gap.

    Args:
        session: Read-only session on the recorder DB.
        run_id: Recording run.
        account_id: Account of the private stream.
        symbol: Strat symbol (gap rows are per symbol).
        window: Comparison window (naive UTC).
        anchors: :func:`end_anchors` at ``window.end`` when the caller has
            them already; looked up otherwise.

    Returns:
        Human-readable skip reason, or None when covered.
    """
    if not _coverage_tables_present(session):
        return PRE_0110_REASON
    if anchors is None:
        anchors = end_anchors(session, run_id, account_id, symbol, window.end)
    sessions = PrivateStreamSessionRepository(session).list_for_run(
        run_id, account_id
    )
    start = _coverage_start(
        session, run_id, account_id, symbol, window,
        sessions[0].connected_at if sessions else None, anchors,
    )
    end = window.end
    # Gaps first: the recorder stops checkpointing while a gap is open, so
    # the session check would hide the gap's bounds and status. Gap rows are
    # per symbol: the recorder writes one per configured symbol for an
    # account-wide outage, so this assumes the strat symbol is recorded.
    gaps = PrivateStreamGapRepository(session).list_overlapping(
        run_id, account_id, symbol, start, end
    )
    if gaps:
        first = gaps[0]
        gap_end = (
            to_naive_utc(first.gap_end) if first.gap_end is not None else "open"
        )
        more = f" (+{len(gaps) - 1} more)" if len(gaps) > 1 else ""
        hint = ""
        if all(
            g.gap_end is not None and to_naive_utc(g.gap_end) < window.start
            for g in gaps
        ):
            # Only the seed stretch is hit: every later window of this run
            # reaches back to the same seed rows and SKIPs too.
            hint = (
                "; the overlapping gap(s) only touch the seed rows before the "
                "window — positions are not backfilled, so later windows SKIP "
                "until that leg's size changes; restart the recorder for a "
                "fresh run_id"
            )
        return (
            f"private-stream gap {to_naive_utc(first.gap_start)}–{gap_end} "
            f"(recovery {first.recovery_status}) overlaps {start}–{end}"
            f"{more}{hint}"
        )
    if not sessions:
        return f"no private-stream session recorded for run {run_id}"
    if not any(
        to_naive_utc(s.connected_at) <= start
        and to_naive_utc(s.last_checkpoint_ts) >= end
        for s in sessions
    ):
        latest = sessions[-1]
        return (
            f"private stream not covered over {start}–{end} (latest session "
            f"connected {to_naive_utc(latest.connected_at)}, checkpoint "
            f"{to_naive_utc(latest.last_checkpoint_ts)})"
        )
    return None


def end_anchors(
    session: Session,
    run_id: str,
    account_id: str,
    symbol: str,
    at_ts: datetime,
) -> dict[str, Optional[PositionSnapshot]]:
    """Each leg's end anchor: its latest live row received at-or-before
    ``at_ts`` (0110 B2c), keyed ``"Buy"`` (long) / ``"Sell"`` (short)."""
    repo = PositionSnapshotRepository(session)
    at = to_naive_utc(at_ts)
    return {
        side: repo.get_latest_received_before(run_id, account_id, symbol, side, at)
        for side in ("Buy", "Sell")
    }


def _anchor_update_time(row: PositionSnapshot) -> datetime:
    """Bybit ``updatedTime`` of an anchor (exchange clock, like execTime).

    Both the WS writer and the REST snapshot keep Bybit's payload in
    ``raw_json``; ``exchange_ts`` is the local fetch time on a REST row, so
    it is only the fallback.
    """
    raw = row.raw_json if isinstance(row.raw_json, dict) else {}
    updated = raw.get("updatedTime")
    if updated not in (None, "", "0", 0):
        try:
            return datetime.fromtimestamp(int(updated) / 1000, tz=UTC).replace(
                tzinfo=None
            )
        except (TypeError, ValueError, OverflowError):
            pass
    return to_naive_utc(row.exchange_ts)


def _execution_legs(
    session: Session,
    run_id: str,
    account_id: str,
    symbol: str,
    executions: list[PrivateExecution],
) -> dict[str, frozenset[str]]:
    """Leg(s) of each execution via its latest order row (run/account/symbol
    scoped). No order row — only Limit orders are recorded — or a NULL
    ``reduce_only`` → both legs."""
    orders = OrderRepository(session).get_latest_by_order_ids(
        run_id, account_id, symbol, sorted({e.order_id for e in executions})
    )
    legs = {}
    for execution in executions:
        row = orders.get(execution.order_id)
        leg = (
            _LEG_BY_SIDE_REDUCE.get((row.side, row.reduce_only))
            if row is not None and row.reduce_only is not None
            else None
        )
        legs[execution.exec_id] = frozenset({leg}) if leg else _BOTH_LEGS
    return legs


def end_anchor_skip_reason(
    session: Session,
    run_id: str,
    account_id: str,
    symbol: str,
    window: Window,
    anchors: dict[str, Optional[PositionSnapshot]],
) -> Optional[str]:
    """SKIP reason unless both end anchors prove the legs' state (0110 B2c).

    A leg's anchor is unfit when it is missing, is a startup placeholder
    without a leg reading (``rest_failure`` / ``malformed`` /
    ``empty_response``), is open with a NULL ``unrealised_pnl``, or predates
    an execution on that leg (``updatedTime < execTime <= window.end``).
    Bybit pushes a position update after every fill, so the newest row is
    always judged — never an older fit one. Two fills in one millisecond are
    only caught once the second fill's push is received.

    Args:
        session: Read-only session on the recorder DB.
        run_id: Recording run.
        account_id: Account whose executions and orders are judged.
        symbol: Strat symbol.
        window: Comparison window (naive UTC).
        anchors: :func:`end_anchors` at ``window.end``.

    Returns:
        Human-readable skip reason, or None when both legs are fit.
    """
    for side, row in anchors.items():
        leg = _LEG_NAME[side]
        if row is None:
            return f"{leg} position row missing at or before {window.end}"
        raw = row.raw_json if isinstance(row.raw_json, dict) else {}
        marker = raw.get("synthetic")
        if marker in _UNFIT_SYNTHETIC:
            return (
                f"{leg} position row is a startup placeholder ({marker}): "
                "insufficient position evidence"
            )
        if row.size != 0 and row.unrealised_pnl is None:
            return (
                f"{leg} position row (size {row.size}) has no unrealised_pnl"
            )
    updated = {side: _anchor_update_time(row) for side, row in anchors.items()}
    executions = (
        session.query(PrivateExecution)
        .filter(
            PrivateExecution.run_id == run_id,
            PrivateExecution.account_id == account_id,
            PrivateExecution.symbol == symbol,
            PrivateExecution.exchange_ts > min(updated.values()),
            PrivateExecution.exchange_ts <= window.end,
        )
        .order_by(PrivateExecution.exchange_ts, PrivateExecution.exec_id)
        .all()
    )
    legs = _execution_legs(session, run_id, account_id, symbol, executions)
    for side, anchor_time in updated.items():
        late = [
            e for e in executions
            if side in legs[e.exec_id] and to_naive_utc(e.exchange_ts) > anchor_time
        ]
        if late:
            first = late[0]
            return (
                f"{_LEG_NAME[side]} position row (updated {anchor_time}) "
                f"predates {len(late)} execution(s) on that leg (first: "
                f"{first.exec_id} at {to_naive_utc(first.exchange_ts)})"
            )
    return None
