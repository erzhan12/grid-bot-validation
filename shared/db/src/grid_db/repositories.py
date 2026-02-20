"""Repository pattern for database operations with multi-tenant filtering."""

from typing import Generic, TypeVar, Optional, List

from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy import insert

from datetime import datetime

from grid_db.models import (
    Base,
    User,
    BybitAccount,
    ApiCredential,
    Strategy,
    Run,
    PublicTrade,
    TickerSnapshot,
    PrivateExecution,
    Order,
    PositionSnapshot,
    WalletSnapshot,
)


T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    """Base repository with common CRUD operations.

    Usage:
        repo = BaseRepository(session, User)
        user = repo.get_by_id("uuid-here")
        all_users = repo.get_all(limit=10)
    """

    def __init__(self, session: Session, model_class: type[T]):
        """Initialize repository with session and model class.

        Args:
            session: SQLAlchemy session instance.
            model_class: The ORM model class to operate on.
        """
        self.session = session
        self.model_class = model_class

    def create(self, entity: T) -> T:
        """Create new entity.

        Args:
            entity: Entity instance to insert.

        Returns:
            The created entity with generated fields populated.
        """
        self.session.add(entity)
        self.session.flush()
        return entity

    def update(self, entity: T) -> T:
        """Update existing entity.

        Args:
            entity: Entity instance to update.

        Returns:
            The updated entity.
        """
        merged = self.session.merge(entity)
        self.session.flush()
        return merged

    def delete(self, entity: T) -> None:
        """Delete entity.

        Args:
            entity: Entity instance to delete.
        """
        self.session.delete(entity)
        self.session.flush()


class UserRepository(BaseRepository[User]):
    """Repository for User operations."""

    def __init__(self, session: Session):
        super().__init__(session, User)

    def get_by_id(self, id: str) -> Optional[User]:
        """Get user by ID.

        Args:
            id: User ID.

        Returns:
            User instance or None.
        """
        return self.session.get(User, id)

    def get_all(self, limit: int = 100, offset: int = 0) -> List[User]:
        """Get all users.

        Args:
            limit: Max users.
            offset: Offset.

        Returns:
            List of users.
        """
        return self.session.query(User).limit(limit).offset(offset).all()

    def get_by_username(self, username: str) -> Optional[User]:
        """Find user by username.

        Args:
            username: The username to search for.

        Returns:
            User instance or None if not found.
        """
        return (
            self.session.query(User)
            .filter(User.username == username)
            .first()
        )

    def get_active_users(self) -> List[User]:
        """Get all active users.

        Returns:
            List of users with status='active'.
        """
        return (
            self.session.query(User)
            .filter(User.status == "active")
            .all()
        )


class BybitAccountRepository(BaseRepository[BybitAccount]):
    """Repository for BybitAccount operations."""

    def __init__(self, session: Session):
        super().__init__(session, BybitAccount)

    def get_by_user_id(self, user_id: str) -> List[BybitAccount]:
        """Get all accounts for a user.

        Args:
            user_id: The user's ID.

        Returns:
            List of BybitAccount instances.
        """
        return (
            self.session.query(BybitAccount)
            .filter(BybitAccount.user_id == user_id)
            .all()
        )

    def get_enabled_accounts(self, user_id: str) -> List[BybitAccount]:
        """Get all enabled accounts for a user.

        Args:
            user_id: The user's ID.

        Returns:
            List of enabled BybitAccount instances.
        """
        return (
            self.session.query(BybitAccount)
            .filter(
                BybitAccount.user_id == user_id,
                BybitAccount.status == "enabled",
            )
            .all()
        )


class ApiCredentialRepository(BaseRepository[ApiCredential]):
    """Repository for ApiCredential operations."""

    def __init__(self, session: Session):
        super().__init__(session, ApiCredential)

    def get_by_account_id(self, user_id: str, account_id: str) -> List[ApiCredential]:
        """Get all credentials for an account with user ownership check.

        Args:
            user_id: The user's ID for access control.
            account_id: The account's ID.

        Returns:
            List of ApiCredential instances owned by the user.
        """
        return (
            self.session.query(ApiCredential)
            .join(BybitAccount)
            .filter(
                BybitAccount.user_id == user_id,
                ApiCredential.account_id == account_id,
            )
            .all()
        )

    def get_active_credential(self, user_id: str, account_id: str) -> Optional[ApiCredential]:
        """Get the active credential for an account with user ownership check.

        Args:
            user_id: The user's ID for access control.
            account_id: The account's ID.

        Returns:
            Active ApiCredential or None.
        """
        return (
            self.session.query(ApiCredential)
            .join(BybitAccount)
            .filter(
                BybitAccount.user_id == user_id,
                ApiCredential.account_id == account_id,
                ApiCredential.is_active.is_(True),
            )
            .first()
        )


