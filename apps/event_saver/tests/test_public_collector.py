"""Tests for PublicCollector."""

import asyncio
import threading
import time

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from event_saver.collectors import CollectorStartError
from event_saver.collectors.public_collector import PublicCollector
from gridcore.events import TickerEvent, PublicTradeEvent


@pytest.fixture
def on_ticker():
    return MagicMock()


@pytest.fixture
def on_trades():
    return MagicMock()


@pytest.fixture
def on_gap():
    return MagicMock()


@pytest.fixture
def collector(on_ticker, on_trades, on_gap):
    return PublicCollector(
        symbols=["BTCUSDT", "ETHUSDT"],
        on_ticker=on_ticker,
        on_trades=on_trades,
        on_gap_detected=on_gap,
        testnet=True,
    )


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


class TestInit:
    def test_stores_symbols(self, collector):
        assert collector.symbols == ["BTCUSDT", "ETHUSDT"]

    def test_stores_callbacks(self, collector, on_ticker, on_trades, on_gap):
        assert collector._on_ticker is on_ticker
        assert collector._on_trades is on_trades
        assert collector._on_gap_detected is on_gap

    def test_not_running_initially(self, collector):
        assert collector.is_running() is False

    def test_no_ws_client_initially(self, collector):
        assert collector._ws_client is None


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_ws_and_connects(self, collector):
        with patch("event_saver.collectors.public_collector.PublicWebSocketClient") as MockWS:
            mock_ws = MagicMock()
            MockWS.return_value = mock_ws

            await collector.start()

            assert collector.is_running() is True
            MockWS.assert_called_once()
            mock_ws.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_twice_warns(self, collector):
        with patch("event_saver.collectors.public_collector.PublicWebSocketClient") as MockWS:
            MockWS.return_value = MagicMock()
            await collector.start()

            MockWS.reset_mock()
            await collector.start()

            MockWS.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_disconnects_ws(self, collector):
        with patch("event_saver.collectors.public_collector.PublicWebSocketClient") as MockWS:
            mock_ws = MagicMock()
            MockWS.return_value = mock_ws

            await collector.start()
            await collector.stop()

            assert collector.is_running() is False
            mock_ws.disconnect.assert_called_once()
            assert collector._ws_client is None

    @pytest.mark.asyncio
    async def test_stop_noop_if_not_running(self, collector):
        await collector.stop()  # No error

    def test_get_connection_state_none_when_no_client(self, collector):
        assert collector.get_connection_state() is None

    @pytest.mark.asyncio
    async def test_get_connection_state_delegates_to_ws(self, collector):
        with patch("event_saver.collectors.public_collector.PublicWebSocketClient") as MockWS:
            mock_ws = MagicMock()
            mock_ws.get_connection_state.return_value = "connected"
            MockWS.return_value = mock_ws

            await collector.start()

            assert collector.get_connection_state() == "connected"


# ---------------------------------------------------------------------------
# _handle_ticker
# ---------------------------------------------------------------------------


class TestHandleTicker:
    def test_normalizes_and_forwards(self, collector, on_ticker):
        ticker_msg = {
            "topic": "tickers.BTCUSDT",
            "type": "snapshot",
            "ts": 1704639600000,
            "data": {
                "symbol": "BTCUSDT",
                "lastPrice": "42500.50",
                "markPrice": "42501.00",
                "bid1Price": "42500.00",
                "ask1Price": "42501.00",
                "fundingRate": "0.0001",
            },
        }

        collector._handle_ticker(ticker_msg)

        on_ticker.assert_called_once()
        event = on_ticker.call_args[0][0]
        assert isinstance(event, TickerEvent)
        assert event.symbol == "BTCUSDT"

    def test_handles_normalization_error(self, collector, on_ticker):
        # Force normalizer to raise by patching it
        collector._normalizer.normalize_ticker = MagicMock(side_effect=Exception("bad data"))
        collector._handle_ticker({"invalid": "data"})

        on_ticker.assert_not_called()

    def test_noop_without_callback(self):
        col = PublicCollector(symbols=["BTCUSDT"], on_ticker=None)
        col._handle_ticker({"topic": "tickers.BTCUSDT", "data": {}})
        # No error


# ---------------------------------------------------------------------------
# _handle_trade
# ---------------------------------------------------------------------------


