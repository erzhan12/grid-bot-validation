"""Trade loaders for live and backtest data sources.

Normalizes both sources into a common NormalizedTrade format for comparison.
"""

import csv
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from grid_db import PrivateExecution, PrivateExecutionRepository
from gridcore.position import DirectionType, SideType

_ZERO = Decimal("0")

logger = logging.getLogger(__name__)


def _normalize_ts(dt: datetime) -> datetime:
    """Convert any datetime to naive UTC.

    SQLite strips timezone info, so we normalize all timestamps to naive UTC
    to avoid TypeError when comparing/subtracting mixed-awareness datetimes.

    If aware, convert to UTC then strip tzinfo.
    If naive, pass through (assumed already UTC).
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@dataclass
class NormalizedTrade:
    """Common trade format for comparison."""

    client_order_id: str
    symbol: str
    side: SideType
    price: Decimal
    qty: Decimal
    fee: Decimal
    realized_pnl: Decimal
    timestamp: datetime
    source: str  # 'live' or 'backtest'
    direction: Optional[DirectionType] = None
    occurrence: int = 0  # nth occurrence of this client_order_id (for reuse handling)


def _assign_occurrences(trades: list[NormalizedTrade]) -> None:
    """Assign occurrence index to each trade per client_order_id.

    Trades must be sorted by (timestamp, client_order_id, side) before calling.
    Mutates trades in-place.

    Known limitation: if two trades share the exact same
    (timestamp, client_order_id, side) tuple, their relative order is
    non-deterministic across live vs backtest sources, which may assign
    different occurrence indices. In practice this requires the same
    deterministic hash to be reused at the exact same millisecond with the
    same side — an extremely unlikely scenario.
    """
    counts: dict[str, int] = defaultdict(int)
    for trade in trades:
        trade.occurrence = counts[trade.client_order_id]
        counts[trade.client_order_id] += 1


class LiveTradeLoader:
    """Load and normalize live trades from database.

    Aggregates partial fills into one trade per order_link_id using VWAP price.
    """

    def __init__(self, session: Session):
        self._repo = PrivateExecutionRepository(session)

    def load(
        self,
        run_id: str,
        start_ts: datetime,
        end_ts: datetime,
        symbol: Optional[str] = None,
    ) -> list[NormalizedTrade]:
        """Load live executions and normalize.

        Args:
            run_id: Live run ID to query.
            start_ts: Start of time window (inclusive).
            end_ts: End of time window (inclusive).
            symbol: Optional symbol filter.

        Returns:
            List of NormalizedTrade sorted by timestamp.
        """
        executions = self._repo.get_by_run_range(run_id, start_ts, end_ts)

        if symbol:
            executions = [e for e in executions if e.symbol == symbol]

        # Group by (order_link_id, order_id) to separate partial fills from
        # lifecycle reuse. Same order_link_id + same order_id = partial fills
        # (aggregate). Same order_link_id + different order_id = ID reuse
        # (separate trades).
        grouped: dict[tuple[str, str], list[PrivateExecution]] = defaultdict(list)
        skipped = 0
        for ex in executions:
            if not ex.order_link_id:
                skipped += 1
                continue
            grouped[(ex.order_link_id, ex.order_id)].append(ex)

        if skipped:
            logger.info("Skipped %d executions without order_link_id", skipped)

        trades = []
        for (client_id, _order_id), fills in grouped.items():
            trade = self._aggregate_fills(client_id, fills)
            trades.append(trade)

        trades.sort(key=lambda t: (t.timestamp, t.client_order_id, t.side))

        # Assign occurrence index per client_order_id (handles ID reuse)
        _assign_occurrences(trades)

        logger.info("Loaded %d live trades (%d raw executions)", len(trades), len(executions) - skipped)
        return trades

    def _aggregate_fills(
        self, client_order_id: str, fills: list[PrivateExecution]
    ) -> NormalizedTrade:
        """Aggregate partial fills into one NormalizedTrade.

        Uses VWAP for price, sums qty/fee/pnl, takes latest timestamp.
        """
        total_qty = Decimal("0")
        total_notional = Decimal("0")
        total_fee = Decimal("0")
        total_pnl = Decimal("0")
        latest_ts = _normalize_ts(fills[0].exchange_ts)

        for f in fills:
            qty = f.exec_qty
            total_qty += qty
            total_notional += f.exec_price * qty
            total_fee += f.exec_fee or Decimal("0")
            total_pnl += f.closed_pnl or Decimal("0")
            f_ts = _normalize_ts(f.exchange_ts)
            if f_ts > latest_ts:
                latest_ts = f_ts

        vwap_price = total_notional / total_qty if total_qty else _ZERO

        # Infer direction: closing trades have non-zero closed_pnl.
        # NOTE: Break-even closes (closed_pnl==0) are misclassified as opening
        # trades. For matched pairs, metrics.py prefers backtest direction
        # (always correct) over this inferred value.
        side = SideType(fills[0].side)
        is_closing = total_pnl != _ZERO
        if is_closing:
            # Buy closing = closing short; Sell closing = closing long
            direction = DirectionType.SHORT if side == SideType.BUY else DirectionType.LONG
        else:
            # Buy opening = opening long; Sell opening = opening short
            direction = DirectionType.LONG if side == SideType.BUY else DirectionType.SHORT

        return NormalizedTrade(
            client_order_id=client_order_id,
            symbol=fills[0].symbol,
            side=side,
            price=vwap_price,
            qty=total_qty,
            fee=total_fee,
            realized_pnl=total_pnl,
            timestamp=latest_ts,
            source="live",
            direction=direction,
        )


class BacktestTradeLoader:
    """Load and normalize backtest trades.

    Supports loading from BacktestSession.trades or from CSV export.
    """

    def load_from_session(self, trades: list) -> list[NormalizedTrade]:
        """Load from BacktestSession.trades list.

        Args:
            trades: List of BacktestTrade objects.

        Returns:
            List of NormalizedTrade sorted by timestamp.
        """
        normalized = []
        for t in trades:
            normalized.append(NormalizedTrade(
                client_order_id=t.client_order_id,
                symbol=t.symbol,
                side=SideType(t.side),
                price=t.price,
                qty=t.qty,
                fee=t.commission,
                realized_pnl=t.realized_pnl,
                timestamp=_normalize_ts(t.timestamp),
                source="backtest",
                direction=DirectionType(t.direction) if t.direction else None,
            ))

        normalized.sort(key=lambda t: (t.timestamp, t.client_order_id, t.side))
        _assign_occurrences(normalized)
        logger.info("Loaded %d backtest trades from session", len(normalized))
        return normalized

    def load_from_csv(self, path: str | Path) -> list[NormalizedTrade]:
        """Load from backtest trades CSV export.

        Expects columns: trade_id, timestamp, symbol, side, direction, price,
        qty, notional, realized_pnl, commission, order_id, client_order_id, strat_id

        Args:
            path: Path to trades CSV file.

        Returns:
            List of NormalizedTrade sorted by timestamp.
        """
        normalized = []
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                normalized.append(NormalizedTrade(
                    client_order_id=row["client_order_id"],
                    symbol=row["symbol"],
                    side=SideType(row["side"]),
                    price=Decimal(row["price"]),
                    qty=Decimal(row["qty"]),
                    fee=Decimal(row["commission"]),
                    realized_pnl=Decimal(row["realized_pnl"]),
                    timestamp=_normalize_ts(datetime.fromisoformat(row["timestamp"])),
                    source="backtest",
                    direction=DirectionType(row["direction"]) if row.get("direction") else None,
                ))

        normalized.sort(key=lambda t: (t.timestamp, t.client_order_id, t.side))
        _assign_occurrences(normalized)
        logger.info("Loaded %d backtest trades from CSV", len(normalized))
        return normalized
