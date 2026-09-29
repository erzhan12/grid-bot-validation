"""Gap detection and REST reconciliation for missed WebSocket data."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, UTC, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional
from uuid import UUID

from bybit_adapter.normalizer import parse_exec_pnl
from bybit_adapter.rest_client import BybitRestClient
from grid_db import (
    DatabaseFactory,
    PublicTrade,
    PublicTradeRepository,
    PrivateExecution,
    PrivateExecutionRepository,
    RecoveryStatus,
)


logger = logging.getLogger(__name__)

_PRIVATE_EXECUTION_RECONCILE_MAX_PAGES = 100
# REST window = [gap_start - margin, gap_end + margin]: covers an execution
# still in flight at either edge; exec_id dedup makes the overlap free.
_RECOVERY_WINDOW_MARGIN = timedelta(seconds=5)
# Bybit /v5/execution/list rejects endTime - startTime > 7 days.
_BYBIT_MAX_EXECUTION_WINDOW = timedelta(days=7)


def _is_trade_row(exec_data) -> bool:
    """True for a REST execution row that recovery must persist."""
    return isinstance(exec_data, dict) and exec_data.get("execType") == "Trade"


def recovery_result_from_future(future) -> "ExecutionRecoveryResult":
    """Map a finished recovery future to its outcome.

    Cancelled or crashed recoveries become ``FAILED`` with a reason, so
    callers never mistake them for an empty gap.

    Args:
        future: A done ``concurrent.futures.Future`` wrapping
            :meth:`GapReconciler.reconcile_executions`.

    Returns:
        The recovery outcome.
    """
    if future.cancelled():
        return ExecutionRecoveryResult(
            RecoveryStatus.FAILED, reason="recovery cancelled"
        )
    if (exc := future.exception()) is not None:
        return ExecutionRecoveryResult(
            RecoveryStatus.FAILED, reason=f"recovery crashed: {exc}"
        )
    return future.result()


@dataclass(frozen=True)
class ExecutionRecoveryResult:
    """Outcome of one symbol's REST execution recovery for a gap.

    ``status`` is never ambiguous: an empty but complete query is
    ``RECOVERED`` with zero counts; ``SKIPPED`` (gap below threshold) and
    ``TRUNCATED`` (not persisted) carry a ``reason``; every error path is
    ``FAILED`` with a ``reason``. ``FAILED`` may still carry non-zero counts
    when a partially converted batch persisted its valid rows.

    Attributes:
        status: Final recovery status.
        inserted: Rows inserted or enriched (``bulk_insert`` return).
        duplicates: Rows already present, unchanged.
        reason: Why the recovery was not ``RECOVERED``, else ``None``.
    """

    status: RecoveryStatus
    inserted: int = 0
    duplicates: int = 0
    reason: Optional[str] = None


def _rest_exec_pnl(exec_data: dict) -> Optional[Decimal]:
    """Closed PnL for a REST ``/v5/execution/list`` row, or None if unknown.

    REST rows carry no per-execution PnL, so it stays NULL (unknown) unless
    the row is an opening fill: ``closedSize`` == 0 means nothing was
    closed, so realized PnL (Bybit ``execPnl``/``cashFlow``, gross of fees)
    is exactly zero. Missing/empty/non-zero ``closedSize`` stays unknown;
    an explicit ``execPnl``/``closedPnl`` always wins.
    """
    try:
        pnl = parse_exec_pnl(exec_data)
    except (InvalidOperation, ValueError, TypeError):
        # Malformed PnL field: unknown — never drop the recovered execution.
        return None
    if pnl is not None:
        return pnl
    closed_size = exec_data.get("closedSize")
    if closed_size in (None, ""):
        return None
    try:
        parsed = Decimal(str(closed_size))
    except (InvalidOperation, ValueError, TypeError):
        # Degrade to unknown — never drop the recovered execution.
        return None
    return Decimal("0") if parsed == 0 else None


class GapReconciler:
    """Detects and fills gaps in captured data using REST API.

    When WebSocket disconnections are detected, queries the REST API
    to retrieve missed data and bulk inserts it into the database.

    Responsibilities:
    - Detect gaps from disconnect/reconnect timestamps
    - Query REST API for missing public trades
    - Query REST API for missing executions
    - Deduplicate against existing database records
    - Bulk insert reconciled data

    Example:
        reconciler = GapReconciler(
            db=db_factory,
            rest_client=bybit_client,
            gap_threshold_seconds=5.0,
        )

        # On WebSocket reconnect:
        await reconciler.reconcile_public_trades(
            symbol="BTCUSDT",
            gap_start=disconnect_ts,
            gap_end=reconnect_ts,
        )
    """

    def __init__(
        self,
        db: DatabaseFactory,
        rest_client: BybitRestClient,
        gap_threshold_seconds: float = 5.0,
    ):
        """Initialize gap reconciler.

        Args:
            db: DatabaseFactory for database access.
            rest_client: BybitRestClient for REST API calls.
            gap_threshold_seconds: Minimum gap duration to trigger reconciliation.
        """
        self._db = db
        self._rest_client = rest_client
        self._gap_threshold = gap_threshold_seconds

        # Stats
        self._trades_reconciled = 0
        self._executions_reconciled = 0
        self._reconciliation_count = 0

    def should_reconcile(self, gap_start: datetime, gap_end: datetime) -> bool:
        """Check if gap duration exceeds threshold.

        Args:
            gap_start: Start of gap (disconnect time).
            gap_end: End of gap (reconnect time).

        Returns:
            True if gap exceeds threshold and reconciliation is needed.
        """
        gap_seconds = (gap_end - gap_start).total_seconds()
        return gap_seconds >= self._gap_threshold

    async def reconcile_public_trades(
        self,
        symbol: str,
        gap_start: datetime,
        gap_end: datetime,
    ) -> int:
        """Reconcile public trades for a symbol during a gap period.

        Queries REST API for trades in the gap period and inserts
        any that are not already in the database.

        Args:
            symbol: Trading symbol (e.g., "BTCUSDT").
            gap_start: Start of gap period.
            gap_end: End of gap period.

        Returns:
            Number of trades reconciled.
        """
        if not self.should_reconcile(gap_start, gap_end):
            logger.debug(f"Gap too small for reconciliation: {symbol}")
            return 0

        gap_seconds = (gap_end - gap_start).total_seconds()
        logger.info(
            f"Reconciling public trades for {symbol} "
            f"(gap: {gap_seconds:.1f}s, {gap_start} to {gap_end})"
        )

        try:
            # Query DB for last persisted timestamp
            with self._db.get_session() as session:
                repo = PublicTradeRepository(session)
                last_persisted_ts = repo.get_last_trade_ts(symbol)

            # Never let post-gap live writes move the filter window past the
            # detected outage; duplicates are filtered by trade_id on insert.
            if (
                last_persisted_ts
                and last_persisted_ts.timestamp() < gap_start.timestamp()
            ):
                reconcile_start = last_persisted_ts
            else:
                reconcile_start = gap_start

            # Add buffer to ensure we capture all trades
            start_ms = int((reconcile_start - timedelta(seconds=1)).timestamp() * 1000)
            end_ms = int((gap_end + timedelta(seconds=1)).timestamp() * 1000)

            logger.debug(
                f"Reconciliation window: {reconcile_start} to {gap_end} "
                f"(last_persisted_ts: {last_persisted_ts})"
            )

            # NOTE: Bybit's public trade endpoint only returns the most recent
            # trades (no time-range query support). The local filter below
            # ensures correctness, but reconciliation will miss trades if the
            # gap is older than the window covered by the latest 1000 trades.
            trades_data = await asyncio.to_thread(
                self._rest_client.get_recent_trades,
                symbol=symbol,
                limit=1000,  # Max limit
            )

            if not trades_data:
                logger.debug(f"No trades returned from REST API for {symbol}")
                return 0

            # Check whether the fetched trades actually cover the gap period
            trade_timestamps = [int(t.get("time", 0)) for t in trades_data]
            oldest_fetched_ms = min(trade_timestamps) if trade_timestamps else 0
            if oldest_fetched_ms > start_ms:
                logger.warning(
                    f"Public trade reconciliation for {symbol} has incomplete "
                    f"coverage: oldest fetched trade ({oldest_fetched_ms}) is "
                    f"newer than gap start ({start_ms}). "
                    f"Gap trades before that timestamp are unrecoverable via "
                    f"the recent-trade endpoint."
                )

            # Filter trades within gap period
            gap_trades = []
            for trade in trades_data:
                trade_ts_ms = int(trade.get("time", 0))
                if start_ms <= trade_ts_ms <= end_ms:
                    gap_trades.append(trade)

            if not gap_trades:
                logger.debug(f"No trades in gap period for {symbol}")
                return 0

            # Convert to models
            models = self._trades_to_models(symbol, gap_trades)

            # Bulk insert (database handles duplicates with unique constraint)
            with self._db.get_session() as session:
                repo = PublicTradeRepository(session)
                count = repo.bulk_insert(models)

                if count > 0:
                    self._trades_reconciled += count
                    self._reconciliation_count += 1
                    skipped = len(models) - count
                    logger.info(
                        f"Reconciled {count} public trades for {symbol} "
                        f"(skipped {skipped} duplicates via unique constraint)"
                    )

            return count

        except Exception as e:
            logger.error(f"Error reconciling public trades for {symbol}: {e}")
            return 0

    async def reconcile_executions(
        self,
        user_id: UUID,
        account_id: UUID,
        run_id: Optional[UUID],
        symbol: str,
        gap_start: datetime,
        gap_end: datetime,
        api_key: str,
        api_secret: str,
        testnet: bool,
    ) -> ExecutionRecoveryResult:
        """Recover private executions missed during a gap via REST.

        Queries ``[gap_start - margin, gap_end + margin]`` and inserts the
        executions not already in the database.

        Args:
            user_id: User ID for tagging.
            account_id: Account ID for tagging.
            run_id: Run ID for tagging (required to persist).
            symbol: Trading symbol.
            gap_start: Start of gap period.
            gap_end: End of gap period.
            api_key: API key for authenticated request.
            api_secret: API secret for authenticated request.
            testnet: Use testnet endpoints (from account environment).

        Returns:
            The recovery outcome (never raises): ``SKIPPED`` below the gap
            threshold; ``TRUNCATED`` when pagination stopped at
            ``max_pages`` (nothing persisted); ``FAILED`` for a missing
            ``run_id``, a REST/DB/conversion error, any Trade row dropped in
            conversion, or a window over Bybit's 7-day limit (the most
            recent 7 days are still queried and persisted); else
            ``RECOVERED``.
        """
        if not self.should_reconcile(gap_start, gap_end):
            logger.debug(f"Gap too small for execution reconciliation: {symbol}")
            return ExecutionRecoveryResult(
                RecoveryStatus.SKIPPED, reason="gap below reconcile threshold"
            )
        if run_id is None:
            logger.warning(
                "Cannot reconcile executions for %s without run_id", symbol
            )
            return ExecutionRecoveryResult(
                RecoveryStatus.FAILED, reason="no run_id to persist under"
            )

        query_start = gap_start - _RECOVERY_WINDOW_MARGIN
        query_end = gap_end + _RECOVERY_WINDOW_MARGIN
        clamp_reason: Optional[str] = None
        if query_end - query_start > _BYBIT_MAX_EXECUTION_WINDOW:
            # Recover the queryable tail; the head stays unrecovered (FAILED).
            clamped_start = query_end - _BYBIT_MAX_EXECUTION_WINDOW
            clamp_reason = (
                f"window {query_start} to {query_end} exceeds Bybit's "
                f"{_BYBIT_MAX_EXECUTION_WINDOW.days} days; queried from "
                f"{clamped_start}, {query_start} to {clamped_start} unrecovered"
            )
            logger.error("Execution reconciliation for %s: %s", symbol, clamp_reason)
            query_start = clamped_start

        logger.info(
            f"Reconciling executions for {symbol} account {account_id} "
            f"(gap: {(gap_end - gap_start).total_seconds():.1f}s, "
            f"query {query_start} to {query_end})"
        )

        try:
            # Authenticated client per account (the shared one has no keys).
            authenticated_client = BybitRestClient(
                api_key=api_key,
                api_secret=api_secret,
                testnet=testnet,
            )
            executions_data, truncated = await asyncio.to_thread(
                authenticated_client.get_executions_all,
                symbol=symbol,
                start_time=int(query_start.timestamp() * 1000),
                end_time=int(query_end.timestamp() * 1000),
                max_pages=_PRIVATE_EXECUTION_RECONCILE_MAX_PAGES,
                return_truncated=True,
            )
        except Exception as e:
            logger.error(
                "Error reconciling executions for %s", symbol, exc_info=True
            )
            return ExecutionRecoveryResult(
                RecoveryStatus.FAILED, reason=f"REST error: {e}"
            )

        if truncated:
            logger.error(
                "Execution reconciliation for %s account %s was truncated "
                "after %d pages; refusing to persist a partial backfill for "
                "%s to %s",
                symbol,
                account_id,
                _PRIVATE_EXECUTION_RECONCILE_MAX_PAGES,
                query_start,
                query_end,
            )
            return ExecutionRecoveryResult(
                RecoveryStatus.TRUNCATED,
                reason=(
                    f"truncated after {_PRIVATE_EXECUTION_RECONCILE_MAX_PAGES} pages"
                ),
            )

        try:
            models = self._executions_to_models(
                user_id=user_id,
                account_id=account_id,
                run_id=run_id,
                executions=executions_data,
            )
            # Rows recovery owes the DB: every Trade row, plus any malformed
            # entry (non-dict, or no execType) not provably a non-Trade row.
            trade_rows = sum(
                1
                for e in executions_data
                if _is_trade_row(e)
                or not isinstance(e, dict)
                or "execType" not in e
            )
        except Exception as e:
            logger.error(
                "Error converting executions for %s", symbol, exc_info=True
            )
            return ExecutionRecoveryResult(
                RecoveryStatus.FAILED, reason=f"conversion error: {e}"
            )

        inserted = 0
        if models:
            try:
                with self._db.get_session() as session:
                    inserted = PrivateExecutionRepository(session).bulk_insert(
                        models
                    )
            except Exception as e:
                logger.error(
                    "Error persisting executions for %s", symbol, exc_info=True
                )
                return ExecutionRecoveryResult(
                    RecoveryStatus.FAILED, reason=f"DB error: {e}"
                )
        # bulk_insert dedupes the batch by exec_id, so count distinct ids.
        duplicates = len({m.exec_id for m in models}) - inserted
        if inserted > 0:
            self._executions_reconciled += inserted
            self._reconciliation_count += 1
            logger.info(
                f"Reconciled {inserted} executions for {symbol} "
                f"(skipped {duplicates} duplicates via unique constraint)"
            )

        reasons = [clamp_reason] if clamp_reason else []
        if len(models) < trade_rows:
            reason = (
                f"{trade_rows - len(models)} of {trade_rows} Trade rows failed "
                f"conversion and were not persisted"
            )
            logger.error("Execution reconciliation for %s: %s", symbol, reason)
            reasons.append(reason)
        if reasons:
            return ExecutionRecoveryResult(
                RecoveryStatus.FAILED, inserted, duplicates, "; ".join(reasons)
            )
        return ExecutionRecoveryResult(
            RecoveryStatus.RECOVERED, inserted, duplicates
        )

    def _trades_to_models(
        self,
        symbol: str,
        trades: list[dict],
    ) -> list[PublicTrade]:
        """Convert REST API trade data to ORM models.

        Args:
            symbol: Trading symbol.
            trades: List of trade dicts from REST API.

        Returns:
            List of PublicTrade models.
        """
        models = []
        for trade in trades:
            try:
                models.append(
                    PublicTrade(
                        symbol=symbol,
                        trade_id=trade.get("execId", ""),
                        exchange_ts=datetime.fromtimestamp(
                            int(trade.get("time", 0)) / 1000, tz=UTC
                        ),
                        local_ts=datetime.now(UTC),
                        side=trade.get("side", ""),
                        price=Decimal(str(trade.get("price", "0"))),
                        size=Decimal(str(trade.get("size", "0"))),
                    )
                )
            except Exception as e:
                logger.warning(f"Error converting trade to model: {e}")
                continue
        return models

    def _executions_to_models(
        self,
        user_id: UUID,
        account_id: UUID,
        run_id: Optional[UUID],
        executions: list[dict],
    ) -> list[PrivateExecution]:
        """Convert REST API execution data to ORM models.

        Args:
            user_id: User ID for tagging.
            account_id: Account ID for tagging.
            run_id: Optional run ID for tagging.
            executions: List of execution dicts from REST API.

        Returns:
            List of PrivateExecution models.
        """
        if run_id is None:
            # run_id is required by the model
            logger.warning("Cannot reconcile executions without run_id")
            return []

        models = []
        for exec_data in executions:
            try:
                # Category is validated on the REST envelope by
                # BybitRestClient.get_executions — rows carry none.
                if not _is_trade_row(exec_data):
                    continue

                models.append(
                    PrivateExecution(
                        run_id=str(run_id),
                        account_id=str(account_id),
                        exec_id=exec_data.get("execId", ""),
                        order_id=exec_data.get("orderId", ""),
                        order_link_id=exec_data.get("orderLinkId"),
                        symbol=exec_data.get("symbol", ""),
                        side=exec_data.get("side", ""),
                        exec_price=Decimal(str(exec_data.get("execPrice", "0"))),
                        exec_qty=Decimal(str(exec_data.get("execQty", "0"))),
                        exec_fee=Decimal(str(exec_data.get("execFee", "0"))),
                        closed_pnl=_rest_exec_pnl(exec_data),
                        exchange_ts=datetime.fromtimestamp(
                            int(exec_data.get("execTime", 0)) / 1000, tz=UTC
                        ),
                        raw_json=exec_data,
                    )
                )
            except Exception as e:
                logger.warning(f"Error converting execution to model: {e}")
                continue
        return models

    def get_stats(self) -> dict:
        """Get reconciler statistics.

        Returns:
            Dict with trades_reconciled, executions_reconciled, reconciliation_count.
        """
        return {
            "trades_reconciled": self._trades_reconciled,
            "executions_reconciled": self._executions_reconciled,
            "reconciliation_count": self._reconciliation_count,
        }
