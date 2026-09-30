"""Collect public market data (ticker + trades) for multiple symbols."""

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Callable, Optional

from bybit_adapter.ws_client import PublicWebSocketClient, ConnectionState
from bybit_adapter.normalizer import BybitNormalizer
from gridcore.events import TickerEvent, PublicTradeEvent

from event_saver.collectors._startup import (
    CollectorStartError,
    StartHandoff,
    disconnect_in_background,
    run_in_daemon_thread,
)


logger = logging.getLogger(__name__)

# Feature 0110 B1c-2: pybit's connect() blocks with no timeout of its own.
_PUBLIC_CONNECT_TIMEOUT = 10.0
_PUBLIC_DISCONNECT_TIMEOUT = 5.0


class PublicCollector:
    """Collects public market data for configured symbols.

    Subscribes to ticker and publicTrade streams via Bybit WebSocket.
    Normalizes incoming messages to gridcore events and forwards them
    to registered callbacks.

    Responsibilities:
    - Manage PublicWebSocketClient lifecycle
    - Normalize incoming messages to gridcore events
    - Buffer events for batch writing
    - Detect disconnections and trigger reconciliation

    Example:
        async def handle_trades(trades: list[PublicTradeEvent]):
            for trade in trades:
                print(f"{trade.symbol}: {trade.price} x {trade.size}")

        collector = PublicCollector(
            symbols=["BTCUSDT", "ETHUSDT"],
            on_trades=handle_trades,
            testnet=True,
        )
        await collector.start()
    """

    def __init__(
        self,
        symbols: list[str],
        on_ticker: Optional[Callable[[TickerEvent], None]] = None,
        on_trades: Optional[Callable[[list[PublicTradeEvent]], None]] = None,
        on_gap_detected: Optional[Callable[[str, datetime, datetime], None]] = None,
        testnet: bool = True,
    ):
        """Initialize public collector.

        Args:
            symbols: List of trading symbols to subscribe to.
            on_ticker: Callback for ticker events.
            on_trades: Callback for trade events (batched).
            on_gap_detected: Callback when gap is detected (symbol, start, end).
            testnet: Use testnet endpoints.
        """
        self.symbols = symbols
        self._on_ticker = on_ticker
        self._on_trades = on_trades
        self._on_gap_detected = on_gap_detected
        self._testnet = testnet

        self._normalizer = BybitNormalizer()
        self._ws_client: Optional[PublicWebSocketClient] = None
        self._last_trade_ts: dict[str, datetime] = {}
        self._running = False

    async def start(self) -> None:
        """Start collecting public data.

        Connects to WebSocket and subscribes to configured streams. The
        blocking connect runs off the event loop, bounded by
        ``_PUBLIC_CONNECT_TIMEOUT``.

        Raises:
            CollectorStartError: connect() did not return in time or raised;
                the collector is left not running. A cancelled start also
                leaves it not running (and re-raises the cancellation).
        """
        if self._running:
            logger.warning("PublicCollector already running")
            return

        logger.info(f"Starting PublicCollector for symbols: {self.symbols}")
        self._running = True

        self._ws_client = PublicWebSocketClient(
            symbols=self.symbols,
            testnet=self._testnet,
            on_ticker=self._handle_ticker if self._on_ticker else None,
            on_trade=self._handle_trade if self._on_trades else None,
            on_disconnect=self._handle_disconnect,
            on_reconnect=self._handle_reconnect,
        )

        client = self._ws_client
        handoff = StartHandoff()

        def _connect() -> None:
            try:
                client.connect()
            except BaseException:
                if not handoff.finish():
                    # The owner gave up, so nobody is waiting for this
                    # error: close what was opened before letting it go.
                    # (If it gives up AFTER this, abandon() tells it to.)
                    with contextlib.suppress(Exception):
                        client.disconnect()
                raise
            if not handoff.finish():
                # start() already gave up; close a connect that succeeded
                # late instead of leaving a live, unowned socket.
                client.disconnect()

        try:
            await asyncio.wait_for(
                run_in_daemon_thread(_connect, name="public-ws-connect"),
                timeout=_PUBLIC_CONNECT_TIMEOUT,
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            # The (daemon) worker is normally parked inside pybit holding the
            # client lock: abandon it without disconnect() (stop() must not
            # touch this client either); it disconnects by itself if
            # connect() ever returns. If it had already finished, the socket
            # is ours to close.
            if handoff.abandon():
                disconnect_in_background(client, name="public-ws-disconnect")
            self._ws_client = None
            self._running = False
            if isinstance(exc, asyncio.CancelledError):
                raise  # start() was cancelled (shutdown signal)
            raise CollectorStartError(
                f"Public WebSocket connect timed out after "
                f"{_PUBLIC_CONNECT_TIMEOUT:.1f}s"
            ) from None
        except Exception as exc:
            # connect() raised and has returned: close whatever it opened.
            # State first: a cancellation during the disconnect must still
            # leave the collector stopped.
            self._ws_client = None
            self._running = False
            try:
                await asyncio.wait_for(
                    run_in_daemon_thread(
                        client.disconnect, name="public-ws-disconnect"
                    ),
                    timeout=_PUBLIC_DISCONNECT_TIMEOUT,
                )
            except Exception:
                logger.warning(
                    "Public WS disconnect after a failed start did not complete"
                )
            raise CollectorStartError(
                f"Public WebSocket connect failed: {exc}"
            ) from exc
        logger.info("PublicCollector started")

    async def stop(self) -> None:
        """Stop collecting and disconnect.

        Gracefully disconnects WebSocket.
        """
        if not self._running:
            return

        logger.info("Stopping PublicCollector")
        self._running = False

        if self._ws_client:
            self._ws_client.disconnect()
            self._ws_client = None

        logger.info("PublicCollector stopped")

    def is_running(self) -> bool:
        """Check if collector is running."""
        return self._running

    def get_connection_state(self) -> Optional[ConnectionState]:
        """Get current WebSocket connection state."""
        if self._ws_client:
            return self._ws_client.get_connection_state()
        return None

    def _handle_ticker(self, message: dict) -> None:
        """Handle raw ticker message from WebSocket."""
        try:
            event = self._normalizer.normalize_ticker(message)
            if self._on_ticker:
                self._on_ticker(event)
        except Exception as e:
            logger.error(f"Error normalizing ticker: {e}")

    def _handle_trade(self, message: dict) -> None:
        """Handle raw trade message from WebSocket."""
        try:
            events = self._normalizer.normalize_public_trade(message)
            if events and self._on_trades:
                # Track last trade timestamp per symbol for gap detection
                for event in events:
                    self._last_trade_ts[event.symbol] = event.exchange_ts
                self._on_trades(events)
        except Exception as e:
            logger.error(f"Error normalizing trades: {e}")

    def _handle_disconnect(self, disconnect_ts: datetime) -> None:
        """Handle WebSocket disconnect event."""
        logger.warning(f"Public WebSocket disconnected at {disconnect_ts}")

    def _handle_reconnect(self, disconnected_at: datetime, reconnected_at: datetime) -> None:
        """Handle WebSocket reconnect event for gap detection."""
        gap_seconds = (reconnected_at - disconnected_at).total_seconds()
        logger.info(f"Public WebSocket reconnected after {gap_seconds:.1f}s gap")

        if self._on_gap_detected:
            # Trigger gap detection for each symbol
            for symbol in self.symbols:
                self._on_gap_detected(symbol, disconnected_at, reconnected_at)
