"""Managed WebSocket connections with reconnection handling.

This module provides WebSocket client wrappers for Bybit's public and private
streams. It tracks connection state for gap detection and provides callbacks
for reconnection events.

Reference:
- pybit WebSocket: https://github.com/bybit-exchange/pybit
- Bybit WebSocket Connect: https://bybit-exchange.github.io/docs/v5/ws/connect
"""

from dataclasses import dataclass, field
from datetime import datetime, UTC
from typing import Callable, Optional
import logging
import threading
import time

from pybit.unified_trading import WebSocket


logger = logging.getLogger(__name__)


# Bybit channel types
CHANNEL_TYPE_LINEAR = "linear"
CHANNEL_TYPE_PRIVATE = "private"

# Heartbeat watchdog constants
DEFAULT_HEARTBEAT_INTERVAL = 5.0  # Check every 5 seconds
DEFAULT_DISCONNECT_THRESHOLD = 30.0  # Consider disconnected after 30s of no messages


@dataclass
class ConnectionState:
    """Track WebSocket connection state for gap detection.

    Attributes:
        connected_at: Timestamp when connection was established
        disconnected_at: Timestamp when last disconnection occurred
        last_message_ts: Timestamp of last received message
        reconnect_count: Number of reconnections since initial connect
        is_connected: Whether currently connected
        _detected_disconnect: Internal flag for disconnect detection
    """

    connected_at: Optional[datetime] = None
    disconnected_at: Optional[datetime] = None
    last_message_ts: Optional[datetime] = None
    reconnect_count: int = 0
    is_connected: bool = False
    _detected_disconnect: bool = field(default=False, init=False)


