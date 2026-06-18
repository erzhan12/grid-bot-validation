"""Snapshot repositories (split from repositories.py, feature 0081 / issue #184)."""

from datetime import datetime
from typing import Optional, List

from sqlalchemy import func, or_, and_, insert
from sqlalchemy.orm import Session
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from grid_db.models import (
    Run,
    PositionSnapshot, WalletSnapshot, GridStateSnapshot,
)
from grid_db.repositories.base import BaseRepository


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

    def get_all_coins_latest_before(
        self,
        run_id: str,
        account_id: str,
        at_ts: datetime,
    ) -> List[WalletSnapshot]:
        """Get the latest wallet snapshot per coin at-or-before ``at_ts``.

        Feature 0065 (multi-coin collateral seed). Groups by ``coin`` and
        returns the row with the max ``exchange_ts`` (<= ``at_ts``) for each
        coin. Run-scoped — pre-0029 rows with NULL ``run_id`` are excluded
        (same contract as ``get_latest_before``). Bybit's UTA wallet WS pushes
        only changed coins, so a quiet collateral coin may carry a stale row;
        callers (``load_collateral_seed``) handle staleness separately.

        Args:
            run_id: Recorder run identifier.
            account_id: Account ID.
            at_ts: Inclusive upper bound on ``exchange_ts``.

        Returns:
            One ``WalletSnapshot`` per coin (latest at-or-before ``at_ts``);
            empty list when no rows match. On an ``exchange_ts`` tie for a coin
            (two pushes at the same instant — there is no unique constraint on
            ``(run_id, account_id, coin, exchange_ts)``) the join yields both
            rows; results are ordered ``(coin, id)`` so a caller that keeps the
            LAST row per coin gets the highest ``id`` (latest insert) — a stable
            tie-break matching ``GridStateSnapshotRepository.get_at_or_before``.
        """
        subq = (
            self.session.query(
                WalletSnapshot.coin.label("coin"),
                func.max(WalletSnapshot.exchange_ts).label("max_ts"),
            )
            .filter(
                WalletSnapshot.run_id == run_id,
                WalletSnapshot.account_id == account_id,
                WalletSnapshot.exchange_ts <= at_ts,
            )
            .group_by(WalletSnapshot.coin)
            .subquery()
        )
        return (
            self.session.query(WalletSnapshot)
            .join(
                subq,
                and_(
                    WalletSnapshot.coin == subq.c.coin,
                    WalletSnapshot.exchange_ts == subq.c.max_ts,
                ),
            )
            .filter(
                WalletSnapshot.run_id == run_id,
                WalletSnapshot.account_id == account_id,
            )
            .order_by(WalletSnapshot.coin, WalletSnapshot.id)
            .all()
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

    def get_active_run_at_or_before(
        self,
        strat_id: str,
        symbol: str,
        at_ts: datetime,
    ) -> Optional[GridStateSnapshot]:
        """Latest live/shadow grid snapshot for ``strat_id`` at-or-before ``at_ts``.

        Same run-active guard as :meth:`get_at_or_before`, but without an
        ``account_id`` predicate. Used when ``seed.account_id`` does not match
        the account stamped on gridbot snapshot rows (legacy Phase 4 DBs).

        Assumes ``(strat_id, symbol)`` is account-unique among grid-writing
        runs — true for the Phase 4 1:1 recorder↔gridbot↔strategy topology.
        If two distinct accounts ever ran the same ``strat_id``+``symbol`` with
        overlapping live/shadow runs in one shared DB, ``ORDER BY exchange_ts
        DESC`` would pick the globally-latest, which may be the wrong account.
        Only reached as a fallback after the account-scoped lookup misses.
        """
        return (
            self.session.query(GridStateSnapshot)
            .join(Run, Run.run_id == GridStateSnapshot.run_id)
            .filter(
                GridStateSnapshot.strat_id == strat_id,
                GridStateSnapshot.symbol == symbol,
                GridStateSnapshot.exchange_ts <= at_ts,
                Run.run_type.in_(("live", "shadow")),
                Run.start_ts <= at_ts,
                or_(Run.end_ts.is_(None), Run.end_ts >= at_ts),
            )
            .order_by(
                GridStateSnapshot.exchange_ts.desc(),
                GridStateSnapshot.id.desc(),
            )
            .first()
        )
