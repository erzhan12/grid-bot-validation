"""Collect private account data (executions, orders, positions, wallet)."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional
from uuid import UUID

from bybit_adapter.ws_client import PrivateWebSocketClient, ConnectionState
from bybit_adapter.normalizer import BybitNormalizer, NormalizerContext
from gridcore.events import ExecutionEvent, OrderUpdateEvent


logger = logging.getLogger(__name__)


@dataclass
class AccountContext:
    """Context for a single account's private streams.

    Contains all information needed to connect to and identify
    private streams for an account.

    IMPORTANT: run_id is REQUIRED for persistence. If None:
    - Executions and orders will be logged but NOT saved to database
    - Position and wallet snapshots will still be saved

    Use PrivateCollector.update_run_id() to set run_id when a run starts.
    """

    account_id: UUID
    user_id: UUID
    run_id: Optional[UUID]  # Required for execution/order persistence
    api_key: str
    api_secret: str
    environment: str  # 'mainnet' or 'testnet'
    symbols: list[str]  # Symbols to filter (empty = all symbols)


class PrivateCollector:
    """Collects private account data for a single account.

    Subscribes to execution, order, position, and wallet streams
    via authenticated Bybit WebSocket. Tags all events with
    multi-tenant identifiers.

    Responsibilities:
    - Manage PrivateWebSocketClient lifecycle
    - Filter messages by symbol (if configured)
    - Normalize and tag events with multi-tenant IDs
    - Track connection state for reconciliation

    Example:
        context = AccountContext(
            account_id=uuid4(),
            user_id=uuid4(),
            run_id=uuid4(),
            api_key="xxx",
            api_secret="yyy",
            environment="testnet",
            symbols=["BTCUSDT"],
        )

        async def handle_execution(event: ExecutionEvent):
            print(f"Execution: {event.exec_id} {event.price}x{event.qty}")

        collector = PrivateCollector(
            context=context,
            on_execution=handle_execution,
        )
        await collector.start()
    """

    def __init__(
        self,
        context: AccountContext,
        on_execution: Optional[Callable[[ExecutionEvent], None]] = None,
        on_order: Optional[Callable[[OrderUpdateEvent], None]] = None,
        on_position: Optional[Callable[[dict], None]] = None,
        on_wallet: Optional[Callable[[dict], None]] = None,
        on_gap_detected: Optional[Callable[[datetime, datetime], None]] = None,
    ):
        """Initialize private collector for an account.

        Args:
            context: Account context with credentials and settings.
            on_execution: Callback for execution events.
            on_order: Callback for order update events.
            on_position: Callback for position snapshots (raw dict).
            on_wallet: Callback for wallet snapshots (raw dict).
            on_gap_detected: Callback when gap is detected (start, end).
        """
        self.context = context
        self._on_execution = on_execution
        self._on_order = on_order
        self._on_position = on_position
        self._on_wallet = on_wallet
        self._on_gap_detected = on_gap_detected

        # Set up normalizer with multi-tenant context
        normalizer_context = NormalizerContext(
            user_id=context.user_id,
            account_id=context.account_id,
            run_id=context.run_id,
        )
        self._normalizer = BybitNormalizer(context=normalizer_context)

        self._ws_client: Optional[PrivateWebSocketClient] = None
        self._running = False
        self._symbols_set = set(context.symbols) if context.symbols else set()

    async def start(self) -> None:
        """Start collecting private data for this account.

        Connects to authenticated WebSocket and subscribes to streams.
        """
        if self._running:
            logger.warning(f"PrivateCollector already running for account {self.context.account_id}")
            return

        is_testnet = self.context.environment == "testnet"
        logger.info(
            f"Starting PrivateCollector for account {self.context.account_id} "
            f"(testnet={is_testnet}, symbols={self.context.symbols})"
        )
        self._running = True

        self._ws_client = PrivateWebSocketClient(
            api_key=self.context.api_key,
            api_secret=self.context.api_secret,
            testnet=is_testnet,
            on_execution=self._handle_execution if self._on_execution else None,
            on_order=self._handle_order if self._on_order else None,
            on_position=self._handle_position if self._on_position else None,
            on_wallet=self._handle_wallet if self._on_wallet else None,
            on_disconnect=self._handle_disconnect,
            on_reconnect=self._handle_reconnect,
        )

        self._ws_client.connect()
        logger.info(f"PrivateCollector started for account {self.context.account_id}")

    async def stop(self) -> None:
        """Stop collecting for this account.

        Gracefully disconnects WebSocket.
        """
        if not self._running:
            return

        logger.info(f"Stopping PrivateCollector for account {self.context.account_id}")
        self._running = False

        if self._ws_client:
            self._ws_client.disconnect()
            self._ws_client = None

        logger.info(f"PrivateCollector stopped for account {self.context.account_id}")

    def is_running(self) -> bool:
        """Check if collector is running."""
        return self._running

    def get_connection_state(self) -> Optional[ConnectionState]:
        """Get current WebSocket connection state."""
        if self._ws_client:
            return self._ws_client.get_connection_state()
        return None

    def update_run_id(self, run_id: Optional[UUID]) -> None:
        """Update the run_id for subsequent events.

        Useful when a new run starts but account remains the same.

        Args:
            run_id: New run ID.
        """
        self.context.run_id = run_id
        self._normalizer.update_run_id(run_id)
        logger.info(f"Updated run_id to {run_id} for account {self.context.account_id}")

    def _should_filter_symbol(self, symbol: str) -> bool:
        """Check if symbol should be filtered out.

        Returns True if symbol should be ignored (not in configured list).
        """
        if not self._symbols_set:
            return False  # No filter, accept all symbols
        return symbol not in self._symbols_set

    def _handle_execution(self, message: dict) -> None:
        """Handle raw execution message from WebSocket."""
        try:
            events = self._normalizer.normalize_execution(message)
            for event in events:
                # Filter by symbol if configured
                if self._should_filter_symbol(event.symbol):
                    continue
                if self._on_execution:
                    self._on_execution(event)
        except Exception as e:
            logger.error(f"Error normalizing execution: {e}")

    def _handle_order(self, message: dict) -> None:
        """Handle raw order message from WebSocket."""
        try:
            events = self._normalizer.normalize_order(message)
            for event in events:
                # Filter by symbol if configured
                if self._should_filter_symbol(event.symbol):
                    continue
                if self._on_order:
                    self._on_order(event)
        except Exception as e:
            logger.error(f"Error normalizing order: {e}")

    def _handle_position(self, message: dict) -> None:
        """Handle raw position message from WebSocket.

        Passes raw message to callback for flexible handling.
        """
        try:
            if self._on_position:
                # Filter by symbol if configured
                data = message.get("data", [])
                filtered_data = []
                for pos in data:
                    symbol = pos.get("symbol", "")
                    if not self._should_filter_symbol(symbol):
                        filtered_data.append(pos)

                if filtered_data:
                    # Pass filtered message
                    filtered_message = {**message, "data": filtered_data}
                    self._on_position(filtered_message)
        except Exception as e:
            logger.error(f"Error handling position: {e}")

    def _handle_wallet(self, message: dict) -> None:
        """Handle raw wallet message from WebSocket.

        Passes raw message to callback for flexible handling.
        """
        try:
            if self._on_wallet:
                self._on_wallet(message)
        except Exception as e:
            logger.error(f"Error handling wallet: {e}")

    def _handle_disconnect(self, disconnect_ts: datetime) -> None:
        """Handle WebSocket disconnect event."""
        logger.warning(
            f"Private WebSocket disconnected for account {self.context.account_id} "
            f"at {disconnect_ts}"
        )

    def _handle_reconnect(self, disconnected_at: datetime, reconnected_at: datetime) -> None:
        """Handle WebSocket reconnect event for gap detection."""
        gap_seconds = (reconnected_at - disconnected_at).total_seconds()
        logger.info(
            f"Private WebSocket reconnected for account {self.context.account_id} "
            f"after {gap_seconds:.1f}s gap"
        )

        if self._on_gap_detected:
            self._on_gap_detected(disconnected_at, reconnected_at)
