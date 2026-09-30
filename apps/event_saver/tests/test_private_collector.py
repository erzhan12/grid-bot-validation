"""Tests for PrivateCollector."""

import asyncio
import logging
import re
import threading
import time
import pytest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from bybit_adapter.ws_client import ConnectionState, PrivateWebSocketClient
from event_saver.collectors.private_collector import (
    PrivateCollector,
    AccountContext,
    LIVENESS_MARGIN,
    _MAX_UNREADY_RESETS,
    _PRIVATE_READY_TIMEOUT,
    _PRIVATE_START_TIMEOUT,
    CollectorStartError,
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
        # The mocked pybit socket never acks, so readiness is stubbed (0110
        # B1c-2: start() raises when the socket is not ready).
        with patch("bybit_adapter.ws_client.WebSocket") as MockWebSocket, patch.object(
            PrivateWebSocketClient, "wait_ready", return_value=True
        ):
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
        collector._last_healthy_ts = disconnected_at + LIVENESS_MARGIN

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
            "event_saver.collectors._startup.threading.Thread",
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


def _probe_collector(
    context, on_gap, *, alive=True, authed=True, ready=True, **hooks
):
    """Collector mid-run with a healthy baseline; knobs make it unhealthy."""
    collector = PrivateCollector(context=context, on_gap_detected=on_gap, **hooks)
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
        """start() connects a tracking client off the loop, then waits for
        readiness within what is left of the start bound."""
        with patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient"
        ) as MockWS:
            ws = MockWS.return_value
            order = []
            ws.connect.side_effect = lambda: order.append(
                ("connect", threading.current_thread() is threading.main_thread())
            )
            ws.wait_ready.side_effect = lambda timeout: (
                order.append(("wait_ready", timeout)) or True
            )
            await collector.start()
            try:
                assert MockWS.call_args.kwargs["track_subscription_acks"] is True
                assert order[0] == ("connect", False)  # off the event loop
                assert order[1][0] == "wait_ready"
                # what is LEFT of the bound after connect(), not the bound
                assert 0 < order[1][1] < _PRIVATE_START_TIMEOUT
                assert collector._ready is True
                assert collector._identity_baseline is ws.socket_identity()
                assert collector._last_healthy_ts is not None
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_start_not_ready_disconnects_and_raises(self, collector):
        """Not ready within the start bound: disconnect and raise; the
        collector is not running and has no health task."""
        with patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient"
        ) as MockWS:
            ws = MockWS.return_value
            ws.wait_ready.return_value = False
            with pytest.raises(CollectorStartError, match="not ready"):
                await collector.start()
            ws.disconnect.assert_called_once()
            assert collector.is_running() is False
            assert collector._ws_client is None
            assert collector._ws_health_task is None

            # a second start() on the same collector works
            ws.wait_ready.return_value = True
            await collector.start()
            try:
                assert collector.is_running() is True
                assert collector._ready is True
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_worker_done_but_owner_timed_out_closes_the_socket(
        self, collector
    ):
        """The worker can finish (socket live and ready) just as the owner
        times out and drops the client: the owner must then close the socket
        itself, or it stays live with nobody owning it."""
        import event_saver.collectors.private_collector as module

        real = module._run_in_daemon_thread

        def _lost_result(fn, *, name=None):
            if name != "ws-start":
                return real(fn, name=name)
            fn()  # the worker completes ...
            return asyncio.get_running_loop().create_future()  # ... unseen

        with patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient"
        ) as MockWS, patch.object(
            module, "_run_in_daemon_thread", _lost_result
        ), patch.object(module, "_PRIVATE_START_TIMEOUT", 0.05), patch.object(
            module, "_READY_WAIT_SLACK", 0.05
        ):
            ws = MockWS.return_value
            with pytest.raises(CollectorStartError, match="timed out"):
                await collector.start()
            deadline = time.monotonic() + 2.0
            while not ws.disconnect.called and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            ws.disconnect.assert_called_once()
            assert collector.is_running() is False

    @pytest.mark.asyncio
    async def test_worker_failed_but_owner_timed_out_closes_the_socket(
        self, collector
    ):
        """The worker can RAISE (socket possibly half open) just as the
        owner times out and never sees the error: the owner must then close
        the socket itself."""
        import event_saver.collectors.private_collector as module

        real = module._run_in_daemon_thread

        def _lost_error(fn, *, name=None):
            if name != "ws-start":
                return real(fn, name=name)
            with pytest.raises(AttributeError):
                fn()  # the worker fails ...
            return asyncio.get_running_loop().create_future()  # ... unseen

        with patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient"
        ) as MockWS, patch.object(
            module, "_run_in_daemon_thread", _lost_error
        ), patch.object(module, "_PRIVATE_START_TIMEOUT", 0.05), patch.object(
            module, "_READY_WAIT_SLACK", 0.05
        ):
            ws = MockWS.return_value
            ws.wait_ready.side_effect = AttributeError("subscriptions")
            with pytest.raises(CollectorStartError, match="timed out"):
                await collector.start()
            deadline = time.monotonic() + 2.0
            while not ws.disconnect.called and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            ws.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_during_the_cleanup_disconnect_leaves_it_stopped(
        self, context
    ):
        """A cancellation that lands while the failed start is disconnecting
        must still leave the collector stopped (a later start() must work)."""
        gate = threading.Event()
        collector = PrivateCollector(context=context, ws_disconnect_timeout=5)
        try:
            with patch(
                "event_saver.collectors.private_collector.PrivateWebSocketClient"
            ) as MockWS:
                ws = MockWS.return_value
                ws.wait_ready.return_value = False
                ws.disconnect.side_effect = lambda: gate.wait(5)
                task = asyncio.create_task(collector.start())
                await asyncio.sleep(0.1)  # parked in the cleanup disconnect
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert collector.is_running() is False
                assert collector._ws_client is None
        finally:
            gate.set()

    @pytest.mark.asyncio
    async def test_abandoned_worker_that_then_fails_still_closes_the_socket(
        self, collector
    ):
        """After the owner gave up, a worker whose readiness check raises
        has nobody to report to: it must close the socket itself."""
        gate = threading.Event()

        def _late_failure(timeout):
            gate.wait(5)
            raise AttributeError("subscriptions")

        try:
            with patch(
                "event_saver.collectors.private_collector.PrivateWebSocketClient"
            ) as MockWS, patch(
                "event_saver.collectors.private_collector._PRIVATE_START_TIMEOUT",
                0.05,
            ), patch(
                "event_saver.collectors.private_collector._READY_WAIT_SLACK", 0.05
            ):
                ws = MockWS.return_value
                ws.wait_ready.side_effect = _late_failure
                with pytest.raises(CollectorStartError, match="timed out"):
                    await collector.start()
                ws.disconnect.assert_not_called()
                gate.set()
                deadline = time.monotonic() + 2.0
                while not ws.disconnect.called and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                ws.disconnect.assert_called_once()
        finally:
            gate.set()

    @pytest.mark.asyncio
    async def test_cancelled_start_abandons_the_connect_worker(self, collector):
        """A start cancelled while connect() is parked (a shutdown signal)
        leaves the collector stopped without the stuck client; the worker
        closes the socket if it ever connects."""
        gate = threading.Event()
        try:
            with patch(
                "event_saver.collectors.private_collector.PrivateWebSocketClient"
            ) as MockWS:
                ws = MockWS.return_value
                ws.connect.side_effect = lambda: gate.wait(5)
                task = asyncio.create_task(collector.start())
                await asyncio.sleep(0.05)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert collector.is_running() is False
                assert collector._ws_client is None
                ws.disconnect.assert_not_called()

                gate.set()
                deadline = time.monotonic() + 2.0
                while not ws.disconnect.called and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                ws.disconnect.assert_called_once()
                ws.wait_ready.assert_not_called()
        finally:
            gate.set()

    @pytest.mark.asyncio
    async def test_start_baseline_is_read_before_the_readiness_wait(
        self, collector
    ):
        """A pybit reconnect during the start readiness wait must not become
        the baseline: the first probe then sees the swap."""
        with patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient"
        ) as MockWS:
            ws = MockWS.return_value
            before = ws.socket_identity.return_value

            def _reconnect_during_wait(timeout):
                ws.socket_identity.return_value = object()
                return True

            ws.wait_ready.side_effect = _reconnect_during_wait
            await collector.start()
            try:
                assert collector._identity_baseline is before
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_start_connect_error_raises_collector_start_error(
        self, collector
    ):
        """connect() raising at start: disconnect (bounded) and raise."""
        with patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient"
        ) as MockWS:
            ws = MockWS.return_value
            ws.connect.side_effect = RuntimeError("dns failure")
            with pytest.raises(CollectorStartError) as excinfo:
                await collector.start()
            assert isinstance(excinfo.value.__cause__, RuntimeError)
            ws.wait_ready.assert_not_called()
            ws.disconnect.assert_called_once()
            assert collector.is_running() is False

    @pytest.mark.asyncio
    async def test_start_failure_with_hung_disconnect_still_raises_start_error(
        self, context
    ):
        """The disconnect after a failed start is bounded: if it hangs, the
        owner still gets CollectorStartError (not a TimeoutError that would
        skip the recorder's sentinel) and the collector is left stopped."""
        gate = threading.Event()
        collector = PrivateCollector(context=context, ws_disconnect_timeout=0.05)
        try:
            with patch(
                "event_saver.collectors.private_collector.PrivateWebSocketClient"
            ) as MockWS:
                ws = MockWS.return_value
                ws.wait_ready.return_value = False
                ws.disconnect.side_effect = lambda: gate.wait(5)
                with pytest.raises(CollectorStartError, match="not ready"):
                    await collector.start()
                assert collector.is_running() is False
                assert collector._ws_client is None
        finally:
            gate.set()

    @pytest.mark.asyncio
    async def test_abandoned_start_disconnects_even_after_the_readiness_wait(
        self, collector
    ):
        """start() can give up while the worker is already past connect()
        and inside the readiness wait: the worker must still close the
        socket when that wait returns."""
        gate = threading.Event()
        try:
            with patch(
                "event_saver.collectors.private_collector.PrivateWebSocketClient"
            ) as MockWS, patch(
                "event_saver.collectors.private_collector._PRIVATE_START_TIMEOUT",
                0.05,
            ), patch(
                "event_saver.collectors.private_collector._READY_WAIT_SLACK", 0.05
            ):
                ws = MockWS.return_value
                ws.wait_ready.side_effect = lambda timeout: gate.wait(5) or True
                with pytest.raises(CollectorStartError, match="timed out"):
                    await collector.start()
                ws.disconnect.assert_not_called()
                gate.set()
                deadline = time.monotonic() + 2.0
                while not ws.disconnect.called and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                ws.disconnect.assert_called_once()
        finally:
            gate.set()

    @pytest.mark.asyncio
    async def test_start_connect_timeout_abandons_and_raises(self, collector):
        """A connect() that never returns is abandoned at the start bound:
        no disconnect (pybit still holds the client lock), and it raises."""
        gate = threading.Event()
        try:
            with patch(
                "event_saver.collectors.private_collector.PrivateWebSocketClient"
            ) as MockWS, patch(
                "event_saver.collectors.private_collector._PRIVATE_START_TIMEOUT",
                0.05,
            ), patch(
                "event_saver.collectors.private_collector._READY_WAIT_SLACK", 0.05
            ):
                ws = MockWS.return_value
                ws.connect.side_effect = lambda: gate.wait(5)
                started = time.monotonic()
                with pytest.raises(CollectorStartError, match="timed out"):
                    await collector.start()
                assert time.monotonic() - started < 1.0
                ws.disconnect.assert_not_called()
                assert collector.is_running() is False

                # pybit retries forever, so the abandoned connect() can still
                # succeed later: that socket must be closed, not left feeding
                # a collector that is not running.
                gate.set()
                deadline = time.monotonic() + 2.0
                while not ws.disconnect.called and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                ws.disconnect.assert_called_once()
                ws.wait_ready.assert_not_called()
        finally:
            gate.set()
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
        assert on_gap.call_args[0][0] == _LAST_HEALTHY - LIVENESS_MARGIN

    @pytest.mark.asyncio
    async def test_unauthenticated_socket_is_unhealthy(self, context, on_gap):
        """Alive but not authenticated is treated like a dead socket."""
        collector, ws = _probe_collector(context, on_gap, authed=False)
        await collector._ws_health_check_once()
        ws.reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_never_ready_socket_is_unhealthy(self, context, on_gap):
        """A socket that is still not ready when re-checked is reset."""
        collector, ws = _probe_collector(context, on_gap, ready=False)
        collector._ready = False
        await collector._ws_health_check_once()
        ws.reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_degrades_to_liveness_after_repeated_unready_resets(
        self, context, on_gap, caplog
    ):
        """A socket that never becomes ready (e.g. one rejected topic) is
        reset at most _MAX_UNREADY_RESETS times, then kept on liveness-only
        health with the gap reported so REST backfill runs."""
        collector, ws = _probe_collector(context, on_gap, alive=False)
        _swap_identity_on_reset(ws)
        ws.wait_ready.return_value = False
        with caplog.at_level(
            logging.ERROR, logger="event_saver.collectors.private_collector"
        ):
            for _ in range(_MAX_UNREADY_RESETS):
                await collector._ws_health_check_once()
                ws.is_socket_alive.return_value = True
        assert ws.reset.call_count == _MAX_UNREADY_RESETS
        on_gap.assert_called_once()
        assert on_gap.call_args[0][0] == _LAST_HEALTHY - LIVENESS_MARGIN
        assert "liveness checks only" in caplog.text
        assert collector._ready is True

        await collector._ws_health_check_once()  # now healthy: no reset
        assert ws.reset.call_count == _MAX_UNREADY_RESETS

    @pytest.mark.asyncio
    async def test_late_ready_socket_is_not_reset(self, context, on_gap):
        """Acks that land after the ready timeout make the SAME socket ready
        on the next probe: no reset, and the unready stretch is a gap."""
        collector, ws = _probe_collector(context, on_gap)
        collector._ready = False
        await collector._ws_health_check_once()
        ws.reset.assert_not_called()
        on_gap.assert_called_once()
        assert on_gap.call_args[0][0] == _LAST_HEALTHY - LIVENESS_MARGIN
        assert collector._ready is True

    @pytest.mark.asyncio
    async def test_start_readiness_error_raises_collector_start_error(
        self, collector
    ):
        """wait_ready raising (e.g. a pybit internals change) at start is a
        start failure like any other: disconnect and CollectorStartError."""
        with patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient"
        ) as MockWS:
            ws = MockWS.return_value
            ws.wait_ready.side_effect = AttributeError("subscriptions")
            with pytest.raises(CollectorStartError) as excinfo:
                await collector.start()
            assert isinstance(excinfo.value.__cause__, AttributeError)
            ws.disconnect.assert_called_once()
            assert collector.is_running() is False
    @pytest.mark.asyncio
    async def test_gap_start_is_last_healthy_minus_margin(self, context, on_gap):
        """Gap start comes from the last healthy probe, not message age."""
        collector, ws = _probe_collector(context, on_gap, alive=False)
        ws.get_connection_state.return_value = ConnectionState(
            last_message_ts=datetime(2026, 8, 1, tzinfo=UTC), is_connected=True
        )
        _swap_identity_on_reset(ws)
        await collector._ws_health_check_once()
        # the post-reset wait keeps its own, shorter bound
        ws.wait_ready.assert_called_once_with(_PRIVATE_READY_TIMEOUT)
        start, end = on_gap.call_args[0]
        assert start == _LAST_HEALTHY - LIVENESS_MARGIN
        assert end > start
        # the new socket becomes the baseline; next probe is healthy
        assert collector._identity_baseline is ws.socket_identity()

    @pytest.mark.asyncio
    async def test_ready_failure_after_reset_keeps_gap_open(
        self, context, on_gap, caplog
    ):
        """Reset but not ready: no gap reported yet; the next probe finds the
        new socket ready and reports the gap from the ORIGINAL start."""
        collector, ws = _probe_collector(context, on_gap, alive=False)
        _swap_identity_on_reset(ws)
        ws.wait_ready.return_value = False
        with caplog.at_level(
            logging.ERROR, logger="event_saver.collectors.private_collector"
        ):
            await collector._ws_health_check_once()
        assert "unready for" in caplog.text
        on_gap.assert_not_called()
        assert collector._last_healthy_ts == _LAST_HEALTHY
        assert collector._ready is False

        ws.is_socket_alive.return_value = True
        ws.wait_ready.return_value = True
        await collector._ws_health_check_once()
        assert ws.reset.call_count == 1  # late-ready: the new socket is kept
        on_gap.assert_called_once()
        assert on_gap.call_args[0][0] == _LAST_HEALTHY - LIVENESS_MARGIN
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
        assert await collector._confirm_ready(ws) is True
        assert collector._identity_baseline is before

        ws.wait_ready.side_effect = None
        _swap_identity_on_reset(ws)
        await collector._ws_health_check_once()
        ws.reset.assert_called_once()
        on_gap.assert_called_once()

    @pytest.mark.asyncio
    async def test_gap_never_starts_before_connect(self, context, on_gap):
        """A gap computed soon after connect starts at the connect time, not
        75 s before it (no pre-run executions under this run)."""
        collector, ws = _probe_collector(context, on_gap)
        collector._ready = False
        collector._connected_at = _LAST_HEALTHY
        await collector._ws_health_check_once()  # late-ready path
        ws.reset.assert_not_called()
        on_gap.assert_called_once()
        assert on_gap.call_args[0][0] == _LAST_HEALTHY
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

    @pytest.mark.asyncio
    async def test_unready_duration_is_not_negative_near_connect(
        self, context, on_gap, caplog
    ):
        """Still not ready after a reset soon after connect: the logged
        unready time counts from the last-healthy time (the connect-time
        clamp must not skew it)."""
        collector, ws = _probe_collector(context, on_gap, ready=False)
        collector._ready = False
        collector._connected_at = collector._last_healthy_ts = datetime.now(UTC)
        _swap_identity_on_reset(ws)
        with caplog.at_level(
            logging.ERROR, logger="event_saver.collectors.private_collector"
        ):
            await collector._ws_health_check_once()
        match = re.search(r"unready for (-?\d+)s", caplog.text)
        assert match is not None
        assert int(match.group(1)) >= 0

