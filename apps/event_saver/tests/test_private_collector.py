"""Tests for PrivateCollector."""

import asyncio
import logging
import threading
import time
import pytest
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

from bybit_adapter.ws_client import ConnectionState
from event_saver.collectors.private_collector import (
    PrivateCollector,
    AccountContext,
    _LIVENESS_MARGIN,
    _PRIVATE_READY_TIMEOUT,
    _run_in_daemon_thread,
)
from gridcore.events import ExecutionEvent, OrderUpdateEvent


@pytest.fixture
def context():
    return AccountContext(
        account_id=uuid4(),
        user_id=uuid4(),
        run_id=uuid4(),
        api_key="test_key",
        api_secret="test_secret",
        environment="testnet",
        symbols=["BTCUSDT"],
    )


@pytest.fixture
def on_execution():
    return MagicMock()


@pytest.fixture
def on_order():
    return MagicMock()


@pytest.fixture
def on_position():
    return MagicMock()


@pytest.fixture
def on_wallet():
    return MagicMock()


@pytest.fixture
def on_gap():
    return MagicMock()


@pytest.fixture
def collector(context, on_execution, on_order, on_position, on_wallet, on_gap):
    return PrivateCollector(
        context=context,
        on_execution=on_execution,
        on_order=on_order,
        on_position=on_position,
        on_wallet=on_wallet,
        on_gap_detected=on_gap,
    )


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


