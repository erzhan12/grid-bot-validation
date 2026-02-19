"""Core data recorder orchestrator.

Coordinates WebSocket data collection and persistence for
standalone mainnet recording sessions.
"""

import asyncio
import logging
import signal
from datetime import datetime, UTC
from typing import Optional
from uuid import UUID, uuid4

from bybit_adapter.rest_client import BybitRestClient
from grid_db import (
    DatabaseFactory,
    User,
    BybitAccount,
    Strategy,
    Run,
)
from gridcore.events import PublicTradeEvent, ExecutionEvent, OrderUpdateEvent, TickerEvent

from event_saver.collectors import PublicCollector, PrivateCollector, AccountContext
from event_saver.writers import (
    TradeWriter,
    TickerWriter,
    ExecutionWriter,
    OrderWriter,
    PositionWriter,
    WalletWriter,
)
from event_saver.reconciler import GapReconciler

from recorder.config import RecorderConfig


logger = logging.getLogger(__name__)

# Fixed UUIDs for standalone recorder (stable across restarts)
_RECORDER_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
_RECORDER_ACCOUNT_ID = UUID("00000000-0000-0000-0000-000000000002")
_RECORDER_STRATEGY_ID = UUID("00000000-0000-0000-0000-000000000003")


class Recorder:
    """Standalone data recorder for Bybit mainnet capture.

    Simpler than EventSaver: no multi-tenant lifecycle, no run_id
    management. Single optional account, configured at startup.

    Example:
        config = load_config("recorder.yaml")
        db = DatabaseFactory(settings)
        recorder = Recorder(config=config, db=db)
        await recorder.start()
        await recorder.run_until_shutdown()
    """

    def __init__(self, config: RecorderConfig, db: DatabaseFactory):
        self._config = config
        self._db = db

        # Collectors
        self._public_collector: Optional[PublicCollector] = None
        self._private_collector: Optional[PrivateCollector] = None

        # Writers
        self._trade_writer: Optional[TradeWriter] = None
        self._ticker_writer: Optional[TickerWriter] = None
        self._execution_writer: Optional[ExecutionWriter] = None
        self._order_writer: Optional[OrderWriter] = None
        self._position_writer: Optional[PositionWriter] = None
        self._wallet_writer: Optional[WalletWriter] = None

        # Infrastructure
        self._reconciler: Optional[GapReconciler] = None
        self._health_task: Optional[asyncio.Task] = None

        # State
        self._running = False
        self._start_time: Optional[datetime] = None
        self._shutdown_event = asyncio.Event()
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._gap_count = 0
        self._run_id: Optional[UUID] = None

    async def start(self) -> None:
        """Start all recording components."""
        if self._running:
            logger.warning("Recorder already running")
            return

        logger.info("Starting Recorder...")
        self._start_time = datetime.now(UTC)
        self._event_loop = asyncio.get_running_loop()

        # Initialize REST client for reconciliation (public endpoints)
        rest_client = BybitRestClient(
            api_key="",
            api_secret="",
            testnet=self._config.testnet,
        )

        # Initialize reconciler
        self._reconciler = GapReconciler(
            db=self._db,
            rest_client=rest_client,
            gap_threshold_seconds=self._config.gap_threshold_seconds,
        )

        # Initialize writers
        writer_kwargs = {
            "db": self._db,
            "batch_size": self._config.batch_size,
            "flush_interval": self._config.flush_interval,
        }

        self._trade_writer = TradeWriter(**writer_kwargs)
        self._ticker_writer = TickerWriter(**writer_kwargs)
        await self._trade_writer.start_auto_flush()
        await self._ticker_writer.start_auto_flush()

        if self._config.account:
            # Seed DB parent records and create a Run for this session
            self._run_id = self._seed_db_records()

            self._execution_writer = ExecutionWriter(**writer_kwargs)
            self._order_writer = OrderWriter(**writer_kwargs)
            self._position_writer = PositionWriter(**writer_kwargs)
            self._wallet_writer = WalletWriter(**writer_kwargs)
            await self._execution_writer.start_auto_flush()
            await self._order_writer.start_auto_flush()
            await self._position_writer.start_auto_flush()
            await self._wallet_writer.start_auto_flush()

        # Start public collector
        if self._config.symbols:
            self._public_collector = PublicCollector(
                symbols=self._config.symbols,
                on_ticker=self._handle_ticker,
                on_trades=self._handle_trades,
                on_gap_detected=self._handle_public_gap,
                testnet=self._config.testnet,
            )
            await self._public_collector.start()

        # Start private collector (optional)
        if self._config.account:
            environment = "testnet" if self._config.testnet else "mainnet"
            context = AccountContext(
                account_id=_RECORDER_ACCOUNT_ID,
                user_id=_RECORDER_USER_ID,
                run_id=self._run_id,
                api_key=self._config.account.api_key.get_secret_value(),
                api_secret=self._config.account.api_secret.get_secret_value(),
                environment=environment,
                symbols=self._config.symbols,
            )
            self._private_collector = PrivateCollector(
                context=context,
                on_execution=self._handle_execution,
                on_order=lambda event: self._handle_order(
                    _RECORDER_ACCOUNT_ID, event
                ),
                on_position=lambda msg: self._handle_position(
                    _RECORDER_ACCOUNT_ID, msg
                ),
                on_wallet=lambda msg: self._handle_wallet(
                    _RECORDER_ACCOUNT_ID, msg
                ),
                on_gap_detected=self._handle_private_gap,
            )
            await self._private_collector.start()

        # Start health logging
        self._health_task = asyncio.create_task(self._health_log_loop())

        self._running = True
        logger.info(
            "Recorder started. "
            f"Symbols: {self._config.symbols}, "
            f"Testnet: {self._config.testnet}, "
            f"Private: {self._config.account is not None}"
        )

    async def stop(self, *, error: bool = False) -> None:
        """Stop all components gracefully.

        Args:
            error: If True, mark the DB run as 'error' instead of 'completed'.
        """
        if not self._running:
            return

        logger.info("Stopping Recorder...")
        self._running = False

        # Stop health logging
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None

        # Stop collectors
        if self._public_collector:
            await self._public_collector.stop()

        if self._private_collector:
            await self._private_collector.stop()

        # GapReconciler is stateless (no background tasks, no held connections).
        # Drop the reference so no new reconciliation futures are scheduled
        # after stop; any in-flight REST futures will complete on their own.
        self._reconciler = None

        # Stop writers (flushes remaining buffers)
        for writer in [
            self._trade_writer,
            self._ticker_writer,
            self._execution_writer,
            self._order_writer,
            self._position_writer,
            self._wallet_writer,
        ]:
            if writer:
                await writer.stop()

        # Mark run status in DB
        status = "error" if error else "completed"
        self._mark_run_status(status)

        # Log final stats
        stats = self.get_stats()
        logger.info(f"Recorder stopped. Final stats: {stats}")

    async def run_until_shutdown(self) -> None:
        """Run until SIGINT/SIGTERM received."""
        loop = asyncio.get_running_loop()

        def shutdown_handler():
            logger.info("Shutdown signal received")
            self._shutdown_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown_handler)

        try:
            await self._shutdown_event.wait()
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
        await self.stop()

    def _seed_db_records(self) -> UUID:
        """Create parent DB records for private stream persistence.

        Ensures User, BybitAccount, Strategy, and Run rows exist so that
        execution_writer and order_writer can store data with a valid run_id.
        Uses fixed UUIDs so rows are reused across recorder restarts.

        Returns:
            The run_id UUID for this recording session.
        """
        run_id = uuid4()
        environment = "testnet" if self._config.testnet else "mainnet"
        # Store first symbol in Strategy.symbol (VARCHAR(20) limit),
        # full list goes in config_json for reference.
        primary_symbol = self._config.symbols[0] if self._config.symbols else "UNKNOWN"

        try:
            with self._db.get_session() as session:
                # Upsert User (merge = insert or update)
                session.merge(User(
                    user_id=str(_RECORDER_USER_ID),
                    username="recorder",
                ))
                # Upsert BybitAccount
                session.merge(BybitAccount(
                    account_id=str(_RECORDER_ACCOUNT_ID),
                    user_id=str(_RECORDER_USER_ID),
                    account_name="recorder",
                    environment=environment,
                ))
                # Upsert Strategy
                session.merge(Strategy(
                    strategy_id=str(_RECORDER_STRATEGY_ID),
                    account_id=str(_RECORDER_ACCOUNT_ID),
                    strategy_type="recorder",
                    symbol=primary_symbol,
                    config_json={
                        "mode": "recorder",
                        "symbols": self._config.symbols,
                    },
                ))
                # Create new Run for this session
                session.add(Run(
                    run_id=str(run_id),
                    user_id=str(_RECORDER_USER_ID),
                    account_id=str(_RECORDER_ACCOUNT_ID),
                    strategy_id=str(_RECORDER_STRATEGY_ID),
                    run_type="recording",
                    status="running",
                ))
        except Exception as e:
            raise RuntimeError(f"Failed to initialize recording session: {e}") from e

        logger.info(f"Created recording run: {run_id}")
        return run_id

    def _mark_run_status(self, status: str) -> None:
        """Mark the current Run's status in the database.

        Args:
            status: Run status to set (e.g. 'completed', 'error').
        """
        if not self._run_id:
            return
        try:
            with self._db.get_session() as session:
                run = session.get(Run, str(self._run_id))
                if run:
                    run.status = status
                    run.end_ts = datetime.now(UTC)
                    logger.info(f"Marked run {self._run_id} as {status}")
                else:
                    logger.warning(f"Run {self._run_id} not found in database")
        except Exception as e:
            logger.error(f"Failed to mark run {self._run_id} as {status}: {e}")

    @staticmethod
    def _log_future_error(label: str):
        """Return a done-callback that logs exceptions from fire-and-forget futures."""
        def _cb(future):
            if (exc := future.exception()) is not None:
                logger.error("%s failed: %s", label, exc)
        return _cb

    def _handle_ticker(self, event: TickerEvent) -> None:
        """Route ticker event to writer."""
        if self._ticker_writer and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._ticker_writer.write([event]),
                self._event_loop,
            )
            fut.add_done_callback(self._log_future_error("ticker write"))

    def _handle_trades(self, events: list[PublicTradeEvent]) -> None:
        """Route trade events to writer."""
        if self._trade_writer and events and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._trade_writer.write(events),
                self._event_loop,
            )
            fut.add_done_callback(self._log_future_error("trade write"))

    def _handle_execution(self, event: ExecutionEvent) -> None:
        """Route execution event to writer."""
        if self._execution_writer and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._execution_writer.write([event]),
                self._event_loop,
            )
            fut.add_done_callback(self._log_future_error("execution write"))

    def _handle_order(self, account_id: UUID, event: OrderUpdateEvent) -> None:
        """Route order event to writer."""
        if self._order_writer and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._order_writer.write(account_id, [event]),
                self._event_loop,
            )
            fut.add_done_callback(self._log_future_error("order write"))

    def _handle_position(self, account_id: UUID, message: dict) -> None:
        """Route position snapshot to writer."""
        if self._position_writer and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._position_writer.write(account_id, [message]),
                self._event_loop,
            )
            fut.add_done_callback(self._log_future_error("position write"))

    def _handle_wallet(self, account_id: UUID, message: dict) -> None:
        """Route wallet snapshot to writer."""
        if self._wallet_writer and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._wallet_writer.write(account_id, [message]),
                self._event_loop,
            )
            fut.add_done_callback(self._log_future_error("wallet write"))

    def _handle_public_gap(
        self, symbol: str, gap_start: datetime, gap_end: datetime
    ) -> None:
        """Trigger REST reconciliation for public data gap."""
        self._gap_count += 1
        if self._reconciler and self._event_loop:
            fut = asyncio.run_coroutine_threadsafe(
                self._reconciler.reconcile_public_trades(
                    symbol=symbol,
                    gap_start=gap_start,
                    gap_end=gap_end,
                ),
                self._event_loop,
            )
            fut.add_done_callback(
                self._log_future_error(f"public reconciliation ({symbol})")
            )

    def _handle_private_gap(
        self, gap_start: datetime, gap_end: datetime
    ) -> None:
        """Reconcile private stream gap via REST API."""
        self._gap_count += 1
        gap_seconds = (gap_end - gap_start).total_seconds()
        logger.warning(
            f"Private stream gap detected: {gap_seconds:.1f}s "
            f"({gap_start} to {gap_end})"
        )

        if (
            self._reconciler
            and self._event_loop
            and self._config.account
            and self._run_id
        ):
            for symbol in self._config.symbols:
                fut = asyncio.run_coroutine_threadsafe(
                    self._reconciler.reconcile_executions(
                        user_id=_RECORDER_USER_ID,
                        account_id=_RECORDER_ACCOUNT_ID,
                        run_id=self._run_id,
                        symbol=symbol,
                        gap_start=gap_start,
                        gap_end=gap_end,
                        api_key=self._config.account.api_key.get_secret_value(),
                        api_secret=self._config.account.api_secret.get_secret_value(),
                        testnet=self._config.testnet,
                    ),
                    self._event_loop,
                )
                fut.add_done_callback(
                    self._log_future_error(f"private reconciliation ({symbol})")
                )

    async def _health_log_loop(self) -> None:
        """Periodically log health stats."""
        while self._running:
            try:
                await asyncio.sleep(self._config.health_log_interval)
                if not self._running:
                    break

                stats = self.get_stats()
                logger.info(f"Health: {stats}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health log loop: {e}")

    def get_stats(self) -> dict:
        """Get recorder statistics."""
        uptime = 0.0
        if self._start_time:
            uptime = (datetime.now(UTC) - self._start_time).total_seconds()

        stats = {
            "uptime_seconds": round(uptime, 1),
            "gaps_detected": self._gap_count,
        }

        # Public WS connection state
        if self._public_collector:
            conn_state = self._public_collector.get_connection_state()
            if conn_state:
                stats["public_ws"] = {
                    "connected": conn_state.is_connected,
                    "reconnect_count": conn_state.reconnect_count,
                }

        # Writer stats with message rates
        for name, writer in [
            ("trades", self._trade_writer),
            ("tickers", self._ticker_writer),
            ("executions", self._execution_writer),
            ("orders", self._order_writer),
            ("positions", self._position_writer),
            ("wallets", self._wallet_writer),
        ]:
            if writer:
                writer_stats = writer.get_stats()
                if uptime > 0:
                    writer_stats["msgs_per_sec"] = round(
                        writer_stats["total_written"] / uptime, 2
                    )
                stats[name] = writer_stats

        # Reconciler stats
        if self._reconciler:
            stats["reconciler"] = self._reconciler.get_stats()

        return stats
