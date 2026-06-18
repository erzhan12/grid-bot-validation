"""Execution repositories (split from repositories.py, feature 0081 / issue #184)."""

from datetime import datetime
from typing import Optional, List

from sqlalchemy import func, tuple_, insert
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from grid_db.models import (
    PrivateExecution, Order,
)
from grid_db.repositories.base import BaseRepository


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
            List of PrivateExecution instances ordered by
            ``(exchange_ts, exec_id)``. The secondary sort makes the order
            deterministic for same-timestamp executions — required by the
            event_follower replay mode (feature 0072), which consumes this
            stream with a forward-only cursor. This repository is the single
            sort site; consumers must not re-sort.
        """
        return (
            self.session.query(PrivateExecution)
            .filter(
                PrivateExecution.run_id == run_id,
                PrivateExecution.exchange_ts >= start_ts,
                PrivateExecution.exchange_ts <= end_ts,
            )
            .order_by(PrivateExecution.exchange_ts, PrivateExecution.exec_id)
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