@dataclass
class PublicWebSocketClient:
    """Manages public WebSocket connection for multiple symbols.

    Subscribes to ticker and publicTrade streams for configured symbols.
    Tracks connection state and provides callbacks for gap detection.

    Responsibilities:
    - Subscribe to ticker and publicTrade streams
    - Handle reconnection with state tracking
    - Emit connection state events for gap detection
    - Support multi-symbol subscription

    Example:
        def on_ticker(msg):
            print(f"Ticker: {msg}")

        def on_trade(msg):
            print(f"Trade: {msg}")

        client = PublicWebSocketClient(
            symbols=["BTCUSDT", "ETHUSDT"],
            testnet=True,
            on_ticker=on_ticker,
            on_trade=on_trade,
        )
        client.connect()
        # ... later
        client.disconnect()
    """

    symbols: list[str]
    testnet: bool = True
    on_ticker: Optional[Callable[[dict], None]] = None
    on_trade: Optional[Callable[[dict], None]] = None
    on_disconnect: Optional[Callable[[datetime], None]] = None
    on_reconnect: Optional[Callable[[datetime, datetime], None]] = None
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL
    disconnect_threshold: float = DEFAULT_DISCONNECT_THRESHOLD

    _ws: Optional[WebSocket] = field(default=None, init=False, repr=False)
    _state: ConnectionState = field(default_factory=ConnectionState, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _heartbeat_thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _stop_heartbeat: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def connect(self) -> None:
        """Establish WebSocket connection and subscribe to streams.

        Subscribes to ticker and publicTrade streams for all configured symbols.
        """
        with self._lock:
            if self._ws is not None:
                logger.warning("PublicWebSocketClient already connected, disconnecting first")
                self._disconnect_internal()

            logger.info(f"Connecting public WebSocket (testnet={self.testnet}) for symbols: {self.symbols}")

            self._ws = WebSocket(
                testnet=self.testnet,
                channel_type=CHANNEL_TYPE_LINEAR,
            )

            # Subscribe to streams for each symbol
            for symbol in self.symbols:
                if self.on_ticker:
                    self._ws.ticker_stream(
                        symbol=symbol,
                        callback=self._handle_ticker,
                    )
                    logger.debug(f"Subscribed to ticker stream for {symbol}")

                if self.on_trade:
                    self._ws.trade_stream(
                        symbol=symbol,
                        callback=self._handle_trade,
                    )
                    logger.debug(f"Subscribed to publicTrade stream for {symbol}")

            self._state.connected_at = datetime.now(UTC)
            self._state.is_connected = True
            self._state.last_message_ts = datetime.now(UTC)
            self._state._detected_disconnect = False
            logger.info("Public WebSocket connected")

            # Start heartbeat watchdog
            self._start_heartbeat_watchdog()

    def disconnect(self) -> None:
        """Gracefully disconnect WebSocket."""
        # Stop heartbeat watchdog first (outside lock to avoid deadlock during join)
        self._stop_heartbeat_watchdog()

        with self._lock:
            self._disconnect_internal()

    def _disconnect_internal(self) -> None:
        """Internal disconnect without lock (must be called with lock held)."""
        if self._ws is not None:
            try:
                self._ws.exit()
            except Exception as e:
                logger.warning(f"Error during WebSocket disconnect: {e}")
            self._ws = None
            self._state.is_connected = False
            self._state.disconnected_at = datetime.now(UTC)
            logger.info("Public WebSocket disconnected")

    def get_connection_state(self) -> ConnectionState:
        """Return current connection state for gap detection.

        Returns:
            Copy of current ConnectionState
        """
        with self._lock:
            return ConnectionState(
                connected_at=self._state.connected_at,
                disconnected_at=self._state.disconnected_at,
                last_message_ts=self._state.last_message_ts,
                reconnect_count=self._state.reconnect_count,
                is_connected=self._state.is_connected,
            )

    def is_connected(self) -> bool:
        """Check if WebSocket is currently connected."""
        with self._lock:
            return self._state.is_connected and self._ws is not None

    def _handle_ticker(self, message: dict) -> None:
        """Handle raw ticker message from WebSocket."""
        self._update_last_message_ts()
        if self.on_ticker:
            try:
                self.on_ticker(message)
            except Exception as e:
                logger.error(f"Error in ticker callback: {e}")

    def _handle_trade(self, message: dict) -> None:
        """Handle raw trade message from WebSocket."""
        self._update_last_message_ts()
        if self.on_trade:
            try:
                self.on_trade(message)
            except Exception as e:
                logger.error(f"Error in trade callback: {e}")

    def _start_heartbeat_watchdog(self) -> None:
        """Start background thread to detect disconnection via message gap."""
        self._stop_heartbeat.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="PublicWS-Heartbeat"
        )
        self._heartbeat_thread.start()
        logger.debug("Heartbeat watchdog started")

    def _stop_heartbeat_watchdog(self) -> None:
        """Stop heartbeat watchdog thread."""
        self._stop_heartbeat.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2.0)
        self._heartbeat_thread = None
        logger.debug("Heartbeat watchdog stopped")

    def _heartbeat_loop(self) -> None:
        """Background loop to detect disconnection via message gap."""
        while not self._stop_heartbeat.is_set():
            self._stop_heartbeat.wait(self.heartbeat_interval)
            if self._stop_heartbeat.is_set():
                break

            # Initialize outside lock to avoid UnboundLocalError
            disconnect_ts = None
            should_fire_disconnect = False

            with self._lock:
                if not self._state.is_connected or self._state.last_message_ts is None:
                    continue

                gap = (datetime.now(UTC) - self._state.last_message_ts).total_seconds()

                if gap > self.disconnect_threshold and not self._state._detected_disconnect:
                    # Detected disconnection via message gap
                    self._state._detected_disconnect = True
                    self._state.disconnected_at = self._state.last_message_ts
                    logger.warning(
                        f"Detected disconnection: no messages for {gap:.1f}s "
                        f"(threshold: {self.disconnect_threshold}s)"
                    )

                    # Capture state for callback (release lock first to prevent deadlock)
                    disconnect_ts = self._state.disconnected_at
                    should_fire_disconnect = True

            # Fire callback outside lock
            if should_fire_disconnect and self.on_disconnect and disconnect_ts:
                try:
                    self.on_disconnect(disconnect_ts)
                except Exception as e:
                    logger.error(f"Error in disconnect callback: {e}")

    def _update_last_message_ts(self) -> None:
        """Update last message timestamp and detect reconnection."""
        now = datetime.now(UTC)

        with self._lock:
            was_detected_disconnect = self._state._detected_disconnect
            disconnected_at = self._state.disconnected_at

            self._state.last_message_ts = now

            if was_detected_disconnect:
                # Message received after detected disconnect = reconnection
                self._state._detected_disconnect = False
                self._state.reconnect_count += 1
                logger.info(
                    f"Detected reconnection (reconnect #{self._state.reconnect_count})"
                )

        # Fire reconnect callback outside lock
        if was_detected_disconnect and self.on_reconnect and disconnected_at:
            try:
                self.on_reconnect(disconnected_at, now)
            except Exception as e:
                logger.error(f"Error in reconnect callback: {e}")