class TestInit:
    def test_stores_context(self, collector, context):
        assert collector.context is context

    def test_not_running(self, collector):
        assert collector.is_running() is False

    def test_symbols_set(self, collector):
        assert collector._symbols_set == {"BTCUSDT"}

    def test_empty_symbols_no_filter(self, context, on_execution):
        context.symbols = []
        col = PrivateCollector(context=context, on_execution=on_execution)
        assert col._symbols_set == set()


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_ws_and_connects(self, collector):
        with patch("event_saver.collectors.private_collector.PrivateWebSocketClient") as MockWS:
            mock_ws = MagicMock()
            MockWS.return_value = mock_ws

            await collector.start()
            try:
                assert collector.is_running() is True
                mock_ws.connect.assert_called_once()
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_start_uses_correct_testnet_flag(self, collector, context):
        with patch("event_saver.collectors.private_collector.PrivateWebSocketClient") as MockWS:
            MockWS.return_value = MagicMock()
            await collector.start()
            try:
                call_kwargs = MockWS.call_args[1]
                assert call_kwargs["testnet"] is True
                assert call_kwargs["api_key"] == "test_key"
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_start_disables_private_message_gap_watchdog(self, collector):
        # Feature 0035 — mirrors gridbot feature 0026: the message-gap watchdog
        # produces false-positive disconnects on a healthy quiet private WS
        # because pybit's ping/pong frames bypass the business-event handler.
        with patch("event_saver.collectors.private_collector.PrivateWebSocketClient") as MockWS:
            MockWS.return_value = MagicMock()
            await collector.start()
            try:
                call_kwargs = MockWS.call_args[1]
                assert call_kwargs["message_gap_watchdog_enabled"] is False
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_start_does_not_spawn_private_heartbeat_thread(self, collector):
        # Feature 0035 defense-in-depth: prove end-to-end through the real
        # PrivateWebSocketClient that the watchdog gate is honoured — the
        # heartbeat thread must not start when the flag is False. Mirrors
        # test_ws_client.py::test_private_watchdog_disabled_skips_heartbeat_thread
        # but goes through the recorder's collector path so a regression in
        # private_collector.py is caught here too.
        with patch("bybit_adapter.ws_client.WebSocket") as MockWebSocket:
            mock_ws = MagicMock()
            MockWebSocket.return_value = mock_ws

            await collector.start()
            try:
                assert collector._ws_client is not None
                # Heartbeat thread must not be started when the watchdog is off.
                assert collector._ws_client._heartbeat_thread is None
                # connect() did not short-circuit before the gate — connection
                # is logically up and all four stream subscriptions were
                # registered with the (mocked) pybit session.
                assert collector._ws_client.is_connected() is True
                mock_ws.execution_stream.assert_called_once()
                mock_ws.order_stream.assert_called_once()
                mock_ws.position_stream.assert_called_once()
                mock_ws.wallet_stream.assert_called_once()
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_start_twice_warns(self, collector):
        with patch("event_saver.collectors.private_collector.PrivateWebSocketClient") as MockWS:
            MockWS.return_value = MagicMock()
            await collector.start()
            try:
                MockWS.reset_mock()
                await collector.start()

                MockWS.assert_not_called()
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_stop_disconnects(self, collector):
        with patch("event_saver.collectors.private_collector.PrivateWebSocketClient") as MockWS:
            mock_ws = MagicMock()
            MockWS.return_value = mock_ws

            await collector.start()
            await collector.stop()

            assert collector.is_running() is False
            mock_ws.disconnect.assert_called_once()
            assert collector._ws_client is None

    @pytest.mark.asyncio
    async def test_stop_noop_if_not_running(self, collector):
        await collector.stop()

    @pytest.mark.asyncio
    async def test_private_ws_health_resets_dead_socket_and_reconciles(self, context, on_gap):
        disconnected_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        collector = PrivateCollector(
            context=context,
            on_gap_detected=on_gap,
        )
        mock_ws = MagicMock()
        mock_ws.is_socket_alive.return_value = False
        _mark_ready(collector, mock_ws)
        mock_ws.get_connection_state.return_value = ConnectionState(
            last_message_ts=disconnected_at,
            is_connected=True,
        )
        collector._running = True
        collector._ws_client = mock_ws
        # 0110 B1b: gap start is the last healthy probe minus the liveness
        # margin, not the last message time.
        collector._last_healthy_ts = disconnected_at + _LIVENESS_MARGIN

        await collector._ws_health_check_once()

        mock_ws.is_socket_alive.assert_called()  # reached the liveness check
        mock_ws.reset.assert_called_once()
        on_gap.assert_called_once()
        assert on_gap.call_args[0][0] == disconnected_at
        assert on_gap.call_args[0][1] >= disconnected_at

    @pytest.mark.asyncio
    async def test_stop_waits_for_in_flight_health_reset_before_disconnect(
        self, context, on_gap
    ):
        disconnected_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        collector = PrivateCollector(
            context=context,
            on_gap_detected=on_gap,
        )
        reset_started = threading.Event()
        allow_reset_finish = threading.Event()
        events: list[str] = []

        def reset() -> None:
            events.append("reset_start")
            reset_started.set()
            assert allow_reset_finish.wait(timeout=1.0)
            events.append("reset_done")

        mock_ws = MagicMock()
        mock_ws.is_socket_alive.return_value = False
        _mark_ready(collector, mock_ws)
        mock_ws.get_connection_state.return_value = ConnectionState(
            last_message_ts=disconnected_at,
            is_connected=True,
        )
        mock_ws.reset.side_effect = reset
        mock_ws.disconnect.side_effect = lambda: events.append("disconnect")
        collector._running = True
        collector._ws_client = mock_ws
        collector._ws_health_stop_event = asyncio.Event()
        collector._ws_health_task = asyncio.create_task(
            collector._ws_health_check_once()
        )

        assert await asyncio.to_thread(reset_started.wait, 1.0)
        stop_task = asyncio.create_task(collector.stop())
        await asyncio.sleep(0.01)

        assert stop_task.done() is False
        assert "disconnect" not in events

        allow_reset_finish.set()
        await stop_task

        assert events == ["reset_start", "reset_done", "disconnect"]

    def test_get_connection_state_none_without_client(self, collector):
        assert collector.get_connection_state() is None


# ---------------------------------------------------------------------------
# Symbol filtering
# ---------------------------------------------------------------------------


class TestSymbolFiltering:
    def test_should_filter_non_subscribed_symbol(self, collector):
        assert collector._should_filter_symbol("ETHUSDT") is True

    def test_should_not_filter_subscribed_symbol(self, collector):
        assert collector._should_filter_symbol("BTCUSDT") is False

    def test_no_filter_when_empty_symbols(self, context, on_execution):
        context.symbols = []
        col = PrivateCollector(context=context, on_execution=on_execution)
        assert col._should_filter_symbol("ANYTHING") is False


# ---------------------------------------------------------------------------
# _handle_execution
# ---------------------------------------------------------------------------