class TestHandleTrade:
    def test_normalizes_and_forwards(self, collector, on_trades):
        trade_msg = {
            "topic": "publicTrade.BTCUSDT",
            "type": "snapshot",
            "ts": 1704639600000,
            "data": [
                {
                    "i": "trade-1",
                    "T": 1704639600000,
                    "p": "42500.50",
                    "v": "0.1",
                    "S": "Buy",
                    "s": "BTCUSDT",
                    "L": "PlusTick",
                    "BT": False,
                },
            ],
        }

        collector._handle_trade(trade_msg)

        on_trades.assert_called_once()
        events = on_trades.call_args[0][0]
        assert len(events) == 1
        assert isinstance(events[0], PublicTradeEvent)

    def test_tracks_last_trade_ts(self, collector, on_trades):
        trade_msg = {
            "topic": "publicTrade.BTCUSDT",
            "type": "snapshot",
            "ts": 1704639600000,
            "data": [
                {
                    "i": "trade-1",
                    "T": 1704639600000,
                    "p": "42500.50",
                    "v": "0.1",
                    "S": "Buy",
                    "s": "BTCUSDT",
                    "L": "PlusTick",
                    "BT": False,
                },
            ],
        }

        collector._handle_trade(trade_msg)

        assert "BTCUSDT" in collector._last_trade_ts

    def test_handles_normalization_error(self, collector, on_trades):
        collector._handle_trade({"invalid": "data"})

        on_trades.assert_not_called()


# ---------------------------------------------------------------------------
# Disconnect / Reconnect
# ---------------------------------------------------------------------------