class StrategyRepository(BaseRepository[Strategy]):
    """Repository for Strategy operations."""

    def __init__(self, session: Session):
        super().__init__(session, Strategy)

    def get_by_account_id(self, user_id: str, account_id: str) -> List[Strategy]:
        """Get all strategies for an account with user ownership check.

        Args:
            user_id: The user's ID for access control.
            account_id: The account's ID.

        Returns:
            List of Strategy instances owned by the user.
        """
        return (
            self.session.query(Strategy)
            .join(BybitAccount)
            .filter(
                BybitAccount.user_id == user_id,
                Strategy.account_id == account_id,
            )
            .all()
        )

    def get_enabled_strategies(self, user_id: str, account_id: str) -> List[Strategy]:
        """Get enabled strategies for an account with user ownership check.

        Args:
            user_id: The user's ID for access control.
            account_id: The account's ID.

        Returns:
            List of enabled Strategy instances owned by the user.
        """
        return (
            self.session.query(Strategy)
            .join(BybitAccount)
            .filter(
                BybitAccount.user_id == user_id,
                Strategy.account_id == account_id,
                Strategy.is_enabled.is_(True),
            )
            .all()
        )

    def get_by_symbol(self, user_id: str, account_id: str, symbol: str) -> Optional[Strategy]:
        """Get strategy for a specific symbol with user ownership check.

        Args:
            user_id: The user's ID for access control.
            account_id: The account's ID.
            symbol: Trading symbol (e.g., 'BTCUSDT').

        Returns:
            Strategy instance or None.
        """
        return (
            self.session.query(Strategy)
            .join(BybitAccount)
            .filter(
                BybitAccount.user_id == user_id,
                Strategy.account_id == account_id,
                Strategy.symbol == symbol,
            )
            .first()
        )


class RunRepository(BaseRepository[Run]):
    """Repository for Run operations with multi-tenant filtering.

    CRITICAL: All queries filter by user_id to enforce data isolation.
    """

    def __init__(self, session: Session):
        super().__init__(session, Run)

    def get_by_user_id(self, user_id: str, limit: int = 100) -> List[Run]:
        """Get runs for a user (multi-tenant access control).

        Args:
            user_id: The user's ID for access filtering.
            limit: Maximum number of runs to return.

        Returns:
            List of Run instances owned by the user.
        """
        return (
            self.session.query(Run)
            .filter(Run.user_id == user_id)
            .order_by(Run.start_ts.desc())
            .limit(limit)
            .all()
        )

    def get_running(self, user_id: str) -> List[Run]:
        """Get currently running runs for a user.

        Args:
            user_id: The user's ID for access filtering.

        Returns:
            List of runs with status='running'.
        """
        return (
            self.session.query(Run)
            .filter(
                Run.user_id == user_id,
                Run.status == "running",
            )
            .all()
        )

    def get_by_account_id(self, user_id: str, account_id: str) -> List[Run]:
        """Get runs for a specific account (with user filtering).

        Args:
            user_id: The user's ID for access filtering.
            account_id: The account's ID.

        Returns:
            List of Run instances for the account.
        """
        return (
            self.session.query(Run)
            .filter(
                Run.user_id == user_id,
                Run.account_id == account_id,
            )
            .order_by(Run.start_ts.desc())
            .all()
        )

    def get_by_run_type(self, user_id: str, run_type: str) -> List[Run]:
        """Get runs by type for a user.

        Args:
            user_id: The user's ID for access filtering.
            run_type: Run type ('live', 'backtest', 'shadow').

        Returns:
            List of Run instances of the specified type.
        """
        return (
            self.session.query(Run)
            .filter(
                Run.user_id == user_id,
                Run.run_type == run_type,
            )
            .order_by(Run.start_ts.desc())
            .all()
        )

    def get_latest_by_type(
        self,
        run_type: str,
        statuses: tuple[str, ...] = ("completed", "running"),
    ) -> Optional[Run]:
        """Get the most recent run of a given type.

        Unlike other methods, does NOT filter by user_id — intended for
        standalone tools (e.g. replay engine) that need to auto-discover
        the latest recording run regardless of user.

        Args:
            run_type: Run type (e.g. 'recording', 'live', 'backtest').
            statuses: Tuple of acceptable statuses to filter by.
                Defaults to ("completed", "running") to skip failed runs.

        Returns:
            Most recent Run of that type, or None if none exist.
        """
        query = (
            self.session.query(Run)
            .filter(Run.run_type == run_type)
        )
        if statuses:
            query = query.filter(Run.status.in_(statuses))
        return query.order_by(Run.start_ts.desc()).first()


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


