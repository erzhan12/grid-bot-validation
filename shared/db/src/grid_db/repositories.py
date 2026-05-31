"""Repository pattern for database operations with multi-tenant filtering."""

from typing import Generic, TypeVar, Optional, List

from sqlalchemy import func, or_, tuple_
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
    GridStateSnapshot,
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

        Example::

            repo = RunRepository(session)
            run = repo.get_latest_by_type("recording")
            if run is None:
                raise ValueError("No recording runs found")
            run_id, start_ts, end_ts = run.run_id, run.start_ts, run.end_ts

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

    def close_stale_running_runs(
        self,
        user_id: str,
        account_id: str,
        strategy_id: str,
        run_type: str,
        *,
        end_ts: datetime,
    ) -> int:
        """Mark orphaned open runs completed before a new run starts (0062 / #148).

        After crash/kill the prior process may leave ``status='running'`` and
        ``end_ts=NULL``. Without closing those rows, replay's cross-run grid
        seed lookup can still treat the stale run as active at ``at_ts``.
        """
        runs = (
            self.session.query(Run)
            .filter(
                Run.user_id == user_id,
                Run.account_id == account_id,
                Run.strategy_id == strategy_id,
                Run.run_type == run_type,
                Run.status == "running",
                Run.end_ts.is_(None),
            )
            .all()
        )
        for run in runs:
            run.status = "completed"
            run.end_ts = end_ts
        return len(runs)


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

    def get_active_at(
        self,
        run_id: str,
        account_id: str,
        symbol: str,
        at_ts: datetime,
    ) -> List[Order]:
        """Get the latest active-state snapshot per order for a moment in time.

        Used by the seed-aware replay loader (feature 0029) to reconstruct
        the set of open orders that existed live at ``at_ts``. The ``orders``
        table stores a stream of state-change snapshots, so "active orders
        at at_ts" = "for each order_id in this run/account/symbol, take the
        latest snapshot at-or-before at_ts; keep it iff status is active and
        leaves_qty > 0".

        Run-scoping is mandatory: an order whose terminal update was missed
        because recorder restarted would have a "New" snapshot in a previous
        run that must NOT leak into a later run's seed.

        Args:
            run_id: Recorder run identifier.
            account_id: Account ID.
            symbol: Trading symbol.
            at_ts: Inclusive upper bound on ``exchange_ts``.

        Returns:
            List of latest-per-order Order rows whose latest state at at_ts
            is ``'New'`` or ``'PartiallyFilled'`` AND ``leaves_qty > 0``.
        """
        # Subquery: for each order_id in this scope, the latest exchange_ts
        # at-or-before at_ts. Composite (order_id, max_ts) is then joined
        # back to the Order table to fetch the full row.
        latest_per_order = (
            self.session.query(
                Order.order_id.label("oid"),
                func.max(Order.exchange_ts).label("max_ts"),
            )
            .filter(
                Order.run_id == run_id,
                Order.account_id == account_id,
                Order.symbol == symbol,
                Order.exchange_ts <= at_ts,
            )
            .group_by(Order.order_id)
            .subquery()
        )

        return (
            self.session.query(Order)
            .join(
                latest_per_order,
                tuple_(Order.order_id, Order.exchange_ts)
                == tuple_(latest_per_order.c.oid, latest_per_order.c.max_ts),
            )
            .filter(
                Order.run_id == run_id,
                Order.account_id == account_id,
                Order.symbol == symbol,
                Order.status.in_(("New", "PartiallyFilled")),
                Order.leaves_qty > 0,
            )
            .all()
        )

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
                # 0029: persist reduce_only for active-order seed direction
                # derivation. None for pre-0029 callers, treated as
                # SeedSchemaError by the loader.
                "reduce_only": order.reduce_only,
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
                "run_id": s.run_id,  # 0029: run-scoped seed lookups
                "account_id": s.account_id,
                "symbol": s.symbol,
                "exchange_ts": s.exchange_ts,
                "local_ts": s.local_ts,
                "side": s.side,
                "size": s.size,
                "entry_price": s.entry_price,
                "liq_price": s.liq_price,
                "unrealised_pnl": s.unrealised_pnl,
                # 0034: position telemetry parity columns.
                "source": s.source if s.source is not None else "live",
                "mark_price": s.mark_price,
                "position_im": s.position_im,
                "position_mm": s.position_mm,
                "cum_realised_pnl": s.cum_realised_pnl,
                "cur_realised_pnl": s.cur_realised_pnl,
                "position_value": s.position_value,  # 0059
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
        source: Optional[str] = "live",
    ) -> Optional[PositionSnapshot]:
        """Get the most recent position snapshot for an account/symbol.

        Args:
            account_id: Account ID.
            symbol: Trading symbol.
            source: Snapshot source filter (0034). Defaults to ``'live'`` so
                legacy callers never silently mix in backtest rows. Pass
                ``'backtest'`` to read backtest rows, or ``None`` to read the
                union (rare — used by the comparator).

        Returns:
            Latest PositionSnapshot or None.
        """
        query = self.session.query(PositionSnapshot).filter(
            PositionSnapshot.account_id == account_id,
            PositionSnapshot.symbol == symbol,
        )
        if source is not None:
            query = query.filter(PositionSnapshot.source == source)
        return query.order_by(PositionSnapshot.exchange_ts.desc()).first()

    def get_latest_before(
        self,
        run_id: str,
        account_id: str,
        symbol: str,
        side: str,
        at_ts: datetime,
        source: Optional[str] = "live",
    ) -> Optional[PositionSnapshot]:
        """Get the latest snapshot for a run/account/symbol/side at-or-before at_ts.

        Used by the seed-aware replay loader (feature 0029) to scope position
        seeding to one recorder run. Pre-0029 rows have NULL ``run_id`` and
        are excluded.

        Args:
            run_id: Recorder run identifier.
            account_id: Account ID.
            symbol: Trading symbol.
            side: 'Buy' (long) or 'Sell' (short) — Bybit hedge-mode convention.
            at_ts: Inclusive upper bound on ``exchange_ts``.
            source: Snapshot source filter (0034). Defaults to ``'live'``.
                Pass ``'backtest'`` for backtest rows or ``None`` for the
                union.

        Returns:
            Latest matching PositionSnapshot, or None if no row exists.
        """
        query = self.session.query(PositionSnapshot).filter(
            PositionSnapshot.run_id == run_id,
            PositionSnapshot.account_id == account_id,
            PositionSnapshot.symbol == symbol,
            PositionSnapshot.side == side,
            PositionSnapshot.exchange_ts <= at_ts,
        )
        if source is not None:
            query = query.filter(PositionSnapshot.source == source)
        return query.order_by(PositionSnapshot.exchange_ts.desc()).first()


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
                "run_id": s.run_id,  # 0029: run-scoped seed lookups
                "account_id": s.account_id,
                "exchange_ts": s.exchange_ts,
                "local_ts": s.local_ts,
                "coin": s.coin,
                "wallet_balance": s.wallet_balance,
                "available_balance": s.available_balance,
                "total_equity": s.total_equity,
                "total_available_balance": s.total_available_balance,
                "total_margin_balance": s.total_margin_balance,
                "account_im_rate": s.account_im_rate,
                "account_mm_rate": s.account_mm_rate,
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

    def get_latest_before(
        self,
        run_id: str,
        account_id: str,
        coin: str,
        at_ts: datetime,
    ) -> Optional[WalletSnapshot]:
        """Get the latest wallet snapshot for a run/account/coin at-or-before at_ts.

        Used by the seed-aware replay loader (feature 0029). Pre-0029 rows
        have NULL ``run_id`` and are excluded.

        Args:
            run_id: Recorder run identifier.
            account_id: Account ID.
            coin: Coin symbol (e.g., 'USDT').
            at_ts: Inclusive upper bound on ``exchange_ts``.

        Returns:
            Latest matching WalletSnapshot, or None if no row exists.
        """
        return (
            self.session.query(WalletSnapshot)
            .filter(
                WalletSnapshot.run_id == run_id,
                WalletSnapshot.account_id == account_id,
                WalletSnapshot.coin == coin,
                WalletSnapshot.exchange_ts <= at_ts,
            )
            .order_by(WalletSnapshot.exchange_ts.desc())
            .first()
        )


