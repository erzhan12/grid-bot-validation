"""Tests for PublicCollector."""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

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