# ---------------------------------------------------------------------------
# Owner coverage hooks (feature 0110 B1c-1)
# ---------------------------------------------------------------------------


class TestPrivateCoverageHooks:
    @pytest.mark.asyncio
    async def test_gap_opened_before_reset(self, context, on_gap):
        """on_disconnect(gap_start) runs before reset(), then the gap closes."""
        calls = []
        on_disconnect = MagicMock(side_effect=lambda ts: calls.append(("open", ts)))
        collector, ws = _probe_collector(
            context, on_gap, alive=False, on_disconnect=on_disconnect
        )
        ws.reset.side_effect = lambda: (
            calls.append(("reset", None)),
            setattr(ws.socket_identity, "return_value", object()),
        )
        await collector._ws_health_check_once()
        assert calls == [
            ("open", _LAST_HEALTHY - LIVENESS_MARGIN),
            ("reset", None),
        ]
        on_gap.assert_called_once()

    @pytest.mark.asyncio
    async def test_open_gap_failure_skips_reset(self, context, on_gap):
        """A failed gap-open write blocks this probe's reset; the next retries."""
        on_disconnect = MagicMock(side_effect=RuntimeError("db down"))
        collector, ws = _probe_collector(
            context, on_gap, alive=False, on_disconnect=on_disconnect
        )
        _swap_identity_on_reset(ws)
        await collector._ws_health_check_once()
        ws.reset.assert_not_called()
        on_gap.assert_not_called()

        on_disconnect.side_effect = None
        await collector._ws_health_check_once()
        ws.reset.assert_called_once()
        on_gap.assert_called_once()
        assert on_disconnect.call_args_list[1].args[0] == (
            _LAST_HEALTHY - LIVENESS_MARGIN
        )

    @pytest.mark.asyncio
    async def test_healthy_probe_awaits_owner_checkpoint(self, context, on_gap):
        """Each healthy probe awaits the owner's checkpoint callback."""
        on_healthy_probe = AsyncMock()
        collector, ws = _probe_collector(
            context, on_gap, on_healthy_probe=on_healthy_probe
        )
        await collector._ws_health_check_once()
        on_healthy_probe.assert_awaited_once()
        ws.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_checkpoint_error_is_logged_not_unhealthy(
        self, context, on_gap, caplog
    ):
        """A failing checkpoint callback is logged; the socket stays healthy."""
        collector, ws = _probe_collector(
            context,
            on_gap,
            on_healthy_probe=AsyncMock(side_effect=RuntimeError("flush failed")),
        )
        with caplog.at_level(
            logging.ERROR, logger="event_saver.collectors.private_collector"
        ):
            await collector._ws_health_check_once()
        assert "checkpoint failed" in caplog.text
        assert collector._last_healthy_ts > _LAST_HEALTHY
        ws.reset.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_checkpoint_while_liveness_only(self, context, on_gap):
        """A socket kept on liveness-only never advances the checkpoint, and
        is_degraded() says so until readiness is confirmed again."""
        on_healthy_probe = AsyncMock()
        collector, ws = _probe_collector(
            context, on_gap, alive=False, on_healthy_probe=on_healthy_probe
        )
        _swap_identity_on_reset(ws)
        ws.wait_ready.return_value = False
        for _ in range(_MAX_UNREADY_RESETS):
            await collector._ws_health_check_once()
            ws.is_socket_alive.return_value = True
        assert collector.is_degraded() is True

        await collector._ws_health_check_once()  # healthy on liveness only
        on_healthy_probe.assert_not_awaited()

        ws.wait_ready.return_value = True
        assert await collector._confirm_ready(ws) is True
        assert collector.is_degraded() is False
        await collector._ws_health_check_once()
        on_healthy_probe.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_liveness_only_period_is_not_certified_on_recovery(
        self, context, on_gap
    ):
        """Healthy probes while liveness-only do not move the last-healthy
        time, so a later outage's gap reaches back over the whole degraded
        stretch (a topic may never have been acked during it)."""
        collector, ws = _probe_collector(context, on_gap, alive=False)
        _swap_identity_on_reset(ws)
        ws.wait_ready.return_value = False
        for _ in range(_MAX_UNREADY_RESETS):
            await collector._ws_health_check_once()
            ws.is_socket_alive.return_value = True
        degraded_at = collector._last_healthy_ts

        await collector._ws_health_check_once()  # healthy, liveness only
        assert collector._last_healthy_ts == degraded_at

        ws.is_socket_alive.return_value = False  # later outage, then ready
        ws.wait_ready.return_value = True
        await collector._ws_health_check_once()
        assert on_gap.call_args[0][0] == degraded_at - LIVENESS_MARGIN
        assert collector.is_degraded() is False

    @pytest.mark.asyncio
    async def test_late_acks_clear_liveness_only_on_the_same_socket(
        self, context, on_gap
    ):
        """Acks that land while liveness-only end it on the next healthy
        probe: no reset, the degraded stretch is reported as a gap, and
        checkpoints resume."""
        on_healthy_probe = AsyncMock()
        collector, ws = _probe_collector(
            context, on_gap, alive=False, on_healthy_probe=on_healthy_probe
        )
        _swap_identity_on_reset(ws)
        ws.wait_ready.return_value = False
        for _ in range(_MAX_UNREADY_RESETS):
            await collector._ws_health_check_once()
            ws.is_socket_alive.return_value = True
        degraded_at = collector._last_healthy_ts
        resets = ws.reset.call_count
        gaps = on_gap.call_count

        ws.wait_ready.return_value = True  # acks landed
        await collector._ws_health_check_once()
        assert collector.is_degraded() is False
        assert ws.reset.call_count == resets
        assert on_gap.call_count == gaps + 1
        assert on_gap.call_args[0][0] == degraded_at - LIVENESS_MARGIN
        ws.wait_ready.assert_called_with(0)  # non-blocking re-check

        await collector._ws_health_check_once()
        on_healthy_probe.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_checkpoint_wait_keeps_shutdown_responsive(
        self, context, on_gap
    ):
        """stop() during a parked on_healthy_probe returns at once."""
        gate = asyncio.Event()

        async def _parked():
            await gate.wait()

        collector, ws = _probe_collector(context, on_gap, on_healthy_probe=_parked)
        collector._ws_health_stop_event = asyncio.Event()
        collector._ws_health_task = asyncio.create_task(
            collector._ws_health_check_once()
        )
        try:
            await asyncio.sleep(0.1)  # probe is now parked in the checkpoint
            started = time.monotonic()
            await collector.stop()
            # stop() swallows CancelledError, so time it instead of wait_for.
            assert time.monotonic() - started < 1.0
        finally:
            gate.set()

    @pytest.mark.asyncio
    async def test_checkpoint_timeout_is_logged_and_probe_continues(
        self, context, on_gap, caplog
    ):
        """A checkpoint slower than its bound is abandoned with a WARNING;
        the socket stays healthy."""
        gate = asyncio.Event()

        async def _parked():
            await gate.wait()

        collector, ws = _probe_collector(context, on_gap, on_healthy_probe=_parked)
        try:
            with patch(
                "event_saver.collectors.private_collector._HEALTHY_PROBE_TIMEOUT",
                0.05,
            ), caplog.at_level(
                logging.WARNING, logger="event_saver.collectors.private_collector"
            ):
                await collector._ws_health_check_once()
            assert "checkpoint timed out" in caplog.text
            ws.reset.assert_not_called()
        finally:
            gate.set()

    @pytest.mark.asyncio
    async def test_start_clears_liveness_only(self, collector):
        """A collector restarted while degraded starts not degraded."""
        collector._liveness_only = True
        with patch(
            "event_saver.collectors.private_collector.PrivateWebSocketClient"
        ):
            await collector.start()
            try:
                assert collector.is_degraded() is False
            finally:
                await collector.stop()