class TestHandleExecution:
    def test_normalizes_and_forwards(self, collector, on_execution):
        msg = {
            "topic": "execution",
            "id": "msg-1",
            "creationTime": 1704639600000,
            "data": [
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "execId": "e1",
                    "orderId": "o1",
                    "orderLinkId": "link1",
                    "execPrice": "42500.50",
                    "execQty": "0.1",
                    "execFee": "0.425",
                    "execType": "Trade",
                    "execTime": "1704639600000",
                    "side": "Buy",
                    "leavesQty": "0",
                    "closedPnl": "0",
                    "closedSize": "0",
                    "isMaker": True,
                },
            ],
        }

        collector._handle_execution(msg)

        on_execution.assert_called_once()
        event = on_execution.call_args[0][0]
        assert isinstance(event, ExecutionEvent)
        assert event.symbol == "BTCUSDT"

    def test_filters_non_subscribed_symbols(self, collector, on_execution):
        msg = {
            "topic": "execution",
            "id": "msg-1",
            "creationTime": 1704639600000,
            "data": [
                {
                    "category": "linear",
                    "symbol": "ETHUSDT",
                    "execId": "e1",
                    "orderId": "o1",
                    "orderLinkId": "link1",
                    "execPrice": "3500",
                    "execQty": "0.1",
                    "execFee": "0.1",
                    "execType": "Trade",
                    "execTime": "1704639600000",
                    "side": "Buy",
                    "leavesQty": "0",
                    "closedPnl": "0",
                    "closedSize": "0",
                    "isMaker": True,
                },
            ],
        }

        collector._handle_execution(msg)

        on_execution.assert_not_called()

    def test_handles_error_gracefully(self, collector, on_execution):
        collector._handle_execution({"invalid": "data"})
        on_execution.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_order
# ---------------------------------------------------------------------------


class TestHandleOrder:
    def test_normalizes_and_forwards(self, collector, on_order):
        msg = {
            "topic": "order",
            "id": "msg-1",
            "creationTime": 1704639600000,
            "data": [
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "orderId": "o1",
                    "orderLinkId": "link1",
                    "orderType": "Limit",
                    "orderStatus": "New",
                    "side": "Buy",
                    "price": "42000.00",
                    "qty": "0.1",
                    "leavesQty": "0.1",
                    "updatedTime": "1704639600000",
                },
            ],
        }

        collector._handle_order(msg)

        on_order.assert_called_once()
        event = on_order.call_args[0][0]
        assert isinstance(event, OrderUpdateEvent)

    def test_filters_non_subscribed_symbols(self, collector, on_order):
        msg = {
            "topic": "order",
            "id": "msg-1",
            "creationTime": 1704639600000,
            "data": [
                {
                    "category": "linear",
                    "symbol": "ETHUSDT",
                    "orderId": "o1",
                    "orderLinkId": "link1",
                    "orderType": "Limit",
                    "orderStatus": "New",
                    "side": "Buy",
                    "price": "3500",
                    "qty": "0.1",
                    "leavesQty": "0.1",
                    "updatedTime": "1704639600000",
                },
            ],
        }

        collector._handle_order(msg)

        on_order.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_position
# ---------------------------------------------------------------------------


class TestHandlePosition:
    def test_forwards_filtered_position(self, collector, on_position):
        msg = {
            "data": [
                {"symbol": "BTCUSDT", "side": "Buy", "size": "0.1"},
                {"symbol": "ETHUSDT", "side": "Buy", "size": "1.0"},
            ],
        }

        collector._handle_position(msg)

        on_position.assert_called_once()
        filtered = on_position.call_args[0][0]
        assert len(filtered["data"]) == 1
        assert filtered["data"][0]["symbol"] == "BTCUSDT"

    def test_no_callback_if_all_filtered(self, collector, on_position):
        msg = {"data": [{"symbol": "ETHUSDT", "side": "Buy", "size": "1.0"}]}

        collector._handle_position(msg)

        on_position.assert_not_called()

    def test_handles_error_gracefully(self, collector, on_position):
        collector._handle_position(None)  # Should not crash
        on_position.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_wallet
# ---------------------------------------------------------------------------


class TestHandleWallet:
    def test_forwards_wallet_message(self, collector, on_wallet):
        msg = {"data": [{"coin": [{"coin": "USDT", "walletBalance": "10000"}]}]}

        collector._handle_wallet(msg)

        on_wallet.assert_called_once_with(msg)

    def test_handles_error_gracefully(self, collector, on_wallet):
        on_wallet.side_effect = Exception("callback error")
        collector._handle_wallet({"data": []})
        # Should not crash


