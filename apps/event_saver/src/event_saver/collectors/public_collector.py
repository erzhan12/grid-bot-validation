"""Collect public market data (ticker + trades) for multiple symbols."""

import logging
from datetime import datetime, UTC
from typing import Callable, Optional

from bybit_adapter.ws_client import PublicWebSocketClient, ConnectionState
from bybit_adapter.normalizer import BybitNormalizer
from gridcore.events import TickerEvent, PublicTradeEvent


logger = logging.getLogger(__name__)


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

        Connects to WebSocket and subscribes to configured streams.
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

        self._ws_client.connect()
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