class PrivateExecutionRepository(BaseRepository[PrivateExecution]):
    """Repository for PrivateExecution operations.

    Operations are scoped by run_id or account_id for data isolation.
    """

    def __init__(self, session: Session):
        super().__init__(session, PrivateExecution)

    def get_by_run_range(
        self,
        run_id: str,
        start_ts: datetime,
        end_ts: datetime,
    ) -> List[PrivateExecution]:
        """Get executions for a run within a time range.

        Args:
            run_id: The run ID.
            start_ts: Start timestamp (inclusive).
            end_ts: End timestamp (inclusive).

        Returns:
            List of PrivateExecution instances ordered by exchange_ts.
        """
        return (
            self.session.query(PrivateExecution)
            .filter(
                PrivateExecution.run_id == run_id,
                PrivateExecution.exchange_ts >= start_ts,
                PrivateExecution.exchange_ts <= end_ts,
            )
            .order_by(PrivateExecution.exchange_ts)
            .all()
        )

    def get_last_execution_ts(self, account_id: str) -> Optional[datetime]:
        """Get timestamp of the last execution for an account.

        Args:
            account_id: The account ID.

        Returns:
            Timestamp of the last execution or None if no executions exist.
        """
        result = (
            self.session.query(PrivateExecution.exchange_ts)
            .filter(PrivateExecution.account_id == account_id)
            .order_by(PrivateExecution.exchange_ts.desc())
            .first()
        )
        return result[0] if result else None

    def exists_by_exec_id(self, exec_id: str) -> bool:
        """Check if an execution with the given exec_id exists.

        Useful for deduplication during gap reconciliation.

        Args:
            exec_id: The exchange execution ID.

        Returns:
            True if execution exists, False otherwise.
        """
        return self.session.query(
            self.session.query(PrivateExecution)
            .filter(PrivateExecution.exec_id == exec_id)
            .exists()
        ).scalar()

    def bulk_insert(self, executions: List[PrivateExecution]) -> int:
        """Bulk insert executions for efficient data insertion.

        Uses ON CONFLICT DO NOTHING to skip duplicate exec_ids silently.

        Args:
            executions: List of PrivateExecution instances to insert.

        Returns:
            Number of executions inserted (excluding duplicates).
        """
        if not executions:
            return 0

        # Convert ORM instances to dict for insert
        executions_data = [
            {
                "run_id": e.run_id,
                "account_id": e.account_id,
                "symbol": e.symbol,
                "exec_id": e.exec_id,
                "order_id": e.order_id,
                "order_link_id": e.order_link_id,
                "exchange_ts": e.exchange_ts,
                "side": e.side,
                "exec_price": e.exec_price,
                "exec_qty": e.exec_qty,
                "exec_fee": e.exec_fee,
                "closed_pnl": e.closed_pnl,
                "raw_json": e.raw_json,
            }
            for e in executions
        ]

        # Use dialect-specific insert for ON CONFLICT support
        db_dialect = self.session.get_bind().dialect.name
        if db_dialect == "postgresql":
            stmt = postgresql_insert(PrivateExecution).values(executions_data)
            stmt = stmt.on_conflict_do_nothing(index_elements=["exec_id"])
        elif db_dialect == "sqlite":
            stmt = sqlite_insert(PrivateExecution).values(executions_data)
            stmt = stmt.on_conflict_do_nothing(index_elements=["exec_id"])
        else:
            # Fallback for unsupported dialects - no conflict handling
            stmt = insert(PrivateExecution).values(executions_data)

        result = self.session.execute(stmt)
        self.session.flush()

        # Return rowcount (number of rows actually inserted, excluding skipped duplicates)
        return result.rowcount if result.rowcount else 0

    def get_by_order_link_id(self, run_id: str, order_link_id: str) -> List[PrivateExecution]:
        """Get executions by client order ID (order_link_id).

        Useful for matching executions with grid levels.

        Args:
            run_id: The run ID.
            order_link_id: The client order ID.

        Returns:
            List of PrivateExecution instances.
        """
        return (
            self.session.query(PrivateExecution)
            .filter(
                PrivateExecution.run_id == run_id,
                PrivateExecution.order_link_id == order_link_id,
            )
            .order_by(PrivateExecution.exchange_ts)
            .all()
        )