# ---------------------------------------------------------------------------
# Disconnect / Reconnect / update_run_id
# ---------------------------------------------------------------------------


class TestMisc:
    def test_handle_disconnect_logs(self, collector, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            collector._handle_disconnect(datetime(2025, 1, 1))
        assert "disconnected" in caplog.text

    def test_handle_reconnect_calls_gap_callback(self, collector, on_gap):
        d1 = datetime(2025, 1, 1, 0, 0, 0)
        d2 = datetime(2025, 1, 1, 0, 0, 10)

        collector._handle_reconnect(d1, d2)

        on_gap.assert_called_once_with(d1, d2)

    def test_handle_reconnect_noop_without_callback(self, context):
        col = PrivateCollector(context=context, on_gap_detected=None)
        col._handle_reconnect(datetime(2025, 1, 1), datetime(2025, 1, 1))

    def test_update_run_id(self, collector, context):
        new_run_id = uuid4()
        collector.update_run_id(new_run_id)

        assert context.run_id == new_run_id


# ---------------------------------------------------------------------------
# WS reset / disconnect timeout (Feature 0039)
# ---------------------------------------------------------------------------


class TestWsResetTimeout:
    @pytest.mark.asyncio
    async def test_private_ws_health_reset_timeout_skips_reconcile(
        self, context, on_gap, caplog
    ):
        disconnected_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        collector = PrivateCollector(
            context=context,
            on_gap_detected=on_gap,
            ws_reset_timeout=0.05,
        )
        allow_reset_finish = threading.Event()

        def reset() -> None:
            assert allow_reset_finish.wait(timeout=2.0)

        mock_ws = MagicMock()
        mock_ws.is_socket_alive.return_value = False
        _mark_ready(collector, mock_ws)
        mock_ws.get_connection_state.return_value = ConnectionState(
            last_message_ts=disconnected_at,
            is_connected=True,
        )
        mock_ws.reset.side_effect = reset
        collector._running = True
        collector._ws_client = mock_ws

        try:
            with caplog.at_level(logging.ERROR):
                await collector._ws_health_check_once()

            on_gap.assert_not_called()
            assert collector._ws_reset_abandoned is True
            assert any(
                "timed out" in record.getMessage().lower()
                and str(context.account_id) in record.getMessage()
                for record in caplog.records
            )
        finally:
            allow_reset_finish.set()

    @pytest.mark.asyncio
    async def test_stop_skips_disconnect_after_reset_timeout(self, context, on_gap):
        ws_reset_timeout = 0.05
        ws_disconnect_timeout = 0.05
        disconnected_at = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        collector = PrivateCollector(
            context=context,
            on_gap_detected=on_gap,
            ws_reset_timeout=ws_reset_timeout,
            ws_disconnect_timeout=ws_disconnect_timeout,
        )
        reset_started = threading.Event()
        allow_reset_finish = threading.Event()

        def reset() -> None:
            reset_started.set()
            assert allow_reset_finish.wait(timeout=2.0)

        mock_ws = MagicMock()
        mock_ws.is_socket_alive.return_value = False
        _mark_ready(collector, mock_ws)
        mock_ws.get_connection_state.return_value = ConnectionState(
            last_message_ts=disconnected_at,
            is_connected=True,
        )
        mock_ws.reset.side_effect = reset
        collector._running = True
        collector._ws_client = mock_ws
        collector._ws_health_stop_event = asyncio.Event()
        collector._ws_health_task = asyncio.create_task(
            collector._ws_health_check_once()
        )

        try:
            assert await asyncio.to_thread(reset_started.wait, 2.0)

            # Drive stop() while worker is STILL parked.
            await asyncio.wait_for(
                collector.stop(),
                timeout=ws_reset_timeout + ws_disconnect_timeout + 0.5,
            )

            # Assert abandonment BEFORE releasing the event.
            assert mock_ws.disconnect.call_count == 0
            assert collector._ws_client is None
            assert collector._ws_reset_abandoned is True
        finally:
            allow_reset_finish.set()

    @pytest.mark.asyncio
    async def test_subsequent_health_check_does_not_touch_abandoned_client(
        self, context
    ):
        # Regression for review P1: after a reset timeout, the abandoned pybit
        # worker is still holding PrivateWebSocketClient._lock. is_socket_alive
        # acquires that same lock, so the next health tick must not call any
        # lock-taking method on the client — otherwise the event loop blocks
        # exactly when SIGTERM needs it.
        collector = PrivateCollector(context=context)
        collector._running = True

        lock_held = threading.Event()
        release_lock = threading.Event()

        def is_socket_alive_blocks():
            lock_held.set()
            assert release_lock.wait(timeout=2.0)
            return False

        mock_ws = MagicMock()
        mock_ws.is_socket_alive.side_effect = is_socket_alive_blocks
        collector._ws_client = mock_ws
        collector._ws_reset_abandoned = True

        try:
            # If the guard is missing, is_socket_alive will block forever and
            # wait_for will fire.
            await asyncio.wait_for(collector._ws_health_check_once(), timeout=0.2)

            assert mock_ws.is_socket_alive.call_count == 0
            assert mock_ws.reset.call_count == 0
        finally:
            release_lock.set()

    @pytest.mark.asyncio
    async def test_start_after_timed_out_stop_clears_abandoned_flag(self, context):
        collector = PrivateCollector(context=context)
        collector._ws_reset_abandoned = True

        with patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient"
        ) as MockWS:
            mock_ws = MagicMock()
            MockWS.return_value = mock_ws

            await collector.start()
            try:
                assert collector._ws_reset_abandoned is False
            finally:
                await collector.stop()

            mock_ws.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_bounds_disconnect_when_no_prior_reset_timeout(
        self, context, caplog
    ):
        ws_disconnect_timeout = 0.05
        collector = PrivateCollector(
            context=context,
            ws_disconnect_timeout=ws_disconnect_timeout,
        )
        allow_disconnect_finish = threading.Event()

        def disconnect() -> None:
            assert allow_disconnect_finish.wait(timeout=2.0)

        mock_ws = MagicMock()
        mock_ws.disconnect.side_effect = disconnect
        collector._running = True
        collector._ws_client = mock_ws

        try:
            with caplog.at_level(logging.WARNING):
                await asyncio.wait_for(
                    collector.stop(),
                    timeout=ws_disconnect_timeout + 0.5,
                )

            assert collector._ws_client is None
            assert any(
                "disconnect" in record.getMessage().lower()
                and "timed out" in record.getMessage().lower()
                for record in caplog.records
            )
        finally:
            allow_disconnect_finish.set()


