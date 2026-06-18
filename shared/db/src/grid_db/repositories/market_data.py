"""Market-data repositories (split from repositories.py, feature 0081 / issue #184)."""

from datetime import datetime
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import insert
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from grid_db.models import (
    PublicTrade, TickerSnapshot,
)
from grid_db.repositories.base import BaseRepository


class PublicTradeRepository(BaseRepository[PublicTrade]):
    """Repository for PublicTrade operations.

    Optimized for high-volume data with bulk insert support.
    """

    def __init__(self, session: Session):
        super().__init__(session, PublicTrade)

    def get_by_symbol_range(
        self,
        symbol: str,
        start_ts: datetime,
        end_ts: datetime,
        limit: int = 10000,
    ) -> List[PublicTrade]:
        """Get trades for a symbol within a time range.

        Args:
            symbol: Trading symbol (e.g., 'BTCUSDT').
            start_ts: Start timestamp (inclusive).
            end_ts: End timestamp (inclusive).
            limit: Maximum number of trades to return.

        Returns:
            List of PublicTrade instances ordered by exchange_ts.
        """
        return (
            self.session.query(PublicTrade)
            .filter(
                PublicTrade.symbol == symbol,
                PublicTrade.exchange_ts >= start_ts,
                PublicTrade.exchange_ts <= end_ts,
            )
            .order_by(PublicTrade.exchange_ts)
            .limit(limit)
            .all()
        )

    def get_last_trade_ts(self, symbol: str) -> Optional[datetime]:
        """Get timestamp of the last trade for a symbol.

        Args:
            symbol: Trading symbol (e.g., 'BTCUSDT').

        Returns:
            Timestamp of the last trade or None if no trades exist.
        """
        result = (
            self.session.query(PublicTrade.exchange_ts)
            .filter(PublicTrade.symbol == symbol)
            .order_by(PublicTrade.exchange_ts.desc())
            .first()
        )
        return result[0] if result else None

    def bulk_insert(self, trades: List[PublicTrade]) -> int:
        """Bulk insert trades for efficient high-volume data insertion.

        Uses ON CONFLICT DO NOTHING to skip duplicate trade_ids silently.

        Args:
            trades: List of PublicTrade instances to insert.

        Returns:
            Number of trades inserted (excluding duplicates).
        """
        if not trades:
            return 0

        # Convert ORM instances to dict for insert
        trades_data = [
            {
                "symbol": t.symbol,
                "trade_id": t.trade_id,
                "exchange_ts": t.exchange_ts,
                "local_ts": t.local_ts,
                "side": t.side,
                "price": t.price,
                "size": t.size,
            }
            for t in trades
        ]

        # Use dialect-specific insert for ON CONFLICT support
        db_dialect = self.session.get_bind().dialect.name
        if db_dialect == "postgresql":
            stmt = postgresql_insert(PublicTrade).values(trades_data)
            stmt = stmt.on_conflict_do_nothing(index_elements=["trade_id"])
        elif db_dialect == "sqlite":
            stmt = sqlite_insert(PublicTrade).values(trades_data)
            stmt = stmt.on_conflict_do_nothing(index_elements=["trade_id"])
        else:
            # Fallback for unsupported dialects - no conflict handling
            stmt = insert(PublicTrade).values(trades_data)

        result = self.session.execute(stmt)
        self.session.flush()

        # Return rowcount (number of rows actually inserted, excluding skipped duplicates)
        return result.rowcount if result.rowcount else 0

    def exists_by_trade_id(self, trade_id: str) -> bool:
        """Check if a trade with the given trade_id exists.

        Useful for deduplication during gap reconciliation.

        Args:
            trade_id: The exchange trade ID.

        Returns:
            True if trade exists, False otherwise.
        """
        return self.session.query(
            self.session.query(PublicTrade)
            .filter(PublicTrade.trade_id == trade_id)
            .exists()
        ).scalar()


class TickerSnapshotRepository(BaseRepository[TickerSnapshot]):
    """Repository for TickerSnapshot operations."""

    def __init__(self, session: Session):
        super().__init__(session, TickerSnapshot)

    def get_last_ticker_ts(self, symbol: str) -> Optional[datetime]:
        """Get timestamp of the last ticker snapshot for a symbol."""
        result = (
            self.session.query(TickerSnapshot.exchange_ts)
            .filter(TickerSnapshot.symbol == symbol)
            .order_by(TickerSnapshot.exchange_ts.desc())
            .first()
        )
        return result[0] if result else None

    def get_latest_by_symbol(self, symbol: str) -> Optional[TickerSnapshot]:
        """Get most recent ticker snapshot for a symbol."""
        return (
            self.session.query(TickerSnapshot)
            .filter(TickerSnapshot.symbol == symbol)
            .order_by(TickerSnapshot.exchange_ts.desc())
            .first()
        )

    def get_mark_at_or_before(
        self, symbol: str, at_ts: datetime
    ) -> Optional[Decimal]:
        """Get the latest ``mark_price`` for a symbol at-or-before ``at_ts``.

        Feature 0065 (collateral re-marking). Carry-forward semantics: returns
        the most recent ``mark_price`` where ``exchange_ts <= at_ts``, or
        ``None`` when no such row exists. Unlike ``get_latest_by_symbol``
        ("latest overall"), this is seed/tick-time bounded — used for both the
        seed-mark fallback and intra-run collateral mark updates.

        Args:
            symbol: Perp symbol carrying the collateral coin's mark (e.g. 'SOLUSDT').
            at_ts: Inclusive upper bound on ``exchange_ts``.

        Returns:
            Latest ``mark_price`` Decimal, or None if no row at-or-before ``at_ts``.
        """
        result = (
            self.session.query(TickerSnapshot.mark_price)
            .filter(
                TickerSnapshot.symbol == symbol,
                TickerSnapshot.exchange_ts <= at_ts,
            )
            .order_by(TickerSnapshot.exchange_ts.desc())
            .first()
        )
        return result[0] if result else None

    def bulk_insert(self, snapshots: List[TickerSnapshot]) -> int:
        """Bulk insert ticker snapshots with duplicate skipping.

        Uses ON CONFLICT DO NOTHING to skip duplicate (symbol, exchange_ts) rows.
        """
        if not snapshots:
            return 0

        snapshots_data = [
            {
                "symbol": s.symbol,
                "exchange_ts": s.exchange_ts,
                "local_ts": s.local_ts,
                "last_price": s.last_price,
                "mark_price": s.mark_price,
                "bid1_price": s.bid1_price,
                "ask1_price": s.ask1_price,
                "funding_rate": s.funding_rate,
                "raw_json": s.raw_json,
            }
            for s in snapshots
        ]

        db_dialect = self.session.get_bind().dialect.name
        if db_dialect == "postgresql":
            stmt = postgresql_insert(TickerSnapshot).values(snapshots_data)
            stmt = stmt.on_conflict_do_nothing(index_elements=["symbol", "exchange_ts"])
        elif db_dialect == "sqlite":
            stmt = sqlite_insert(TickerSnapshot).values(snapshots_data)
            stmt = stmt.on_conflict_do_nothing(index_elements=["symbol", "exchange_ts"])
        else:
            stmt = insert(TickerSnapshot).values(snapshots_data)

        result = self.session.execute(stmt)
        self.session.flush()
        return result.rowcount if result.rowcount else 0


