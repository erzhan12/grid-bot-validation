"""Historical data provider for backtest.

Provides historical price data from database as TickerEvent stream.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterator, Optional

from gridcore import TickerEvent, EventType
from grid_db import DatabaseFactory, TickerSnapshot, PublicTrade


@dataclass
class DataRangeInfo:
    """Information about available data range."""

    symbol: str
    start_ts: Optional[datetime]
    end_ts: Optional[datetime]
    total_records: int


class HistoricalDataProvider:
    """Provides historical price data for backtest simulation.

    Supports two data sources:
    - TickerSnapshot: Full ticker data with bid/ask/funding
    - PublicTrade: Trade data (price derived from trades)

    Default is TickerSnapshot which provides complete market data.
    """

    def __init__(
        self,
        db: DatabaseFactory,
        symbol: str,
        start_ts: datetime,
        end_ts: datetime,
        batch_size: int = 1000,
        use_trades: bool = False,
    ):
        """Initialize data provider.

        Args:
            db: Database factory for session management.
            symbol: Trading symbol (e.g., 'BTCUSDT').
            start_ts: Start timestamp (inclusive).
            end_ts: End timestamp (inclusive).
            batch_size: Number of records to fetch per batch.
            use_trades: Use PublicTrade instead of TickerSnapshot.
        """
        self._db = db
        self._symbol = symbol
        self._start_ts = start_ts
        self._end_ts = end_ts
        self._batch_size = batch_size
        self._use_trades = use_trades

    def __iter__(self) -> Iterator[TickerEvent]:
        """Iterate over historical data as TickerEvents.

        Yields TickerEvents in chronological order.
        """
        if self._use_trades:
            yield from self._iterate_trades()
        else:
            yield from self._iterate_tickers()

    def _iterate_tickers(self) -> Iterator[TickerEvent]:
        """Iterate over TickerSnapshot records.

        Uses cursor-based pagination on exchange_ts for O(1) page fetches.
        Safe because (symbol, exchange_ts) has a unique constraint.
        """
        with self._db.get_session() as session:
            cursor_ts = self._start_ts
            use_gte = True  # First query uses >=, subsequent use >

            while True:
                query = (
                    session.query(TickerSnapshot)
                    .filter(TickerSnapshot.symbol == self._symbol)
                    .filter(TickerSnapshot.exchange_ts <= self._end_ts)
                )
                if use_gte:
                    query = query.filter(TickerSnapshot.exchange_ts >= cursor_ts)
                else:
                    query = query.filter(TickerSnapshot.exchange_ts > cursor_ts)

                snapshots = (
                    query.order_by(TickerSnapshot.exchange_ts)
                    .limit(self._batch_size)
                    .all()
                )

                if not snapshots:
                    break

                for snapshot in snapshots:
                    yield TickerEvent(
                        event_type=EventType.TICKER,
                        symbol=snapshot.symbol,
                        exchange_ts=snapshot.exchange_ts,
                        local_ts=snapshot.local_ts,
                        last_price=snapshot.last_price,
                        mark_price=snapshot.mark_price,
                        bid1_price=snapshot.bid1_price,
                        ask1_price=snapshot.ask1_price,
                        funding_rate=snapshot.funding_rate,
                    )

                if len(snapshots) < self._batch_size:
                    break

                cursor_ts = snapshots[-1].exchange_ts
                use_gte = False

    def _iterate_trades(self) -> Iterator[TickerEvent]:
        """Iterate over PublicTrade records, converting to TickerEvents.

        Uses composite cursor (exchange_ts, id) because multiple trades
        can share the same timestamp. The id column breaks ties.

        Note: Trades don't have bid/ask/funding, so we use price for all.
        """
        with self._db.get_session() as session:
            from sqlalchemy import tuple_

            cursor_ts = self._start_ts
            cursor_id = 0
            is_first = True  # First query uses >= on ts, subsequent use composite >

            while True:
                query = (
                    session.query(PublicTrade)
                    .filter(PublicTrade.symbol == self._symbol)
                    .filter(PublicTrade.exchange_ts <= self._end_ts)
                )
                if is_first:
                    query = query.filter(PublicTrade.exchange_ts >= cursor_ts)
                else:
                    # Composite cursor: skip rows at or before (cursor_ts, cursor_id)
                    query = query.filter(
                        tuple_(PublicTrade.exchange_ts, PublicTrade.id)
                        > tuple_(cursor_ts, cursor_id)
                    )

                trades = (
                    query.order_by(PublicTrade.exchange_ts, PublicTrade.id)
                    .limit(self._batch_size)
                    .all()
                )

                if not trades:
                    break

                for trade in trades:
                    yield TickerEvent(
                        event_type=EventType.TICKER,
                        symbol=trade.symbol,
                        exchange_ts=trade.exchange_ts,
                        local_ts=trade.local_ts,
                        last_price=trade.price,
                        mark_price=trade.price,  # Use trade price
                        bid1_price=trade.price,  # Approximate
                        ask1_price=trade.price,  # Approximate
                        funding_rate=Decimal("0"),  # Not available
                    )

                if len(trades) < self._batch_size:
                    break

                cursor_ts = trades[-1].exchange_ts
                cursor_id = trades[-1].id
                is_first = False

    def get_data_range_info(self) -> DataRangeInfo:
        """Get information about available data range.

        Returns:
            DataRangeInfo with actual start/end and record count.
        """
        with self._db.get_session() as session:
            if self._use_trades:
                model = PublicTrade
            else:
                model = TickerSnapshot

            # Get count and range
            query = (
                session.query(model)
                .filter(model.symbol == self._symbol)
                .filter(model.exchange_ts >= self._start_ts)
                .filter(model.exchange_ts <= self._end_ts)
            )

            total_records = query.count()

            # Get actual min/max timestamps
            from sqlalchemy import func

            result = (
                session.query(
                    func.min(model.exchange_ts),
                    func.max(model.exchange_ts),
                )
                .filter(model.symbol == self._symbol)
                .filter(model.exchange_ts >= self._start_ts)
                .filter(model.exchange_ts <= self._end_ts)
                .first()
            )

            actual_start = result[0] if result else None
            actual_end = result[1] if result else None

            return DataRangeInfo(
                symbol=self._symbol,
                start_ts=actual_start,
                end_ts=actual_end,
                total_records=total_records,
            )


class InMemoryDataProvider:
    """In-memory data provider for testing.

    Accepts pre-created TickerEvents for testing without database.
    """

    def __init__(self, events: list[TickerEvent]):
        """Initialize with list of events.

        Args:
            events: Pre-created TickerEvents (should be in chronological order).
        """
        self._events = events

    def __iter__(self) -> Iterator[TickerEvent]:
        """Iterate over events."""
        yield from self._events

    def get_data_range_info(self) -> DataRangeInfo:
        """Get data range info from events."""
        if not self._events:
            return DataRangeInfo(
                symbol="",
                start_ts=None,
                end_ts=None,
                total_records=0,
            )

        return DataRangeInfo(
            symbol=self._events[0].symbol,
            start_ts=self._events[0].exchange_ts,
            end_ts=self._events[-1].exchange_ts,
            total_records=len(self._events),
        )