class OrderRepository(BaseRepository[Order]):
    """Repository for Order operations."""

    def __init__(self, session: Session):
        super().__init__(session, Order)

    def get_by_run_range(
        self, run_id: str, start_ts: datetime, end_ts: datetime
    ) -> List[Order]:
        """Get orders for a run within a time range.

        Args:
            run_id: The run ID.
            start_ts: Start timestamp (inclusive).
            end_ts: End timestamp (inclusive).

        Returns:
            List of Order instances ordered by exchange_ts.
        """
        return (
            self.session.query(Order)
            .filter(
                Order.run_id == run_id,
                Order.exchange_ts >= start_ts,
                Order.exchange_ts <= end_ts,
            )
            .order_by(Order.exchange_ts)
            .all()
        )

    def get_last_order_ts(self, account_id: str) -> Optional[datetime]:
        """Get timestamp of the last order for an account.

        Args:
            account_id: The account ID.

        Returns:
            Timestamp of the last order or None if no orders exist.
        """
        result = (
            self.session.query(Order.exchange_ts)
            .filter(Order.account_id == account_id)
            .order_by(Order.exchange_ts.desc())
            .first()
        )
        return result[0] if result else None

    def bulk_insert(self, orders: List[Order]) -> int:
        """Bulk insert orders for efficient high-volume data insertion.

        Uses ON CONFLICT DO UPDATE to store the latest state for each order_id.

        Args:
            orders: List of Order instances to insert.

        Returns:
            Number of orders inserted/updated.
        """
        if not orders:
            return 0

        # Convert to dict for bulk insert
        orders_data = [
            {
                "run_id": order.run_id,
                "account_id": order.account_id,
                "order_id": order.order_id,
                "order_link_id": order.order_link_id,
                "symbol": order.symbol,
                "exchange_ts": order.exchange_ts,
                "local_ts": order.local_ts,
                "status": order.status,
                "side": order.side,
                "price": order.price,
                "qty": order.qty,
                "leaves_qty": order.leaves_qty,
                "raw_json": order.raw_json,
            }
            for order in orders
        ]

        # Use dialect-specific conflict handling
        dialect_name = self.session.bind.dialect.name

        if dialect_name == "postgresql":
            # PostgreSQL: ON CONFLICT DO UPDATE to keep latest state
            from sqlalchemy.dialects.postgresql import insert

            stmt = insert(Order).values(orders_data)
            stmt = stmt.on_conflict_do_update(
                index_elements=["account_id", "order_id", "exchange_ts"],
                set_={
                    "status": stmt.excluded.status,
                    "leaves_qty": stmt.excluded.leaves_qty,
                    "raw_json": stmt.excluded.raw_json,
                },
            )
        elif dialect_name == "sqlite":
            # SQLite: ON CONFLICT REPLACE (keeps latest)
            from sqlalchemy.dialects.sqlite import insert

            stmt = insert(Order).values(orders_data)
            stmt = stmt.on_conflict_do_update(
                index_elements=["account_id", "order_id", "exchange_ts"],
                set_={
                    "status": stmt.excluded.status,
                    "leaves_qty": stmt.excluded.leaves_qty,
                    "raw_json": stmt.excluded.raw_json,
                },
            )
        else:
            # Fallback for unsupported dialects - simple insert
            stmt = insert(Order).values(orders_data)

        result = self.session.execute(stmt)
        self.session.flush()

        # Return rowcount
        return result.rowcount if result.rowcount else 0


