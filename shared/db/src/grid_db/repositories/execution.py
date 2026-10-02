"""Execution repositories (split from repositories.py, feature 0081 / issue #184)."""

from datetime import UTC, datetime
from typing import Optional, List

from sqlalchemy import func, or_, tuple_, insert
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from grid_db.enums import RecoveryStatus
from grid_db.models import (
    PrivateExecution, Order, PrivateStreamGap, PrivateStreamSession,
)
from grid_db.repositories.base import BaseRepository, RowNotFoundError


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

        A duplicate ``exec_id`` is skipped, except that an existing NULL
        (unknown) ``closed_pnl`` is enriched by an incoming known value — the
        race where a REST backfill row (no per-execution PnL) lands before
        the buffered WS row. A known PnL is never overwritten.

        Args:
            executions: List of PrivateExecution instances to insert.

        Returns:
            Number of rows inserted or enriched; ``len(executions) - result``
            are unchanged duplicates (in-batch duplicates included).
        """
        if not executions:
            return 0

        # In-batch dedupe by exec_id, keeping a known-PnL member: Postgres
        # rejects one upsert touching the same row twice.
        by_exec_id: dict[str, PrivateExecution] = {}
        for e in executions:
            kept = by_exec_id.get(e.exec_id)
            if kept is None or (
                kept.closed_pnl is None and e.closed_pnl is not None
            ):
                by_exec_id[e.exec_id] = e
        executions = list(by_exec_id.values())

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
        if db_dialect in ("postgresql", "sqlite"):
            dialect_insert = (
                postgresql_insert if db_dialect == "postgresql" else sqlite_insert
            )
            stmt = dialect_insert(PrivateExecution).values(executions_data)
            # Predicated so rowcount counts only real inserts/enrichments.
            # Only closed_pnl is enriched; raw_json deliberately keeps the
            # first (e.g. REST) payload as a record of the row's origin.
            stmt = stmt.on_conflict_do_update(
                index_elements=["exec_id"],
                set_={"closed_pnl": stmt.excluded.closed_pnl},
                where=(
                    PrivateExecution.closed_pnl.is_(None)
                    & stmt.excluded.closed_pnl.is_not(None)
                    & (PrivateExecution.account_id == stmt.excluded.account_id)
                ),
            )
        else:
            # Fallback for unsupported dialects - no conflict handling
            stmt = insert(PrivateExecution).values(executions_data)

        result = self.session.execute(stmt)
        self.session.flush()

        # Rows inserted or enriched (unchanged duplicates excluded)
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


_GAP_REASON_MAX_LEN = 500


def _naive_utc(ts: datetime) -> datetime:
    """``ts`` as naive UTC; a naive value is taken to be UTC already."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