class TestDisconnectReconnect:
    def test_handle_disconnect_logs(self, collector, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            collector._handle_disconnect(datetime(2025, 1, 1))

        assert "disconnected" in caplog.text

    def test_handle_reconnect_triggers_gap_for_all_symbols(self, collector, on_gap):
        d1 = datetime(2025, 1, 1, 0, 0, 0)
        d2 = datetime(2025, 1, 1, 0, 0, 10)

        collector._handle_reconnect(d1, d2)

        assert on_gap.call_count == 2  # BTCUSDT + ETHUSDT
        on_gap.assert_any_call("BTCUSDT", d1, d2)
        on_gap.assert_any_call("ETHUSDT", d1, d2)

    def test_handle_reconnect_noop_without_callback(self):
        col = PublicCollector(symbols=["BTCUSDT"], on_gap_detected=None)
        col._handle_reconnect(datetime(2025, 1, 1), datetime(2025, 1, 1))
        # No error


class TestPublicStartBound:
    @pytest.mark.asyncio
    async def test_connect_runs_off_the_event_loop(self, collector):
        """connect() is a blocking pybit call: it runs on a worker thread."""
        with patch(
            "event_saver.collectors.public_collector.PublicWebSocketClient"
        ) as MockWS:
            seen = []
            MockWS.return_value.connect.side_effect = lambda: seen.append(
                threading.current_thread() is threading.main_thread()
            )
            await collector.start()
            try:
                assert seen == [False]
                assert collector.is_running() is True
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_connect_timeout_raises(self, collector):
        """A connect() that never returns is abandoned at the bound and
        start() raises instead of hanging the owner's startup."""
        gate = threading.Event()
        try:
            with patch(
                "event_saver.collectors.public_collector.PublicWebSocketClient"
            ) as MockWS, patch(
                "event_saver.collectors.public_collector._PUBLIC_CONNECT_TIMEOUT",
                0.05,
            ):
                MockWS.return_value.connect.side_effect = lambda: gate.wait(5)
                started = time.monotonic()
                ws = MockWS.return_value
                with pytest.raises(CollectorStartError, match="timed out"):
                    await collector.start()
                assert time.monotonic() - started < 1.0
                assert collector.is_running() is False
                assert collector._ws_client is None
                ws.disconnect.assert_not_called()

                # A connect that succeeds after the bound is closed by the
                # abandoned worker, not left as a live unowned socket.
                gate.set()
                deadline = time.monotonic() + 2.0
                while not ws.disconnect.called and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                ws.disconnect.assert_called_once()
        finally:
            gate.set()

    @pytest.mark.asyncio
    async def test_connect_error_raises_collector_start_error(self, collector):
        """connect() raising is a start failure like a timeout: the owner
        gets CollectorStartError and the collector is left not running."""
        with patch(
            "event_saver.collectors.public_collector.PublicWebSocketClient"
        ) as MockWS:
            MockWS.return_value.connect.side_effect = RuntimeError("dns failure")
            with pytest.raises(CollectorStartError) as excinfo:
                await collector.start()
            assert isinstance(excinfo.value.__cause__, RuntimeError)
            assert collector.is_running() is False
            assert collector._ws_client is None
            # whatever connect() opened before raising is closed
            MockWS.return_value.disconnect.assert_called_once()

            # a second start() on the same collector works
            MockWS.return_value.connect.side_effect = None
            await collector.start()
            try:
                assert collector.is_running() is True
            finally:
                await collector.stop()

    @pytest.mark.asyncio
    async def test_worker_done_but_owner_timed_out_closes_the_socket(
        self, collector
    ):
        """connect() can return just as the owner times out and drops the
        client: the owner must then close the socket itself."""
        import event_saver.collectors.public_collector as module

        real = module.run_in_daemon_thread

        def _lost_result(fn, *, name=None):
            if name != "public-ws-connect":
                return real(fn, name=name)
            fn()  # the worker completes ...
            return asyncio.get_running_loop().create_future()  # ... unseen

        with patch(
            "event_saver.collectors.public_collector.PublicWebSocketClient"
        ) as MockWS, patch.object(
            module, "run_in_daemon_thread", _lost_result
        ), patch.object(module, "_PUBLIC_CONNECT_TIMEOUT", 0.05):
            ws = MockWS.return_value
            with pytest.raises(CollectorStartError, match="timed out"):
                await collector.start()
            deadline = time.monotonic() + 2.0
            while not ws.disconnect.called and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            ws.disconnect.assert_called_once()
            assert collector.is_running() is False

    @pytest.mark.asyncio
    async def test_cancelled_start_leaves_a_stoppable_collector(self, collector):
        """A start cancelled while connect() is parked (a shutdown signal)
        must not leave the stuck client behind: stop() would block on the
        lock connect() holds. The worker closes the socket if it ever
        connects."""
        gate = threading.Event()
        try:
            with patch(
                "event_saver.collectors.public_collector.PublicWebSocketClient"
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
                await collector.stop()  # no-op, must not touch the client
                ws.disconnect.assert_not_called()

                gate.set()
                deadline = time.monotonic() + 2.0
                while not ws.disconnect.called and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                ws.disconnect.assert_called_once()
        finally:
            gate.set()

    @pytest.mark.asyncio
    async def test_cancel_during_the_cleanup_disconnect_leaves_it_stopped(
        self, collector
    ):
        """A cancellation that lands while a failed start is disconnecting
        must still leave the collector stopped, so stop() does not call
        disconnect() again on a client whose lock is held."""
        gate = threading.Event()
        try:
            with patch(
                "event_saver.collectors.public_collector.PublicWebSocketClient"
            ) as MockWS:
                ws = MockWS.return_value
                ws.connect.side_effect = RuntimeError("dns failure")
                ws.disconnect.side_effect = lambda: gate.wait(5)
                task = asyncio.create_task(collector.start())
                await asyncio.sleep(0.1)  # parked in the cleanup disconnect
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert collector.is_running() is False
                assert collector._ws_client is None
                await collector.stop()  # no-op
                assert ws.disconnect.call_count == 1
        finally:
            gate.set()

    @pytest.mark.asyncio
    async def test_abandoned_worker_that_then_fails_still_closes_the_socket(
        self, collector
    ):
        """After the owner gave up, a connect() that raises has nobody to
        report to: the worker must close whatever it opened."""
        gate = threading.Event()

        def _late_failure():
            gate.wait(5)
            raise RuntimeError("subscribe failed")

        try:
            with patch(
                "event_saver.collectors.public_collector.PublicWebSocketClient"
            ) as MockWS, patch(
                "event_saver.collectors.public_collector._PUBLIC_CONNECT_TIMEOUT",
                0.05,
            ):
                ws = MockWS.return_value
                ws.connect.side_effect = _late_failure
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
    async def test_worker_failed_but_owner_timed_out_closes_the_socket(
        self, collector
    ):
        """connect() can RAISE just as the owner times out and never sees
        the error: the owner must then close the socket itself."""
        import event_saver.collectors.public_collector as module

        real = module.run_in_daemon_thread

        def _lost_error(fn, *, name=None):
            if name != "public-ws-connect":
                return real(fn, name=name)
            with pytest.raises(RuntimeError):
                fn()  # the worker fails ...
            return asyncio.get_running_loop().create_future()  # ... unseen

        with patch(
            "event_saver.collectors.public_collector.PublicWebSocketClient"
        ) as MockWS, patch.object(
            module, "run_in_daemon_thread", _lost_error
        ), patch.object(module, "_PUBLIC_CONNECT_TIMEOUT", 0.05):
            ws = MockWS.return_value
            ws.connect.side_effect = RuntimeError("subscribe failed")
            with pytest.raises(CollectorStartError, match="timed out"):
                await collector.start()
            deadline = time.monotonic() + 2.0
            while not ws.disconnect.called and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            ws.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_error_from_connect_is_a_connect_failure(
        self, collector
    ):
        """A TimeoutError raised BY connect() (e.g. socket.timeout) is a
        connect failure with its cause, not the start bound."""
        with patch(
            "event_saver.collectors.public_collector.PublicWebSocketClient"
        ) as MockWS:
            MockWS.return_value.connect.side_effect = TimeoutError("handshake")
            with pytest.raises(CollectorStartError, match="failed") as excinfo:
                await collector.start()
            assert isinstance(excinfo.value.__cause__, TimeoutError)
            MockWS.return_value.disconnect.assert_called_once()
            assert collector.is_running() is False
