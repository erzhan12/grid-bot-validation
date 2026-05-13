"""Collect private account data (executions, orders, positions, wallet)."""

import asyncio
import contextlib
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Optional
from uuid import UUID

from bybit_adapter.ws_client import PrivateWebSocketClient, ConnectionState
from bybit_adapter.normalizer import BybitNormalizer, NormalizerContext
from gridcore.events import ExecutionEvent, OrderUpdateEvent


logger = logging.getLogger(__name__)

_PRIVATE_WS_HEALTH_CHECK_INTERVAL = 10.0
_PRIVATE_WS_RESET_TIMEOUT = 30.0
_PRIVATE_WS_DISCONNECT_TIMEOUT = 5.0


def _run_in_daemon_thread(
    fn: Callable[[], Any], *, name: Optional[str] = None
) -> "asyncio.Future[Any]":
    """Run ``fn`` on a dedicated daemon thread; return a future bound to the loop.

    Used to wrap blocking pybit calls (``reset`` / ``disconnect``) so they can
    be bounded by ``asyncio.wait_for`` and **abandoned** on timeout without
    leaking into ``concurrent.futures.thread._python_exit`` at interpreter
    shutdown (which would join the worker and re-introduce the hang).

    Cancellation-safety: if ``wait_for`` cancels the future before the thread
    returns, the completer guards on ``fut.done()`` so a late-returning worker
    does not raise ``InvalidStateError`` on the loop. If the loop has been
    closed by the time the worker returns, ``call_soon_threadsafe`` raises
    ``RuntimeError`` which we swallow — the daemon thread exits quietly.
    """
    loop = asyncio.get_running_loop()
    fut: "asyncio.Future[Any]" = loop.create_future()

    def _complete(result: Any = None, exc: Optional[BaseException] = None) -> None:
        if fut.done():
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)

    def _target() -> None:
        try:
            result = fn()
            exc: Optional[BaseException] = None
        except BaseException as e:  # noqa: BLE001 — route to future
            result = None
            exc = e
        try:
            loop.call_soon_threadsafe(_complete, result, exc)
        except RuntimeError:
            # Loop already closed; daemon thread just exits.
            pass

    threading.Thread(target=_target, name=name, daemon=True).start()
    return fut


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
        ws_health_check_interval: float = _PRIVATE_WS_HEALTH_CHECK_INTERVAL,
        ws_reset_timeout: float = _PRIVATE_WS_RESET_TIMEOUT,
        ws_disconnect_timeout: float = _PRIVATE_WS_DISCONNECT_TIMEOUT,
    ):
        """Initialize private collector for an account.

        Args:
            context: Account context with credentials and settings.
            on_execution: Callback for execution events.
            on_order: Callback for order update events.
            on_position: Callback for position snapshots (raw dict).
            on_wallet: Callback for wallet snapshots (raw dict).
            on_gap_detected: Callback when gap is detected (start, end).
            ws_health_check_interval: TCP socket health-check interval in seconds.
            ws_reset_timeout: Bound on the blocking ``client.reset()`` call from
                the TCP health loop. On timeout the worker is abandoned and
                ``_handle_reconnect`` is skipped (no REST reconciliation on an
                unconfirmed reset).
            ws_disconnect_timeout: Bound on ``client.disconnect()`` during
                ``stop()`` so shutdown stays responsive when pybit is wedged.
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
        self._ws_health_check_interval = ws_health_check_interval
        self._ws_reset_timeout = ws_reset_timeout
        self._ws_disconnect_timeout = ws_disconnect_timeout
        self._ws_health_task: Optional[asyncio.Task[None]] = None
        self._ws_health_stop_event: Optional[asyncio.Event] = None
        self._ws_reset_abandoned = False

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
            message_gap_watchdog_enabled=False,
        )
        # Fresh client; defensively clear any abandoned-flag inherited from a
        # previous timed-out stop() so this collector is not crippled.
        self._ws_reset_abandoned = False

        self._ws_client.connect()
        self._ws_health_stop_event = asyncio.Event()
        self._ws_health_task = asyncio.create_task(self._ws_health_check_loop())
        logger.info(f"PrivateCollector started for account {self.context.account_id}")

    async def stop(self) -> None:
        """Stop collecting for this account.

        Gracefully disconnects WebSocket.
        """
        if not self._running:
            return

        logger.info(f"Stopping PrivateCollector for account {self.context.account_id}")
        self._running = False

        if self._ws_health_stop_event:
            self._ws_health_stop_event.set()

        if self._ws_health_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_health_task
            self._ws_health_task = None
            self._ws_health_stop_event = None

        client = self._ws_client
        if client is not None:
            if self._ws_reset_abandoned:
                logger.warning(
                    "Skipping private WS disconnect for account %s — prior "
                    "reset timed out and pybit is still parked; the worker "
                    "thread is leaked until the process exits",
                    self.context.account_id,
                )
            else:
                try:
                    await asyncio.wait_for(
                        _run_in_daemon_thread(
                            client.disconnect, name="ws-disconnect"
                        ),
                        timeout=self._ws_disconnect_timeout,
                    )
                except TimeoutError:
                    logger.warning(
                        "Private WS disconnect timed out after %.1fs for "
                        "account %s; clearing client and continuing",
                        self._ws_disconnect_timeout,
                        self.context.account_id,
                    )
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

    async def _ws_health_check_loop(self) -> None:
        """Reset dead private sockets and trigger REST reconciliation."""
        while self._running:
            stop_event = self._ws_health_stop_event
            if stop_event is None:
                return
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._ws_health_check_interval,
                )
                return
            except TimeoutError:
                pass
            await self._ws_health_check_once()

    async def _ws_health_check_once(self) -> None:
        """Perform one TCP-level private WebSocket health check."""
        client = self._ws_client
        if not self._running or client is None:
            return

        if self._ws_reset_abandoned:
            # A previous reset() timed out and the worker is still parked
            # inside pybit holding PrivateWebSocketClient._lock. is_socket_alive
            # also acquires that lock, so touching the client here would block
            # the event loop and reintroduce the SIGTERM hang this feature is
            # meant to fix. Stay out until start()/stop() resets the flag.
            return

        try:
            if client.is_socket_alive():
                return

            state = client.get_connection_state()
            disconnected_at = (
                state.last_message_ts
                or state.disconnected_at
                or datetime.now(UTC)
            )
            self._handle_disconnect(disconnected_at)

            logger.warning(
                "Private WebSocket socket dead for account %s; resetting",
                self.context.account_id,
            )
            try:
                await asyncio.wait_for(
                    _run_in_daemon_thread(client.reset, name="ws-reset"),
                    timeout=self._ws_reset_timeout,
                )
            except TimeoutError:
                self._ws_reset_abandoned = True
                logger.error(
                    "Private WS reset timed out after %.1fs for account %s; "
                    "abandoning worker thread and skipping REST gap "
                    "reconciliation (reset unconfirmed)",
                    self._ws_reset_timeout,
                    self.context.account_id,
                )
                return
            self._ws_reset_abandoned = False
            self._handle_reconnect(disconnected_at, datetime.now(UTC))
        except Exception as e:
            logger.error(
                "Private WebSocket health check failed for account %s: %s",
                self.context.account_id,
                e,
                exc_info=True,
            )

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
