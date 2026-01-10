"""Gap detection and REST reconciliation for missed WebSocket data."""

import asyncio
import logging
from datetime import datetime, UTC, timedelta
from decimal import Decimal
from typing import Optional
from uuid import UUID

from bybit_adapter.rest_client import BybitRestClient
from grid_db import (
    DatabaseFactory,
    PublicTrade,
    PublicTradeRepository,
    PrivateExecution,
    PrivateExecutionRepository,
)


logger = logging.getLogger(__name__)


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

            # Use last persisted timestamp if available, otherwise use gap_start
            reconcile_start = last_persisted_ts if last_persisted_ts else gap_start

            # Add buffer to ensure we capture all trades
            start_ms = int((reconcile_start - timedelta(seconds=1)).timestamp() * 1000)
            end_ms = int((gap_end + timedelta(seconds=1)).timestamp() * 1000)

            logger.debug(
                f"Reconciliation window: {reconcile_start} to {gap_end} "
                f"(last_persisted_ts: {last_persisted_ts})"
            )

            # Run synchronous REST call in thread to avoid blocking event loop
            trades_data = await asyncio.to_thread(
                self._rest_client.get_recent_trades,
                symbol=symbol,
                limit=1000,  # Max limit
            )

            if not trades_data:
                logger.debug(f"No trades returned from REST API for {symbol}")
                return 0

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
    ) -> int:
        """Reconcile private executions during a gap period.

        Queries REST API for executions in the gap period and inserts
        any that are not already in the database.

        Args:
            user_id: User ID for tagging.
            account_id: Account ID for tagging.
            run_id: Optional run ID for tagging.
            symbol: Trading symbol.
            gap_start: Start of gap period.
            gap_end: End of gap period.
            api_key: API key for authenticated request.
            api_secret: API secret for authenticated request.
            testnet: Use testnet endpoints (from account environment).

        Returns:
            Number of executions reconciled.
        """
        if not self.should_reconcile(gap_start, gap_end):
            logger.debug(f"Gap too small for execution reconciliation: {symbol}")
            return 0

        gap_seconds = (gap_end - gap_start).total_seconds()
        logger.info(
            f"Reconciling executions for {symbol} account {account_id} "
            f"(gap: {gap_seconds:.1f}s)"
        )

        try:
            # Query DB for last persisted timestamp for this account
            with self._db.get_session() as session:
                repo = PrivateExecutionRepository(session)
                last_persisted_ts = repo.get_last_execution_ts(str(account_id))

            # Use last persisted timestamp if available, otherwise use gap_start
            reconcile_start = last_persisted_ts if last_persisted_ts else gap_start

            logger.debug(
                f"Reconciliation window: {reconcile_start} to {gap_end} "
                f"(last_persisted_ts: {last_persisted_ts})"
            )

            # Create authenticated REST client for this account
            # (cannot use shared client with empty credentials)
            from bybit_adapter.rest_client import BybitRestClient
            authenticated_client = BybitRestClient(
                api_key=api_key,
                api_secret=api_secret,
                testnet=testnet,
            )

            # Get executions from REST API (with pagination)
            start_ms = int(reconcile_start.timestamp() * 1000)
            end_ms = int(gap_end.timestamp() * 1000)

            # Run synchronous REST call in thread to avoid blocking event loop
            # Use get_executions_all to handle pagination automatically
            executions_data = await asyncio.to_thread(
                authenticated_client.get_executions_all,
                symbol=symbol,
                start_time=start_ms,
                end_time=end_ms,
                max_pages=10,  # Safety limit
            )

            if not executions_data:
                logger.debug(f"No executions returned from REST API for {symbol}")
                return 0

            # Convert to models
            models = self._executions_to_models(
                user_id=user_id,
                account_id=account_id,
                run_id=run_id,
                executions=executions_data,
            )

            if not models:
                return 0

            # Bulk insert (database handles duplicates with unique constraint)
            with self._db.get_session() as session:
                repo = PrivateExecutionRepository(session)
                count = repo.bulk_insert(models)

                if count > 0:
                    self._executions_reconciled += count
                    self._reconciliation_count += 1
                    skipped = len(models) - count
                    logger.info(
                        f"Reconciled {count} executions for {symbol} "
                        f"(skipped {skipped} duplicates via unique constraint)"
                    )

            return count

        except Exception as e:
            logger.error(f"Error reconciling executions for {symbol}: {e}")
            return 0

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
                # Filter: only linear perpetuals and trade executions
                if exec_data.get("category") != "linear":
                    continue
                if exec_data.get("execType") != "Trade":
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
                        closed_pnl=Decimal(str(exec_data.get("closedPnl", "0"))),
                        exchange_ts=datetime.fromtimestamp(
                            int(exec_data.get("execTime", 0)) / 1000, tz=UTC
                        ),
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