class TestRunInDaemonThread:
    @pytest.mark.asyncio
    async def test_uses_daemon_true(self):
        captured: dict = {}
        original_thread = threading.Thread

        def fake_thread(*args, **kwargs):
            captured.update(kwargs)
            return original_thread(*args, **kwargs)

        done = threading.Event()

        def fn() -> None:
            done.set()

        with patch(
            "event_saver.collectors.private_collector.threading.Thread",
            side_effect=fake_thread,
        ):
            fut = _run_in_daemon_thread(fn)
            await fut

        assert captured.get("daemon") is True
        assert done.is_set()

    @pytest.mark.asyncio
    async def test_completion_after_cancel_does_not_raise(self):
        loop = asyncio.get_running_loop()
        exception_records: list[dict] = []
        loop.set_exception_handler(lambda _loop, ctx: exception_records.append(ctx))

        gate = threading.Event()

        def fn() -> None:
            assert gate.wait(timeout=2.0)

        fut = _run_in_daemon_thread(fn)
        fut.cancel()
        # Wait for cancellation to propagate.
        await asyncio.sleep(0.01)

        try:
            gate.set()
            # Yield so the daemon thread's call_soon_threadsafe completer runs.
            await asyncio.sleep(0.05)

            assert exception_records == []
        finally:
            loop.set_exception_handler(None)

    def test_completion_after_loop_closed_does_not_raise(self):
        loop = asyncio.new_event_loop()
        gate = threading.Event()
        threads_before = {t.ident for t in threading.enumerate()}
        try:
            asyncio.set_event_loop(loop)

            def fn() -> None:
                assert gate.wait(timeout=2.0)

            async def spawn():
                return _run_in_daemon_thread(fn)

            loop.run_until_complete(spawn())
        finally:
            loop.close()
            asyncio.set_event_loop(None)

        # Loop is now closed; daemon thread is still parked on the gate.
        worker = next(
            (t for t in threading.enumerate() if t.ident not in threads_before),
            None,
        )
        assert worker is not None
        assert worker.daemon is True

        gate.set()
        worker.join(timeout=2.0)
        assert worker.is_alive() is False