class PrivateStreamGapRepository(BaseRepository[PrivateStreamGap]):
    """Private-stream gaps and their REST recovery outcomes (feature 0110)."""

    def __init__(self, session: Session):
        """Initialize repository.

        Args:
            session: SQLAlchemy session instance.
        """
        super().__init__(session, PrivateStreamGap)

    def add_gap(
        self,
        run_id: str,
        account_id: str,
        symbol: str,
        gap_start: datetime,
        gap_end: Optional[datetime],
    ) -> PrivateStreamGap:
        """Record one symbol's gap with a ``pending`` recovery status.

        Args:
            run_id: Recording run the gap belongs to.
            account_id: Account whose private stream dropped.
            symbol: Symbol whose executions need recovery.
            gap_start: Start of the outage.
            gap_end: End of the outage (reconnect time), or None to open the
                gap; :meth:`close_gap` sets it later.

        Returns:
            The flushed row (``id`` populated).
        """
        return self.create(
            PrivateStreamGap(
                run_id=str(run_id),
                account_id=str(account_id),
                symbol=symbol,
                gap_start=gap_start,
                gap_end=gap_end,
                recovery_status=RecoveryStatus.PENDING,
                inserted=0,
                duplicates=0,
            )
        )

    def set_outcome(
        self,
        gap_id: int,
        *,
        run_id: str,
        status: RecoveryStatus,
        inserted: int,
        duplicates: int,
        reason: Optional[str],
    ) -> None:
        """Store a gap's recovery outcome.

        Args:
            gap_id: Row id returned by :meth:`add_gap`.
            run_id: Run the gap belongs to (tenant scope for the lookup).
            status: Final recovery status.
            inserted: Rows inserted or enriched.
            duplicates: Rows already present, unchanged.
            reason: Failure / skip reason, if any.

        Raises:
            RowNotFoundError: No gap row with ``gap_id`` under ``run_id``.
        """
        gap = self._get_in_run(gap_id, run_id)
        gap.recovery_status = status
        gap.inserted = inserted
        gap.duplicates = duplicates
        # Bounded: raw exception text can be long (SQL + params).
        gap.reason = reason[:_GAP_REASON_MAX_LEN] if reason else reason
        self.session.flush()

    def close_gap(self, gap_id: int, *, run_id: str, gap_end: datetime) -> None:
        """Set the end of a gap opened with ``gap_end=None``.

        Args:
            gap_id: Row id returned by :meth:`add_gap`.
            run_id: Run the gap belongs to (tenant scope for the lookup).
            gap_end: Reconnect time.

        Raises:
            RowNotFoundError: No gap row with ``gap_id`` under ``run_id``.
        """
        gap = self._get_in_run(gap_id, run_id)
        gap.gap_end = gap_end
        self.session.flush()

    def list_overlapping(
        self,
        run_id: str,
        account_id: str,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[PrivateStreamGap]:
        """Gaps of one run/account/symbol that overlap ``[start, end]``.

        An open gap (``gap_end`` NULL) overlaps once it started by ``end``.
        Bounds are inclusive: a gap touching ``start`` or ``end`` overlaps.

        Args:
            run_id: Recording run.
            account_id: Account of the private stream.
            symbol: Symbol.
            start: Interval start.
            end: Interval end.

        Returns:
            Overlapping gaps, oldest first.
        """
        return (
            self.session.query(PrivateStreamGap)
            .filter(
                PrivateStreamGap.run_id == str(run_id),
                PrivateStreamGap.account_id == str(account_id),
                PrivateStreamGap.symbol == symbol,
                PrivateStreamGap.gap_start <= _naive_utc(end),
                or_(
                    PrivateStreamGap.gap_end.is_(None),
                    PrivateStreamGap.gap_end >= _naive_utc(start),
                ),
            )
            .order_by(PrivateStreamGap.gap_start, PrivateStreamGap.id)
            .all()
        )

    def _get_in_run(self, gap_id: int, run_id: str) -> PrivateStreamGap:
        gap = (
            self.session.query(PrivateStreamGap)
            .filter(
                PrivateStreamGap.id == gap_id,
                PrivateStreamGap.run_id == str(run_id),
            )
            .one_or_none()
        )
        if gap is None:
            raise RowNotFoundError(
                f"private_stream_gaps row {gap_id} not found for run {run_id}"
            )
        return gap


class PrivateStreamSessionRepository(BaseRepository[PrivateStreamSession]):
    """Private-stream sessions and their coverage checkpoints (feature 0110)."""

    def __init__(self, session: Session):
        """Initialize repository.

        Args:
            session: SQLAlchemy session instance.
        """
        super().__init__(session, PrivateStreamSession)

    def open_session(
        self, run_id: str, account_id: str, connected_at: datetime
    ) -> PrivateStreamSession:
        """Record a confirmed session; its checkpoint starts at ``connected_at``.

        Args:
            run_id: Recording run the session belongs to.
            account_id: Account whose private stream connected.
            connected_at: Time the private session was confirmed ready.

        Returns:
            The flushed row (``id`` populated).
        """
        return self.create(
            PrivateStreamSession(
                run_id=str(run_id),
                account_id=str(account_id),
                connected_at=connected_at,
                last_checkpoint_ts=connected_at,
            )
        )

    def list_for_run(
        self, run_id: str, account_id: str
    ) -> list[PrivateStreamSession]:
        """All sessions of one run/account, oldest first.

        Args:
            run_id: Recording run.
            account_id: Account of the private stream.

        Returns:
            Session rows ordered by ``connected_at``.
        """
        return (
            self.session.query(PrivateStreamSession)
            .filter(
                PrivateStreamSession.run_id == str(run_id),
                PrivateStreamSession.account_id == str(account_id),
            )
            .order_by(PrivateStreamSession.connected_at, PrivateStreamSession.id)
            .all()
        )

    def advance_checkpoint(
        self, session_id: int, *, run_id: str, ts: datetime
    ) -> None:
        """Move the checkpoint to ``ts``; an older ``ts`` is ignored.

        Args:
            session_id: Row id returned by :meth:`open_session`.
            run_id: Run the session belongs to (tenant scope for the lookup).
            ts: New checkpoint time.

        Raises:
            RowNotFoundError: No session row with ``session_id`` under
                ``run_id``.
        """
        row = (
            self.session.query(PrivateStreamSession)
            .filter(
                PrivateStreamSession.id == session_id,
                PrivateStreamSession.run_id == str(run_id),
            )
            .one_or_none()
        )
        if row is None:
            raise RowNotFoundError(
                f"private_stream_sessions row {session_id} not found for run "
                f"{run_id}"
            )
        # SQLite returns naive (UTC) datetimes; compare on naive UTC.
        if _naive_utc(ts) > _naive_utc(row.last_checkpoint_ts):
            row.last_checkpoint_ts = ts
            self.session.flush()