class PositionSnapshotRepository(BaseRepository[PositionSnapshot]):
    """Repository for PositionSnapshot operations."""

    def __init__(self, session: Session):
        super().__init__(session, PositionSnapshot)

    def bulk_insert(self, snapshots: List[PositionSnapshot]) -> int:
        """Bulk insert position snapshots for efficient data insertion.

        Args:
            snapshots: List of PositionSnapshot instances to insert.

        Returns:
            Number of snapshots inserted.
        """
        if not snapshots:
            return 0

        # Convert ORM instances to dict for insert
        snapshots_data = [
            {
                "account_id": s.account_id,
                "symbol": s.symbol,
                "exchange_ts": s.exchange_ts,
                "local_ts": s.local_ts,
                "side": s.side,
                "size": s.size,
                "entry_price": s.entry_price,
                "liq_price": s.liq_price,
                "unrealised_pnl": s.unrealised_pnl,
                "raw_json": s.raw_json,
            }
            for s in snapshots
        ]

        stmt = insert(PositionSnapshot).values(snapshots_data)
        result = self.session.execute(stmt)
        self.session.flush()

        return result.rowcount if result.rowcount else 0

    def get_latest_by_account_symbol(
        self,
        account_id: str,
        symbol: str,
    ) -> Optional[PositionSnapshot]:
        """Get the most recent position snapshot for an account/symbol.

        Args:
            account_id: Account ID.
            symbol: Trading symbol.

        Returns:
            Latest PositionSnapshot or None.
        """
        return (
            self.session.query(PositionSnapshot)
            .filter(
                PositionSnapshot.account_id == account_id,
                PositionSnapshot.symbol == symbol,
            )
            .order_by(PositionSnapshot.exchange_ts.desc())
            .first()
        )


class WalletSnapshotRepository(BaseRepository[WalletSnapshot]):
    """Repository for WalletSnapshot operations."""

    def __init__(self, session: Session):
        super().__init__(session, WalletSnapshot)

    def bulk_insert(self, snapshots: List[WalletSnapshot]) -> int:
        """Bulk insert wallet snapshots for efficient data insertion.

        Args:
            snapshots: List of WalletSnapshot instances to insert.

        Returns:
            Number of snapshots inserted.
        """
        if not snapshots:
            return 0

        # Convert ORM instances to dict for insert
        snapshots_data = [
            {
                "account_id": s.account_id,
                "exchange_ts": s.exchange_ts,
                "local_ts": s.local_ts,
                "coin": s.coin,
                "wallet_balance": s.wallet_balance,
                "available_balance": s.available_balance,
                "raw_json": s.raw_json,
            }
            for s in snapshots
        ]

        stmt = insert(WalletSnapshot).values(snapshots_data)
        result = self.session.execute(stmt)
        self.session.flush()

        return result.rowcount if result.rowcount else 0

    def get_by_account_range(
        self,
        account_id: str,
        coin: str,
        start_ts: datetime,
        end_ts: datetime,
    ) -> List[WalletSnapshot]:
        """Get wallet snapshots for an account/coin within a time range.

        Args:
            account_id: Account ID.
            coin: Coin symbol (e.g., 'USDT').
            start_ts: Start timestamp (inclusive).
            end_ts: End timestamp (inclusive).

        Returns:
            List of WalletSnapshot instances ordered by exchange_ts.
        """
        return (
            self.session.query(WalletSnapshot)
            .filter(
                WalletSnapshot.account_id == account_id,
                WalletSnapshot.coin == coin,
                WalletSnapshot.exchange_ts >= start_ts,
                WalletSnapshot.exchange_ts <= end_ts,
            )
            .order_by(WalletSnapshot.exchange_ts)
            .all()
        )

    def get_latest_by_account_coin(
        self,
        account_id: str,
        coin: str,
    ) -> Optional[WalletSnapshot]:
        """Get the most recent wallet snapshot for an account/coin.

        Args:
            account_id: Account ID.
            coin: Coin symbol (e.g., 'USDT').

        Returns:
            Latest WalletSnapshot or None.
        """
        return (
            self.session.query(WalletSnapshot)
            .filter(
                WalletSnapshot.account_id == account_id,
                WalletSnapshot.coin == coin,
            )
            .order_by(WalletSnapshot.exchange_ts.desc())
            .first()
        )