# ---------------------------------------------------------------------------
# Readiness, silent reconnects and gap start (feature 0110 B1b)
# ---------------------------------------------------------------------------

_LAST_HEALTHY = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def _mark_ready(collector, ws):
    """Put a collector in the confirmed-ready state on ``ws``, so a health
    probe gets past the readiness check to the socket checks."""
    baseline = object()
    ws.is_authenticated.return_value = True
    ws.socket_identity.return_value = baseline
    collector._identity_baseline = baseline
    collector._ready = True


def _probe_collector(context, on_gap, *, alive=True, authed=True, ready=True):
    """Collector mid-run with a healthy baseline; knobs make it unhealthy."""
    collector = PrivateCollector(context=context, on_gap_detected=on_gap)
    ws = MagicMock()
    baseline = object()
    ws.is_socket_alive.return_value = alive
    ws.is_authenticated.return_value = authed
    ws.socket_identity.return_value = baseline
    ws.wait_ready.return_value = ready
    collector._running = True
    collector._ws_client = ws
    collector._identity_baseline = baseline
    collector._ready = True
    collector._last_healthy_ts = _LAST_HEALTHY
    return collector, ws


def _swap_identity_on_reset(ws):
    """A real reset() builds a new pybit socket: give it a new identity."""
    ws.reset.side_effect = lambda: setattr(
        ws.socket_identity, "return_value", object()
    )


