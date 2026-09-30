"""Collect private account data (executions, orders, positions, wallet)."""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable, Optional
from uuid import UUID

from bybit_adapter.ws_client import PrivateWebSocketClient, ConnectionState
from bybit_adapter.normalizer import BybitNormalizer, NormalizerContext
from gridcore.events import ExecutionEvent, OrderUpdateEvent

from event_saver.collectors._startup import (
    CollectorStartError,
    StartHandoff,
    disconnect_in_background,
    run_in_daemon_thread as _run_in_daemon_thread,
)


logger = logging.getLogger(__name__)


_PRIVATE_WS_HEALTH_CHECK_INTERVAL = 10.0
_PRIVATE_WS_RESET_TIMEOUT = 30.0
_PRIVATE_WS_DISCONNECT_TIMEOUT = 5.0
# Feature 0110 B1b: how long a reset waits for auth + subscription acks.
_PRIVATE_READY_TIMEOUT = 5.0
# Feature 0110 B1c-2: bound on connect() + readiness together at start().
# Longer than the post-reset wait: a refused start costs more than a few
# seconds, and the Phase 4 launcher waits 60 s for the snapshot sentinel.
_PRIVATE_START_TIMEOUT = 10.0
# Extra time the event loop gives the wait_ready thread past its own deadline.
_READY_WAIT_SLACK = 1.0
# Bound on the owner's on_healthy_probe (coverage checkpoint) per probe.
_HEALTHY_PROBE_TIMEOUT = 10.0
# Resets in a row that end not ready before the socket is kept on liveness
# checks only (e.g. one topic rejected: the others still deliver data).
_MAX_UNREADY_RESETS = 3
# A half-open socket still reads "connected" until pybit's ping times out:
# websocket-client's first ping goes out ~2x ping_interval (20 s) after
# open and the timeout (10 s) is only checked after the next select, so
# detection can take ~2x20 + 2x10 = 60 s; +15 s slack. A gap therefore
# starts this long before the last healthy probe. Public: the recorder's
# coverage checkpoint must lag by the same amount.
LIVENESS_MARGIN = timedelta(seconds=75)


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
        on_disconnect: Optional[Callable[[datetime], None]] = None,
        on_healthy_probe: Optional[Callable[[], Awaitable[None]]] = None,
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
            on_disconnect: Called with the gap start when the socket is found
                unhealthy, BEFORE it is reset. If it raises, the reset is
                skipped this probe and retried on the next one (so the owner's
                open-gap record always exists before recovery work).
            on_healthy_probe: Awaited on each healthy probe of a confirmed
                ready socket (never while kept on liveness checks only); the
                owner advances its coverage checkpoint here.
            ws_health_check_interval: Private socket health-check interval in
                seconds.
            ws_reset_timeout: Bound on the blocking ``client.reset()`` call from
                the health loop. On timeout the worker is abandoned and
                ``_handle_reconnect`` is skipped (no REST reconciliation on an
                unconfirmed reset). A reset whose new socket is not ready also
                skips it; the next probe retries.
            ws_disconnect_timeout: Bound on ``client.disconnect()`` during
                ``stop()`` so shutdown stays responsive when pybit is wedged.
        """
        self.context = context
        self._on_execution = on_execution
        self._on_order = on_order
        self._on_position = on_position
        self._on_wallet = on_wallet
        self._on_gap_detected = on_gap_detected
        self._on_disconnect = on_disconnect
        self._on_healthy_probe = on_healthy_probe

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
        # Feature 0110 B1b: identity of the pybit socket last confirmed
        # ready, whether it is ready, and the last healthy probe time.
        self._identity_baseline: object = None
        self._ready = False
        self._last_healthy_ts: Optional[datetime] = None
        self._connected_at: Optional[datetime] = None
        self._unready_resets = 0
        # Kept on liveness checks only after _MAX_UNREADY_RESETS: healthy
        # probes no longer certify coverage (a topic may never be acked).
        self._liveness_only = False

    async def start(self) -> None:
        """Start collecting private data for this account.

        Connects to the authenticated WebSocket, subscribes to streams and
        waits for auth plus every subscription ack — all off the event loop
        and within ``_PRIVATE_START_TIMEOUT`` in total.

        Raises:
            CollectorStartError: Not connected and ready in time (or the
                connect / readiness check raised). The socket is
                disconnected where possible and the collector is left not
                running.
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
            track_subscription_acks=True,
        )
        # Fresh client; defensively clear any abandoned-flag inherited from a
        # previous timed-out stop() so this collector is not crippled.
        self._ws_reset_abandoned = False

        # A gap never starts before the connect time (no pre-run executions
        # under this run).
        self._connected_at = self._last_healthy_ts = datetime.now(UTC)
        self._unready_resets = 0
        self._liveness_only = False
        self._ready = False
        await self._connect_and_confirm(self._ws_client)
        self._ws_health_stop_event = asyncio.Event()
        self._ws_health_task = asyncio.create_task(self._ws_health_check_loop())
        logger.info(f"PrivateCollector started for account {self.context.account_id}")

    async def _connect_and_confirm(self, client: PrivateWebSocketClient) -> None:
        """Connect and wait for readiness off the loop, within one bound.

        ``connect()`` is a blocking pybit call with no timeout of its own, so
        it shares a worker thread and ``_PRIVATE_START_TIMEOUT`` with the
        readiness wait. The identity baseline is read between the two, so a
        pybit silent reconnect during the wait fails readiness.

        Raises:
            CollectorStartError: see :meth:`start`.
        """
        account = self.context.account_id

        handoff = StartHandoff()

        def _connect_and_wait() -> tuple[bool, object]:
            deadline = time.monotonic() + _PRIVATE_START_TIMEOUT
            try:
                client.connect()
                if handoff.abandoned():
                    # start() already gave up; pybit retries forever, so
                    # this connect can still succeed later. Close it rather
                    # than leave a live socket feeding a collector that is
                    # not running.
                    client.disconnect()
                    return False, None
                baseline = client.socket_identity()
                remaining = max(deadline - time.monotonic(), 0.0)
                ready = client.wait_ready(remaining)
            except BaseException:
                if not handoff.finish():
                    # The owner gave up, so nobody is waiting for this
                    # error: close what was opened before letting it go.
                    # (If it gives up AFTER this, abandon() tells it to.)
                    with contextlib.suppress(Exception):
                        client.disconnect()
                raise
            if not handoff.finish():  # gave up during the readiness wait
                client.disconnect()
                return False, None
            return ready, baseline

        start_fut = _run_in_daemon_thread(_connect_and_wait, name="ws-start")
        try:
            ready, baseline = await asyncio.wait_for(
                start_fut, timeout=_PRIVATE_START_TIMEOUT + _READY_WAIT_SLACK
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            if isinstance(exc, TimeoutError) and not start_fut.cancelled():
                # The worker itself raised TimeoutError (e.g. socket.timeout):
                # a start failure with a cause, not the bound.
                await self._abort_start(client)
                raise CollectorStartError(
                    f"Private WebSocket start failed for account {account}: "
                    f"{exc!r}"
                ) from exc
            # The worker is normally parked inside pybit holding the client
            # lock: disconnect() would block on it. Abandon the (daemon)
            # thread; it disconnects by itself if connect() ever returns.
            # If it had already finished, the socket is ours to close.
            if handoff.abandon():
                disconnect_in_background(client, name="ws-disconnect")
            self._ws_client = None
            self._running = False
            if isinstance(exc, asyncio.CancelledError):
                raise  # start() was cancelled (shutdown signal)
            raise CollectorStartError(
                f"Private WebSocket start timed out after "
                f"{_PRIVATE_START_TIMEOUT:.1f}s for account {account}"
            ) from None
        except Exception as exc:
            await self._abort_start(client)
            raise CollectorStartError(
                f"Private WebSocket start failed for account {account}: {exc}"
            ) from exc
        if not ready:
            await self._abort_start(client)
            raise CollectorStartError(
                f"Private WebSocket not ready within "
                f"{_PRIVATE_START_TIMEOUT:.1f}s for account {account}"
            )
        self._ready = True
        self._identity_baseline = baseline
        self._last_healthy_ts = datetime.now(UTC)

    async def _abort_start(self, client: PrivateWebSocketClient) -> None:
        """Disconnect a client whose start failed; leave the collector stopped."""
        # State first: a cancellation during the disconnect below must still
        # leave the collector stopped.
        self._ws_client = None
        self._running = False
        try:
            await asyncio.wait_for(
                _run_in_daemon_thread(client.disconnect, name="ws-disconnect"),
                timeout=self._ws_disconnect_timeout,
            )
        except Exception:
            logger.warning(
                "Private WS disconnect after a failed start did not complete "
                "for account %s",
                self.context.account_id,
                exc_info=True,
            )

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

    def is_degraded(self) -> bool:
        """True while the socket is kept on liveness checks only.

        Set after ``_MAX_UNREADY_RESETS`` resets that never became ready;
        cleared by the next confirmed readiness. While set, healthy probes do
        not call ``on_healthy_probe`` (coverage is not certified).
        """
        return self._liveness_only

    def get_connection_state(self) -> Optional[ConnectionState]:
        """Get current WebSocket connection state."""
        if self._ws_client:
            return self._ws_client.get_connection_state()
        return None

    async def _ws_health_check_loop(self) -> None:
        """Reset unhealthy private sockets and trigger REST reconciliation."""
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
        """Perform one private WebSocket health check.

        Unhealthy means not confirmed ready, a dead TCP socket, lost auth, or
        a different pybit socket than the ready one (a silent reconnect).
        """
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
            if self._is_healthy(client):
                if self._liveness_only:
                    # Not certified: leave the last-healthy time at the
                    # moment it degraded, so the next gap reaches back over
                    # the whole liveness-only stretch.
                    if not client.wait_ready(0):  # single check, no wait
                        return
                    # Acks landed on this socket after all: certified again;
                    # recover the liveness-only stretch as a gap.
                    degraded_at = self._gap_start()
                    self._liveness_only = False
                    self._last_healthy_ts = datetime.now(UTC)
                    logger.info(
                        "Private WebSocket for account %s is ready again "
                        "(was on liveness checks only)",
                        self.context.account_id,
                    )
                    self._handle_reconnect(degraded_at, self._last_healthy_ts)
                    return
                self._last_healthy_ts = datetime.now(UTC)
                if self._on_healthy_probe is not None and self._running:
                    await self._run_checkpoint()
                return

            disconnected_at = self._gap_start()

            if (
                not self._ready
                and client.is_socket_alive()
                and await self._confirm_ready(client)
            ):
                # Acks landed after the ready timeout: keep this socket (a
                # reset would restart the clock) and recover the unready
                # stretch as a gap.
                logger.info(
                    "Private WebSocket became ready late for account %s",
                    self.context.account_id,
                )
                self._handle_reconnect(disconnected_at, datetime.now(UTC))
                return
            if not self._running:  # stop() ended the readiness wait
                return

            self._handle_disconnect(disconnected_at)
            if self._on_disconnect is not None:
                # Raises → the outer except logs it and this probe skips the
                # reset; the next probe retries with the same gap start.
                self._on_disconnect(disconnected_at)

            logger.warning(
                "Private WebSocket unhealthy for account %s "
                "(socket dead, silently reconnected, unauthenticated or "
                "never ready); resetting",
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
            if not await self._confirm_ready(client):
                if not self._running:  # stop() ended the wait
                    return
                self._unready_resets += 1
                if self._unready_resets < _MAX_UNREADY_RESETS:
                    # Gap stays unreported; the next probe retries and
                    # reports it from the same (unchanged) last-healthy time.
                    unready_for = datetime.now(UTC) - (
                        self._last_healthy_ts or disconnected_at
                    )
                    logger.error(
                        "Private WebSocket not ready after reset for "
                        "account %s (unready for %.0fs)",
                        self.context.account_id,
                        unready_for.total_seconds(),
                    )
                    return
                # Resetting again would only tear down whatever the stream
                # still delivers: keep the socket, report the gap so REST
                # backfill runs.
                logger.error(
                    "Private WebSocket for account %s still not ready after "
                    "%d resets; keeping it on liveness checks only",
                    self.context.account_id,
                    self._unready_resets,
                )
                self._unready_resets = 0
                self._liveness_only = True
                self._ready = True
                self._identity_baseline = client.socket_identity()
                self._last_healthy_ts = datetime.now(UTC)
            self._handle_reconnect(disconnected_at, datetime.now(UTC))
        except Exception as e:
            logger.error(
                "Private WebSocket health check failed for account %s: %s",
                self.context.account_id,
                e,
                exc_info=True,
            )

    async def _wait_unless_stopped(
        self, fut: "asyncio.Future[Any]", timeout: float
    ) -> bool:
        """Wait for ``fut``; False if ``timeout`` passed or stop() came first.

        Races ``_ws_health_stop_event`` so shutdown never waits out a slow
        reply. The caller cancels ``fut`` when this returns False.
        """
        waiters = {fut}
        stop_event = self._ws_health_stop_event
        stopping = None
        if stop_event is not None:
            stopping = asyncio.ensure_future(stop_event.wait())
            waiters.add(stopping)
        try:
            await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if stopping is not None:
                stopping.cancel()
        return fut.done()

    async def _run_checkpoint(self) -> None:
        """Await the owner's ``on_healthy_probe``, bounded and stop-aware.

        A slow or failing checkpoint only means it does not advance this
        probe; the socket stays healthy.
        """
        task = asyncio.ensure_future(self._on_healthy_probe())
        if not await self._wait_unless_stopped(task, _HEALTHY_PROBE_TIMEOUT):
            task.cancel()
            if self._running:  # a timeout, not stop()
                logger.warning(
                    "Private stream checkpoint timed out after %.1fs for "
                    "account %s; not advanced",
                    _HEALTHY_PROBE_TIMEOUT,
                    self.context.account_id,
                )
            return
        try:
            task.result()
        except Exception:
            logger.error(
                "Private stream checkpoint failed for account %s",
                self.context.account_id,
                exc_info=True,
            )

    def _gap_start(self) -> datetime:
        """Conservative gap start for an outage detected now.

        The last healthy probe minus the time a half-open socket can still
        look alive (not message age — a healthy private stream can be quiet
        for days), never before the collector connected.
        """
        start = (self._last_healthy_ts or datetime.now(UTC)) - LIVENESS_MARGIN
        if self._connected_at is not None:
            start = max(start, self._connected_at)
        return start

    def _is_healthy(self, client: PrivateWebSocketClient) -> bool:
        """Alive, confirmed ready, authenticated, and the same pybit socket."""
        return (
            self._ready
            and client.is_socket_alive()
            and client.is_authenticated()
            and client.socket_identity() is self._identity_baseline
        )

    async def _confirm_ready(self, client: PrivateWebSocketClient) -> bool:
        """Wait (off the loop) for auth + acks; on success take the baseline.

        The baseline identity is read BEFORE waiting, so a pybit silent
        reconnect during the wait fails readiness and is caught by the
        next probe. ``stop()`` ends the wait at once (not ready): the waiting
        thread holds no lock and is abandoned. Used after a reset and for a
        late-readiness re-check; ``start()`` uses ``_connect_and_confirm``.
        """
        self._ready = False
        baseline = client.socket_identity()
        ready_wait = _run_in_daemon_thread(
            lambda: client.wait_ready(_PRIVATE_READY_TIMEOUT), name="ws-ready"
        )
        if not await self._wait_unless_stopped(
            ready_wait, _PRIVATE_READY_TIMEOUT + _READY_WAIT_SLACK
        ):
            ready_wait.cancel()
            return False
        try:
            ready = ready_wait.result()
        except Exception:
            # e.g. a pybit internals change: degrade to "not ready".
            logger.error(
                "Private WebSocket readiness check failed for account %s",
                self.context.account_id,
                exc_info=True,
            )
            ready = False
        if ready:
            self._ready = True
            self._liveness_only = False
            self._unready_resets = 0
            self._identity_baseline = baseline
            self._last_healthy_ts = datetime.now(UTC)
        return bool(ready)

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
