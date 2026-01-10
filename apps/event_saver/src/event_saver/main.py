"""Event saver main orchestrator.

Coordinates data collection from public and private WebSocket streams,
persistence to database, and gap reconciliation.
"""

import asyncio
import logging
import signal
from datetime import datetime
from typing import Optional
from uuid import UUID

from bybit_adapter.rest_client import BybitRestClient
from grid_db import DatabaseFactory, DatabaseSettings
from gridcore.events import PublicTradeEvent, ExecutionEvent, TickerEvent

from event_saver.config import EventSaverConfig
from event_saver.collectors import PublicCollector, PrivateCollector, AccountContext
from event_saver.writers import (
    TradeWriter,
    ExecutionWriter,
    TickerWriter,
    OrderWriter,
    PositionWriter,
    WalletWriter,
)
from event_saver.reconciler import GapReconciler


logger = logging.getLogger(__name__)


class EventSaver:
    """Main orchestrator for event data capture.

    Manages lifecycle of collectors, writers, and reconciler.
    Provides clean startup and shutdown handling.

    Example:
        config = EventSaverConfig()
        db = DatabaseFactory(config.database_url)

        saver = EventSaver(config=config, db=db)

        # Add accounts for private data collection
        await saver.add_account(AccountContext(...))

        await saver.start()
        # ... runs until shutdown signal
        await saver.stop()
    """

    def __init__(
        self,
        config: EventSaverConfig,
        db: DatabaseFactory,
    ):
        """Initialize event saver.

        Args:
            config: EventSaverConfig with settings.
            db: DatabaseFactory for database access.
        """
        self._config = config
        self._db = db

        # Components
        self._public_collector: Optional[PublicCollector] = None
        self._private_collectors: dict[UUID, PrivateCollector] = {}
        self._trade_writer: Optional[TradeWriter] = None
        self._ticker_writer: Optional[TickerWriter] = None
        self._execution_writer: Optional[ExecutionWriter] = None
        self._order_writer: Optional[OrderWriter] = None
        self._position_writer: Optional[PositionWriter] = None
        self._wallet_writer: Optional[WalletWriter] = None
        self._reconciler: Optional[GapReconciler] = None
        self._rest_client: Optional[BybitRestClient] = None

        # State
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None

    async def add_account(self, context: AccountContext) -> None:
        """Add an account for private data collection.

        If EventSaver is already running, the collector will be started immediately.
        Otherwise, it will be started when start() is called.

        Args:
            context: AccountContext with credentials and settings.
        """
        if context.account_id in self._private_collectors:
            logger.warning(f"Account {context.account_id} already added")
            return

        if context.run_id is None:
            logger.warning(
                f"Account {context.account_id} added without run_id. "
                f"Executions and orders will be logged but NOT persisted to database."
            )

        collector = PrivateCollector(
            context=context,
            on_execution=self._handle_execution,
            on_order=lambda event: self._handle_order(context.account_id, event),
            on_position=lambda msg: self._handle_position(context.account_id, msg),
            on_wallet=lambda msg: self._handle_wallet(context.account_id, msg),
            on_gap_detected=lambda start, end: self._handle_private_gap(
                context, start, end
            ),
        )
        self._private_collectors[context.account_id] = collector

        # If EventSaver is already running, start the collector immediately
        # Otherwise, it will be started when start() is called
        if self._running:
            await collector.start()
            logger.info(
                f"Added and started account {context.account_id} for private data collection"
            )
        else:
            logger.info(
                f"Added account {context.account_id} for private data collection "
                f"(will start when EventSaver.start() is called)"
            )

    def remove_account(self, account_id: UUID) -> None:
        """Remove an account from private data collection.

        Args:
            account_id: Account ID to remove.
        """
        if account_id in self._private_collectors:
            collector = self._private_collectors.pop(account_id)
            if collector.is_running() and self._event_loop:
                # Schedule stop from any thread (may be called from WebSocket thread)
                asyncio.run_coroutine_threadsafe(
                    collector.stop(),
                    self._event_loop
                )
            logger.info(f"Removed account {account_id} from private data collection")

    async def start(self) -> None:
        """Start all data collection components."""
        if self._running:
            logger.warning("EventSaver already running")
            return

        logger.info("Starting EventSaver...")
        self._running = True
        
        # Store event loop reference for thread-safe coroutine scheduling
        # Handlers are called from pybit's WebSocket thread, not the asyncio event loop
        self._event_loop = asyncio.get_running_loop()

        # Initialize REST client for reconciliation
        # Note: Public endpoints work with empty credentials
        # For private reconciliation, use per-account credentials
        self._rest_client = BybitRestClient(
            api_key="",
            api_secret="",
            testnet=self._config.testnet,
        )

        # Initialize reconciler
        self._reconciler = GapReconciler(
            db=self._db,
            rest_client=self._rest_client,
            gap_threshold_seconds=self._config.gap_threshold_seconds,
        )

        # Initialize writers
        self._trade_writer = TradeWriter(
            db=self._db,
            batch_size=self._config.batch_size,
            flush_interval=self._config.flush_interval,
        )
        await self._trade_writer.start_auto_flush()

        self._ticker_writer = TickerWriter(
            db=self._db,
            batch_size=self._config.batch_size,
            flush_interval=self._config.flush_interval,
        )
        await self._ticker_writer.start_auto_flush()

        self._execution_writer = ExecutionWriter(
            db=self._db,
            batch_size=self._config.batch_size,
            flush_interval=self._config.flush_interval,
        )
        await self._execution_writer.start_auto_flush()

        self._order_writer = OrderWriter(
            db=self._db,
            batch_size=self._config.batch_size,
            flush_interval=self._config.flush_interval,
        )
        await self._order_writer.start_auto_flush()

        self._position_writer = PositionWriter(
            db=self._db,
            batch_size=self._config.batch_size,
            flush_interval=self._config.flush_interval,
        )
        await self._position_writer.start_auto_flush()

        self._wallet_writer = WalletWriter(
            db=self._db,
            batch_size=self._config.batch_size,
            flush_interval=self._config.flush_interval,
        )
        await self._wallet_writer.start_auto_flush()

        # Start public collector if symbols configured
        symbols = self._config.get_symbols()
        if symbols:
            self._public_collector = PublicCollector(
                symbols=symbols,
                on_ticker=self._handle_ticker,
                on_trades=self._handle_trades,
                on_gap_detected=self._handle_public_gap,
                testnet=self._config.testnet,
            )
            await self._public_collector.start()

        # Start private collectors
        for collector in self._private_collectors.values():
            await collector.start()

        logger.info(
            f"EventSaver started. "
            f"Symbols: {symbols}, "
            f"Accounts: {len(self._private_collectors)}"
        )

    async def stop(self) -> None:
        """Stop all components gracefully."""
        if not self._running:
            return

        logger.info("Stopping EventSaver...")
        self._running = False

        # Stop collectors
        if self._public_collector:
            await self._public_collector.stop()

        for collector in self._private_collectors.values():
            await collector.stop()

        # Stop writers (flushes remaining buffers)
        if self._trade_writer:
            await self._trade_writer.stop()

        if self._ticker_writer:
            await self._ticker_writer.stop()

        if self._execution_writer:
            await self._execution_writer.stop()

        if self._order_writer:
            await self._order_writer.stop()

        if self._position_writer:
            await self._position_writer.stop()

        if self._wallet_writer:
            await self._wallet_writer.stop()

        logger.info("EventSaver stopped")

    async def run_until_shutdown(self) -> None:
        """Run until shutdown signal received.

        Sets up signal handlers for graceful shutdown.
        """
        # Set up signal handlers
        loop = asyncio.get_running_loop()

        def shutdown_handler():
            logger.info("Shutdown signal received")
            self._shutdown_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown_handler)

        # Wait for shutdown
        await self._shutdown_event.wait()

        # Clean shutdown
        await self.stop()

    def _handle_ticker(self, event: TickerEvent) -> None:
        """Handle incoming ticker event.

        Persists ticker snapshots to database via TickerWriter.

        Args:
            event: TickerEvent from collector.
        """
        if self._ticker_writer and self._event_loop:
            asyncio.run_coroutine_threadsafe(
                self._ticker_writer.write([event]),
                self._event_loop
            )
        logger.debug(f"Ticker: {event.symbol} price={event.last_price}")

    def _handle_trades(self, events: list[PublicTradeEvent]) -> None:
        """Handle incoming public trade events.

        Args:
            events: List of PublicTradeEvent from collector.
        """
        if self._trade_writer and events and self._event_loop:
            # Schedule async write from WebSocket thread to event loop
            asyncio.run_coroutine_threadsafe(
                self._trade_writer.write(events),
                self._event_loop
            )

    def _handle_execution(self, event: ExecutionEvent) -> None:
        """Handle incoming execution event.

        Args:
            event: ExecutionEvent from collector.
        """
        if self._execution_writer and self._event_loop:
            asyncio.run_coroutine_threadsafe(
                self._execution_writer.write([event]),
                self._event_loop
            )

    def _handle_order(self, account_id: UUID, event) -> None:
        """Handle incoming order update event.

        Persists order updates to database via OrderWriter.

        Args:
            account_id: Account ID for tagging.
            event: OrderUpdateEvent from collector.
        """
        if self._order_writer and self._event_loop:
            asyncio.run_coroutine_threadsafe(
                self._order_writer.write(account_id, [event]),
                self._event_loop
            )

        # Also log for visibility
        logger.debug(f"Order update: {event.order_id} {event.status}")

    def _handle_position(self, account_id: UUID, message: dict) -> None:
        """Handle incoming position snapshot.

        Persists position snapshots to database via PositionWriter.

        Args:
            account_id: Account ID for tagging.
            message: Raw position message from collector.
        """
        if self._position_writer and self._event_loop:
            asyncio.run_coroutine_threadsafe(
                self._position_writer.write(account_id, [message]),
                self._event_loop
            )

        # Also log for visibility
        data = message.get("data", [])
        for pos in data:
            logger.debug(
                f"Position: {pos.get('symbol')} {pos.get('side')} "
                f"size={pos.get('size')}"
            )

    def _handle_wallet(self, account_id: UUID, message: dict) -> None:
        """Handle incoming wallet snapshot.

        Persists wallet snapshots to database via WalletWriter.

        Args:
            account_id: Account ID for tagging.
            message: Raw wallet message from collector.
        """
        if self._wallet_writer and self._event_loop:
            asyncio.run_coroutine_threadsafe(
                self._wallet_writer.write(account_id, [message]),
                self._event_loop
            )

        # Also log for visibility
        data = message.get("data", [])
        for wallet in data:
            coins = wallet.get("coin", [])
            for coin in coins:
                logger.debug(
                    f"Wallet: {coin.get('coin')} "
                    f"balance={coin.get('walletBalance')}"
                )

    def _handle_public_gap(
        self,
        symbol: str,
        gap_start: datetime,
        gap_end: datetime,
    ) -> None:
        """Handle gap detected in public data stream.

        Args:
            symbol: Symbol with gap.
            gap_start: Start of gap (disconnect time).
            gap_end: End of gap (reconnect time).
        """
        if self._reconciler and self._event_loop:
            asyncio.run_coroutine_threadsafe(
                self._reconciler.reconcile_public_trades(
                    symbol=symbol,
                    gap_start=gap_start,
                    gap_end=gap_end,
                ),
                self._event_loop
            )

    def _handle_private_gap(
        self,
        context: AccountContext,
        gap_start: datetime,
        gap_end: datetime,
    ) -> None:
        """Handle gap detected in private data stream.

        Args:
            context: Account context for the stream.
            gap_start: Start of gap (disconnect time).
            gap_end: End of gap (reconnect time).
        """
        if self._reconciler and self._event_loop:
            # Reconcile for each symbol
            testnet = context.environment == "testnet"
            for symbol in context.symbols or self._config.get_symbols():
                asyncio.run_coroutine_threadsafe(
                    self._reconciler.reconcile_executions(
                        user_id=context.user_id,
                        account_id=context.account_id,
                        run_id=context.run_id,
                        symbol=symbol,
                        gap_start=gap_start,
                        gap_end=gap_end,
                        api_key=context.api_key,
                        api_secret=context.api_secret,
                        testnet=testnet,
                    ),
                    self._event_loop
                )

    def get_stats(self) -> dict:
        """Get statistics from all components.

        Returns:
            Dict with stats from writers and reconciler.
        """
        stats = {
            "running": self._running,
            "symbols": self._config.get_symbols(),
            "accounts": len(self._private_collectors),
        }

        if self._trade_writer:
            stats["trade_writer"] = self._trade_writer.get_stats()

        if self._ticker_writer:
            stats["ticker_writer"] = self._ticker_writer.get_stats()

        if self._execution_writer:
            stats["execution_writer"] = self._execution_writer.get_stats()

        if self._order_writer:
            stats["order_writer"] = self._order_writer.get_stats()

        if self._position_writer:
            stats["position_writer"] = self._position_writer.get_stats()

        if self._wallet_writer:
            stats["wallet_writer"] = self._wallet_writer.get_stats()

        if self._reconciler:
            stats["reconciler"] = self._reconciler.get_stats()

        return stats


async def main():
    """Entry point for event saver application."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Load configuration
    config = EventSaverConfig()

    logger.info(f"Event Saver Configuration:")
    logger.info(f"  Symbols: {config.get_symbols()}")
    logger.info(f"  Testnet: {config.testnet}")
    logger.info(f"  Batch size: {config.batch_size}")
    logger.info(f"  Flush interval: {config.flush_interval}s")
    logger.info(f"  Gap threshold: {config.gap_threshold_seconds}s")

    # Initialize database
    db = DatabaseFactory(DatabaseSettings(database_url=config.database_url))

    # Create and start event saver
    saver = EventSaver(config=config, db=db)

    # Note: Private accounts would be added here based on configuration
    # Example:
    # await saver.add_account(AccountContext(...))

    await saver.start()
    await saver.run_until_shutdown()


if __name__ == "__main__":
    asyncio.run(main())