class GridStateSnapshotRepository(BaseRepository[GridStateSnapshot]):
    """Repository for GridStateSnapshot operations (feature 0047).

    Insert path is single-row with ``ON CONFLICT DO NOTHING`` against the
    partial unique index ``uq_grid_state_snapshots_fingerprint_at_ts`` (only
    rows with non-NULL ``raw_fingerprint`` participate). The replay loader
    reads back via ``get_at_or_before`` which orders by
    ``exchange_ts DESC, id DESC`` — the latter breaks ties for
    multi-notify outer mutations (e.g. ``update_grid`` out-of-bounds path
    emits post-rebuild then post-side-assignment at the same ts; the
    largest id wins, which by writer FIFO contract is the final snapshot).
    """

    def __init__(self, session: Session):
        super().__init__(session, GridStateSnapshot)

    def insert(self, snapshot: GridStateSnapshot) -> int:
        """Insert a single snapshot, no-op on partial-index conflict.

        Returns rowcount (0 if dedup'd by the partial unique constraint).
        """
        snapshot_data = {
            "run_id": snapshot.run_id,
            "account_id": snapshot.account_id,
            "strat_id": snapshot.strat_id,
            "symbol": snapshot.symbol,
            "exchange_ts": snapshot.exchange_ts,
            "local_ts": snapshot.local_ts,
            "grid_json": snapshot.grid_json,
            "grid_step": snapshot.grid_step,
            "grid_count": snapshot.grid_count,
            "raw_fingerprint": snapshot.raw_fingerprint,
        }

        # index_where MUST match the partial-index WHERE predicate or
        # PostgreSQL won't bind the conflict target to the partial constraint
        # (and SQLite >=3.24 partial-index ON CONFLICT behaves the same way).
        index_elements = [
            "run_id", "account_id", "strat_id", "exchange_ts", "raw_fingerprint",
        ]
        index_where = GridStateSnapshot.raw_fingerprint.is_not(None)

        db_dialect = self.session.get_bind().dialect.name
        if db_dialect == "postgresql":
            stmt = postgresql_insert(GridStateSnapshot).values(**snapshot_data)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=index_elements,
                index_where=index_where,
            )
        elif db_dialect == "sqlite":
            stmt = sqlite_insert(GridStateSnapshot).values(**snapshot_data)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=index_elements,
                index_where=index_where,
            )
        else:
            stmt = insert(GridStateSnapshot).values(**snapshot_data)

        result = self.session.execute(stmt)
        self.session.flush()
        return result.rowcount if result.rowcount else 0

    def get_latest(
        self,
        run_id: str,
        account_id: str,
        strat_id: str,
    ) -> Optional[GridStateSnapshot]:
        """Latest grid snapshot for the scope — **per-run** by design.

        Consumed by ``GridStateWriter.get_last_fingerprint`` and the
        orchestrator bootstrap probe (``_bootstrap_grid_snapshots``,
        issue #108). Both deliberately depend on per-``run_id`` scoping:
        the writer's in-memory dedupe gate must seed from the CURRENT
        run, and the bootstrap probe alerts when a stale row from a
        prior process appears under the same run_id (run_id reuse).

        Note the intentional asymmetry with ``get_at_or_before``: the
        replay seed loader uses cross-run lookup by
        ``(account_id, strat_id, symbol)`` because gridbot and recorder
        run under independent ``run_id``s.

        ``ORDER BY`` matches ``get_at_or_before`` (without the ts predicate).
        """
        return (
            self.session.query(GridStateSnapshot)
            .filter(
                GridStateSnapshot.run_id == run_id,
                GridStateSnapshot.account_id == account_id,
                GridStateSnapshot.strat_id == strat_id,
            )
            .order_by(
                GridStateSnapshot.exchange_ts.desc(),
                GridStateSnapshot.id.desc(),
            )
            .first()
        )

    def get_at_or_before(
        self,
        account_id: str,
        strat_id: str,
        symbol: str,
        at_ts: datetime,
    ) -> Optional[GridStateSnapshot]:
        """Latest grid snapshot at-or-before ``at_ts`` — **cross-run** lookup.

        Filters on ``(account_id, strat_id, symbol, exchange_ts <= at_ts)``
        with NO ``run_id`` predicate. Used by the replay seed loader
        (0052): gridbot (live ``run_id``) and recorder (recording
        ``run_id``) are independent processes — scoping by recorder's
        ``run_id`` matches zero rows because gridbot wrote under its own.

        Intentional asymmetry with ``get_latest``: see that method's
        docstring. The ``symbol`` predicate prevents cross-symbol bleed
        for accounts whose ``strat_id`` was retained after a symbol
        rename.

        **Run-active guard (feature 0062).** INNER JOINs ``runs`` and
        requires the writer run was **active at ``at_ts``**
        (``start_ts <= at_ts`` AND ``end_ts`` NULL or ``>= at_ts``) AND its
        ``run_type`` is a grid-writing type (``live``/``shadow``; the
        recorder's ``recording`` run never writes grid snapshots). Without
        this guard, after a graceful gridbot restart a completed run's last
        snapshot could seed replay in the gap before the new run's bootstrap
        write — a silently WRONG grid, since feature 0054 only raises when
        *no* row is found. ``end_ts`` is **inclusive**: a run that ended
        exactly at ``at_ts`` still owns the grid state at that instant.
        Known gap: an *unclean* shutdown can leave the old run
        ``end_ts=NULL`` (still matching) until startup orphan cleanup closes
        it — see RULES.md / feature 0062 §4.6.

        Overlapping live+shadow on the same key are both eligible; the
        ``ORDER BY`` below deterministically picks the most recent.

        ``ORDER BY exchange_ts DESC, id DESC`` — the secondary ``id`` sort
        picks the final notify of a multi-notify outer mutation when both
        snapshots share the same ``exchange_ts``.
        """
        return (
            self.session.query(GridStateSnapshot)
            .join(Run, Run.run_id == GridStateSnapshot.run_id)
            .filter(
                GridStateSnapshot.account_id == account_id,
                GridStateSnapshot.strat_id == strat_id,
                GridStateSnapshot.symbol == symbol,
                GridStateSnapshot.exchange_ts <= at_ts,
                Run.run_type.in_(("live", "shadow")),
                Run.start_ts <= at_ts,
                # Orphaned runs (crash/kill) are closed at gridbot startup
                # via RunRepository.close_stale_running_runs (#148).
                or_(Run.end_ts.is_(None), Run.end_ts >= at_ts),
            )
            .order_by(
                GridStateSnapshot.exchange_ts.desc(),
                GridStateSnapshot.id.desc(),
            )
            .first()
        )