class TestPrivateReadinessAndGapStart:
    @pytest.mark.asyncio
    async def test_start_confirms_ready_with_ack_tracking(self, collector):
        """start() builds a tracking client and waits for readiness."""
        with patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient"
        ) as MockWS:
            ws = MockWS.return_value
            ws.wait_ready.return_value = True
            await collector.start()
            try:
                assert MockWS.call_args.kwargs["track_subscription_acks"] is True
                ws.wait_ready.assert_called_once_with(_PRIVATE_READY_TIMEOUT)
                assert collector._ready is True
                assert collector._identity_baseline is ws.socket_identity()
                assert collector._last_healthy_ts is not None
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_start_ready_timeout_logs_and_does_not_raise(
        self, collector, caplog
    ):
        """Not ready at start: ERROR, keep running, first probe resets."""
        with patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient"
        ) as MockWS:
            MockWS.return_value.wait_ready.return_value = False
            with caplog.at_level(
                logging.ERROR, logger="event_saver.collectors.private_collector"
            ):
                await collector.start()
            try:
                assert collector.is_running() is True
                assert collector._ready is False
                assert "not ready" in caplog.text
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_quiet_healthy_probe_advances_last_healthy(self, context, on_gap):
        """A healthy socket with no messages is healthy; no gap, no reset."""
        collector, ws = _probe_collector(context, on_gap)
        await collector._ws_health_check_once()
        ws.reset.assert_not_called()
        on_gap.assert_not_called()
        ws.get_connection_state.assert_not_called()  # message age unused
        assert collector._last_healthy_ts > _LAST_HEALTHY

    @pytest.mark.asyncio
    async def test_silent_pybit_reconnect_is_a_gap(self, context, on_gap):
        """Alive socket but a new pybit WebSocketApp: reset and report gap."""
        collector, ws = _probe_collector(context, on_gap)
        ws.socket_identity.return_value = object()  # pybit reconnected
        await collector._ws_health_check_once()
        ws.reset.assert_called_once()
        on_gap.assert_called_once()
        assert on_gap.call_args[0][0] == _LAST_HEALTHY - _LIVENESS_MARGIN

    @pytest.mark.asyncio
    async def test_unauthenticated_socket_is_unhealthy(self, context, on_gap):
        """Alive but not authenticated is treated like a dead socket."""
        collector, ws = _probe_collector(context, on_gap, authed=False)
        await collector._ws_health_check_once()
        ws.reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_never_ready_socket_is_unhealthy(self, context, on_gap):
        """A socket not confirmed ready at start is reset by the probe."""
        collector, ws = _probe_collector(context, on_gap)
        collector._ready = False
        await collector._ws_health_check_once()
        ws.reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_gap_start_is_last_healthy_minus_margin(self, context, on_gap):
        """Gap start comes from the last healthy probe, not message age."""
        collector, ws = _probe_collector(context, on_gap, alive=False)
        ws.get_connection_state.return_value = ConnectionState(
            last_message_ts=datetime(2026, 8, 1, tzinfo=UTC), is_connected=True
        )
        _swap_identity_on_reset(ws)
        await collector._ws_health_check_once()
        start, end = on_gap.call_args[0]
        assert start == _LAST_HEALTHY - _LIVENESS_MARGIN
        assert end > start
        # the new socket becomes the baseline; next probe is healthy
        assert collector._identity_baseline is ws.socket_identity()

    @pytest.mark.asyncio
    async def test_ready_failure_after_reset_keeps_gap_open(
        self, context, on_gap
    ):
        """Reset but not ready: no gap reported yet; the next probe retries
        and reports the gap from the ORIGINAL last-healthy time."""
        collector, ws = _probe_collector(context, on_gap, alive=False)
        _swap_identity_on_reset(ws)
        ws.wait_ready.return_value = False
        await collector._ws_health_check_once()
        on_gap.assert_not_called()
        assert collector._last_healthy_ts == _LAST_HEALTHY
        assert collector._ready is False

        ws.is_socket_alive.return_value = True
        ws.wait_ready.return_value = True
        await collector._ws_health_check_once()
        assert ws.reset.call_count == 2
        on_gap.assert_called_once()
        assert on_gap.call_args[0][0] == _LAST_HEALTHY - _LIVENESS_MARGIN
        assert collector._ready is True

    @pytest.mark.asyncio
    async def test_identity_baseline_is_read_before_waiting(self, context, on_gap):
        """A pybit reconnect during wait_ready must not become the baseline:
        the next probe sees the swap and reports a gap."""
        collector, ws = _probe_collector(context, on_gap)
        before = ws.socket_identity.return_value
        collector._ready = False

        def _reconnect_during_wait(timeout):
            ws.socket_identity.return_value = object()
            return True

        ws.wait_ready.side_effect = _reconnect_during_wait
        assert await collector._confirm_ready() is True
        assert collector._identity_baseline is before

        ws.wait_ready.side_effect = None
        _swap_identity_on_reset(ws)
        await collector._ws_health_check_once()
        ws.reset.assert_called_once()
        on_gap.assert_called_once()

    @pytest.mark.asyncio
    async def test_not_ready_at_start_gap_dates_from_connect(self, collector, on_gap):
        """Not ready at start: the first probe resets and dates the gap from
        the connect time minus the liveness margin."""
        with patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient"
        ) as MockWS:
            ws = MockWS.return_value
            ws.wait_ready.return_value = False
            await collector.start()
            try:
                connected_at = collector._last_healthy_ts
                ws.wait_ready.return_value = True
                _swap_identity_on_reset(ws)
                await collector._ws_health_check_once()
                ws.reset.assert_called_once()
                on_gap.assert_called_once()
                assert on_gap.call_args[0][0] == connected_at - _LIVENESS_MARGIN
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_auth_wait_keeps_shutdown_responsive(
        self, context, on_gap, caplog
    ):
        """stop() during a post-reset readiness wait returns at once: the
        wait runs off the loop and is abandoned, and no gap is reported."""
        collector, ws = _probe_collector(context, on_gap, alive=False)
        _swap_identity_on_reset(ws)
        gate = threading.Event()
        ws.wait_ready.side_effect = lambda timeout: gate.wait(timeout)
        collector._ws_health_stop_event = asyncio.Event()
        collector._ws_health_task = asyncio.create_task(
            collector._ws_health_check_once()
        )
        try:
            await asyncio.sleep(0.1)  # probe is now parked in wait_ready
            started = time.monotonic()
            await collector.stop()
            # stop() swallows CancelledError, so time it instead of wait_for.
            assert time.monotonic() - started < 1.0
            on_gap.assert_not_called()
            assert "not ready after reset" not in caplog.text
            assert collector._ready is False
        finally:
            gate.set()