@dataclass
class PrivateWebSocketClient:
    """Manages private WebSocket connection for a single account.

    Subscribes to execution, order, position, and wallet streams.
    Requires API credentials for authentication.

    Responsibilities:
    - Authenticate with API credentials
    - Subscribe to execution, order, position, wallet streams
    - Filter messages by category (linear only)
    - Track connection state per account

    Example:
        def on_execution(msg):
            print(f"Execution: {msg}")

        client = PrivateWebSocketClient(
            api_key="xxx",
            api_secret="yyy",
            testnet=True,
            on_execution=on_execution,
        )
        client.connect()
        # ... later
        client.disconnect()
    """

    api_key: str
    api_secret: str
    testnet: bool = True
    on_execution: Optional[Callable[[dict], None]] = None
    on_order: Optional[Callable[[dict], None]] = None
    on_position: Optional[Callable[[dict], None]] = None
    on_wallet: Optional[Callable[[dict], None]] = None
    on_disconnect: Optional[Callable[[datetime], None]] = None
    on_reconnect: Optional[Callable[[datetime, datetime], None]] = None
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL
    disconnect_threshold: float = DEFAULT_DISCONNECT_THRESHOLD

    _ws: Optional[WebSocket] = field(default=None, init=False, repr=False)
    _state: ConnectionState = field(default_factory=ConnectionState, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _heartbeat_thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _stop_heartbeat: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def connect(self) -> None:
        """Establish authenticated WebSocket connection and subscribe to streams.

        Subscribes to execution, order, position, and wallet streams based on
        which callbacks are provided.
        """
        with self._lock:
            if self._ws is not None:
                logger.warning("PrivateWebSocketClient already connected, disconnecting first")
                self._disconnect_internal()

            logger.info(f"Connecting private WebSocket (testnet={self.testnet})")

            self._ws = WebSocket(
                testnet=self.testnet,
                channel_type=CHANNEL_TYPE_PRIVATE,
                api_key=self.api_key,
                api_secret=self.api_secret,
                trace_logging=False,
            )

            # Subscribe to streams based on provided callbacks
            if self.on_execution:
                self._ws.execution_stream(callback=self._handle_execution)
                logger.debug("Subscribed to execution stream")

            if self.on_order:
                self._ws.order_stream(callback=self._handle_order)
                logger.debug("Subscribed to order stream")

            if self.on_position:
                self._ws.position_stream(callback=self._handle_position)
                logger.debug("Subscribed to position stream")

            if self.on_wallet:
                self._ws.wallet_stream(callback=self._handle_wallet)
                logger.debug("Subscribed to wallet stream")

            self._state.connected_at = datetime.now(UTC)
            self._state.is_connected = True
            self._state.last_message_ts = datetime.now(UTC)
            self._state._detected_disconnect = False
            logger.info("Private WebSocket connected")

            # Start heartbeat watchdog
            self._start_heartbeat_watchdog()

    def disconnect(self) -> None:
        """Gracefully disconnect WebSocket."""
        # Stop heartbeat watchdog first (outside lock to avoid deadlock during join)
        self._stop_heartbeat_watchdog()

        with self._lock:
            self._disconnect_internal()

    def _disconnect_internal(self) -> None:
        """Internal disconnect without lock (must be called with lock held)."""
        if self._ws is not None:
            try:
                self._ws.exit()
            except Exception as e:
                logger.warning(f"Error during WebSocket disconnect: {e}")
            self._ws = None
            self._state.is_connected = False
            self._state.disconnected_at = datetime.now(UTC)
            logger.info("Private WebSocket disconnected")

    def get_connection_state(self) -> ConnectionState:
        """Return current connection state for gap detection.

        Returns:
            Copy of current ConnectionState
        """
        with self._lock:
            return ConnectionState(
                connected_at=self._state.connected_at,
                disconnected_at=self._state.disconnected_at,
                last_message_ts=self._state.last_message_ts,
                reconnect_count=self._state.reconnect_count,
                is_connected=self._state.is_connected,
            )

    def is_connected(self) -> bool:
        """Check if WebSocket is currently connected."""
        with self._lock:
            return self._state.is_connected and self._ws is not None

    def _handle_execution(self, message: dict) -> None:
        """Handle raw execution message from WebSocket."""
        self._update_last_message_ts()
        if self.on_execution:
            try:
                self.on_execution(message)
            except Exception as e:
                logger.error(f"Error in execution callback: {e}")

    def _handle_order(self, message: dict) -> None:
        """Handle raw order message from WebSocket."""
        self._update_last_message_ts()
        if self.on_order:
            try:
                self.on_order(message)
            except Exception as e:
                logger.error(f"Error in order callback: {e}")

    def _handle_position(self, message: dict) -> None:
        """Handle raw position message from WebSocket."""
        self._update_last_message_ts()
        if self.on_position:
            try:
                self.on_position(message)
            except Exception as e:
                logger.error(f"Error in position callback: {e}")

    def _handle_wallet(self, message: dict) -> None:
        """Handle raw wallet message from WebSocket."""
        self._update_last_message_ts()
        if self.on_wallet:
            try:
                self.on_wallet(message)
            except Exception as e:
                logger.error(f"Error in wallet callback: {e}")

    def _start_heartbeat_watchdog(self) -> None:
        """Start background thread to detect disconnection via message gap."""
        self._stop_heartbeat.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="PrivateWS-Heartbeat"
        )
        self._heartbeat_thread.start()
        logger.debug("Heartbeat watchdog started")

    def _stop_heartbeat_watchdog(self) -> None:
        """Stop heartbeat watchdog thread."""
        self._stop_heartbeat.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2.0)
        self._heartbeat_thread = None
        logger.debug("Heartbeat watchdog stopped")

    def _heartbeat_loop(self) -> None:
        """Background loop to detect disconnection via message gap."""
        while not self._stop_heartbeat.is_set():
            self._stop_heartbeat.wait(self.heartbeat_interval)
            if self._stop_heartbeat.is_set():
                break

            # Initialize outside lock to avoid UnboundLocalError
            disconnect_ts = None
            should_fire_disconnect = False

            with self._lock:
                if not self._state.is_connected or self._state.last_message_ts is None:
                    continue

                gap = (datetime.now(UTC) - self._state.last_message_ts).total_seconds()

                if gap > self.disconnect_threshold and not self._state._detected_disconnect:
                    # Detected disconnection via message gap
                    self._state._detected_disconnect = True
                    self._state.disconnected_at = self._state.last_message_ts
                    logger.warning(
                        f"Detected disconnection: no messages for {gap:.1f}s "
                        f"(threshold: {self.disconnect_threshold}s)"
                    )

                    # Capture state for callback (release lock first to prevent deadlock)
                    disconnect_ts = self._state.disconnected_at
                    should_fire_disconnect = True

            # Fire callback outside lock
            if should_fire_disconnect and self.on_disconnect and disconnect_ts:
                try:
                    self.on_disconnect(disconnect_ts)
                except Exception as e:
                    logger.error(f"Error in disconnect callback: {e}")

    def _update_last_message_ts(self) -> None:
        """Update last message timestamp and detect reconnection."""
        now = datetime.now(UTC)

        with self._lock:
            was_detected_disconnect = self._state._detected_disconnect
            disconnected_at = self._state.disconnected_at

            self._state.last_message_ts = now

            if was_detected_disconnect:
                # Message received after detected disconnect = reconnection
                self._state._detected_disconnect = False
                self._state.reconnect_count += 1
                logger.info(
                    f"Detected reconnection (reconnect #{self._state.reconnect_count})"
                )

        # Fire reconnect callback outside lock
        if was_detected_disconnect and self.on_reconnect and disconnected_at:
            try:
                self.on_reconnect(disconnected_at, now)
            except Exception as e:
                logger.error(f"Error in reconnect callback: {e}")
