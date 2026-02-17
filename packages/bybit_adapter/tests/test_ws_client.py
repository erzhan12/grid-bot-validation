"""Tests for WebSocket client disconnect/reconnect detection."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import time

from bybit_adapter.ws_client import (
    PublicWebSocketClient,
    PrivateWebSocketClient,
    ConnectionState,
)


class TestConnectionState:
    """Test ConnectionState dataclass."""

    def test_default_values(self):
        """ConnectionState has correct defaults."""
        state = ConnectionState()
        assert state.connected_at is None
        assert state.disconnected_at is None
        assert state.last_message_ts is None
        assert state.reconnect_count == 0
        assert state.is_connected is False
        assert state._detected_disconnect is False


class TestPublicWebSocketClientDisconnectDetection:
    """Test disconnect detection via heartbeat watchdog."""

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_detects_disconnect_on_message_gap(self, mock_ws_class):
        """Detects disconnect when no messages received for threshold duration."""
        disconnect_callback = MagicMock()

        client = PublicWebSocketClient(
            symbols=["BTCUSDT"],
            testnet=True,
            on_trade=lambda x: None,
            on_disconnect=disconnect_callback,
            heartbeat_interval=0.1,  # Fast for testing
            disconnect_threshold=0.3,  # Short threshold for testing
        )

        client.connect()

        # Wait for disconnect to be detected
        time.sleep(0.5)

        client.disconnect()

        # Verify disconnect callback was called
        assert disconnect_callback.called

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_detects_reconnect_after_disconnect(self, mock_ws_class):
        """Detects reconnect when message received after disconnect."""
        disconnect_callback = MagicMock()
        reconnect_callback = MagicMock()

        client = PublicWebSocketClient(
            symbols=["BTCUSDT"],
            testnet=True,
            on_trade=lambda x: None,
            on_disconnect=disconnect_callback,
            on_reconnect=reconnect_callback,
            heartbeat_interval=0.1,
            disconnect_threshold=0.3,
        )

        client.connect()

        # Wait for disconnect to be detected
        time.sleep(0.5)

        # Simulate message received (triggers reconnect detection)
        client._update_last_message_ts()

        client.disconnect()

        # Verify both callbacks were called
        assert disconnect_callback.called
        assert reconnect_callback.called

        # Verify reconnect callback received timestamps
        call_args = reconnect_callback.call_args[0]
        assert isinstance(call_args[0], datetime)  # disconnected_at
        assert isinstance(call_args[1], datetime)  # reconnected_at

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_reconnect_count_increments(self, mock_ws_class):
        """Reconnect count increments on each detected reconnection."""
        client = PublicWebSocketClient(
            symbols=["BTCUSDT"],
            testnet=True,
            on_trade=lambda x: None,
            heartbeat_interval=0.1,
            disconnect_threshold=0.2,
        )

        client.connect()

        # First disconnect/reconnect cycle
        time.sleep(0.3)
        client._update_last_message_ts()

        # Second disconnect/reconnect cycle
        time.sleep(0.3)
        client._update_last_message_ts()

        client.disconnect()

        state = client.get_connection_state()
        assert state.reconnect_count == 2

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_no_disconnect_when_messages_flowing(self, mock_ws_class):
        """No disconnect detected when messages are continuously received."""
        disconnect_callback = MagicMock()

        client = PublicWebSocketClient(
            symbols=["BTCUSDT"],
            testnet=True,
            on_trade=lambda x: None,
            on_disconnect=disconnect_callback,
            heartbeat_interval=0.1,
            disconnect_threshold=0.3,
        )

        client.connect()

        # Simulate messages being received
        for _ in range(5):
            time.sleep(0.1)
            client._update_last_message_ts()

        client.disconnect()

        # Verify disconnect callback was NOT called
        assert not disconnect_callback.called


class TestPrivateWebSocketClientDisconnectDetection:
    """Test disconnect detection for private WebSocket client."""

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_detects_disconnect_on_message_gap(self, mock_ws_class):
        """Detects disconnect when no messages received for threshold duration."""
        disconnect_callback = MagicMock()

        client = PrivateWebSocketClient(
            api_key="test_key",
            api_secret="test_secret",
            testnet=True,
            on_execution=lambda x: None,
            on_disconnect=disconnect_callback,
            heartbeat_interval=0.1,
            disconnect_threshold=0.3,
        )

        client.connect()

        # Wait for disconnect to be detected
        time.sleep(0.5)

        client.disconnect()

        # Verify disconnect callback was called
        assert disconnect_callback.called

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_detects_reconnect_after_disconnect(self, mock_ws_class):
        """Detects reconnect when message received after disconnect."""
        disconnect_callback = MagicMock()
        reconnect_callback = MagicMock()

        client = PrivateWebSocketClient(
            api_key="test_key",
            api_secret="test_secret",
            testnet=True,
            on_execution=lambda x: None,
            on_disconnect=disconnect_callback,
            on_reconnect=reconnect_callback,
            heartbeat_interval=0.1,
            disconnect_threshold=0.3,
        )

        client.connect()

        # Wait for disconnect to be detected
        time.sleep(0.5)

        # Simulate message received (triggers reconnect detection)
        client._update_last_message_ts()

        client.disconnect()

        # Verify both callbacks were called
        assert disconnect_callback.called
        assert reconnect_callback.called

        # Verify reconnect callback received timestamps
        call_args = reconnect_callback.call_args[0]
        assert isinstance(call_args[0], datetime)  # disconnected_at
        assert isinstance(call_args[1], datetime)  # reconnected_at

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_reconnect_count_increments(self, mock_ws_class):
        """Reconnect count increments on each detected reconnection."""
        client = PrivateWebSocketClient(
            api_key="test_key",
            api_secret="test_secret",
            testnet=True,
            on_execution=lambda x: None,
            heartbeat_interval=0.1,
            disconnect_threshold=0.2,
        )

        client.connect()

        # First disconnect/reconnect cycle
        time.sleep(0.3)
        client._update_last_message_ts()

        # Second disconnect/reconnect cycle
        time.sleep(0.3)
        client._update_last_message_ts()

        client.disconnect()

        state = client.get_connection_state()
        assert state.reconnect_count == 2


class TestPublicWebSocketClientEdgeCases:
    """Additional edge case tests for PublicWebSocketClient."""

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_connect_when_already_connected_disconnects_first(self, mock_ws_class):
        """Connecting while already connected should disconnect first."""
        client = PublicWebSocketClient(
            symbols=["BTCUSDT"],
            testnet=True,
            on_trade=lambda x: None,
        )

        client.connect()
        first_ws = client._ws

        # Connect again
        client.connect()

        # First WS should have been cleaned up
        first_ws.exit.assert_called_once()
        client.disconnect()

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_disconnect_internal_handles_ws_exit_exception(self, mock_ws_class):
        """Disconnect should handle errors from ws.exit() gracefully."""
        mock_ws = MagicMock()
        mock_ws.exit.side_effect = Exception("socket error")
        mock_ws_class.return_value = mock_ws

        client = PublicWebSocketClient(
            symbols=["BTCUSDT"],
            testnet=True,
            on_trade=lambda x: None,
        )

        client.connect()
        client.disconnect()  # Should not raise

        assert not client.is_connected()

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_handle_ticker_updates_last_message_ts(self, mock_ws_class):
        """Ticker handler updates last message timestamp."""
        ticker_received = MagicMock()
        client = PublicWebSocketClient(
            symbols=["BTCUSDT"],
            testnet=True,
            on_ticker=ticker_received,
        )

        client.connect()
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        client._state.last_message_ts = past

        client._handle_ticker({"topic": "tickers.BTCUSDT", "data": {}})

        assert client._state.last_message_ts > past
        ticker_received.assert_called_once()
        client.disconnect()

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_handle_trade_updates_last_message_ts(self, mock_ws_class):
        """Trade handler updates last message timestamp."""
        trade_received = MagicMock()
        client = PublicWebSocketClient(
            symbols=["BTCUSDT"],
            testnet=True,
            on_trade=trade_received,
        )

        client.connect()
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        client._state.last_message_ts = past

        client._handle_trade({"topic": "publicTrade.BTCUSDT", "data": []})

        assert client._state.last_message_ts > past
        trade_received.assert_called_once()
        client.disconnect()

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_callback_exception_caught(self, mock_ws_class):
        """Exceptions in callbacks should be caught, not crash the client."""
        def bad_callback(msg):
            raise ValueError("callback error")

        client = PublicWebSocketClient(
            symbols=["BTCUSDT"],
            testnet=True,
            on_ticker=bad_callback,
        )

        client.connect()
        client._handle_ticker({"topic": "tickers.BTCUSDT", "data": {}})
        # Should not raise
        assert client.is_connected()
        client.disconnect()

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_get_connection_state_returns_copy(self, mock_ws_class):
        """get_connection_state returns independent copy."""
        client = PublicWebSocketClient(
            symbols=["BTCUSDT"],
            testnet=True,
        )

        client.connect()
        state1 = client.get_connection_state()
        state2 = client.get_connection_state()

        assert state1 is not state2
        assert state1.connected_at == state2.connected_at
        client.disconnect()


class TestConnectionStateTracking:
    """Test connection state tracking for both client types."""

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_public_client_tracks_state(self, mock_ws_class):
        """Public client tracks connection state correctly."""
        client = PublicWebSocketClient(
            symbols=["BTCUSDT"],
            testnet=True,
            on_trade=lambda x: None,
        )

        # Before connection
        assert not client.is_connected()

        # After connection
        client.connect()
        assert client.is_connected()
        state = client.get_connection_state()
        assert state.is_connected
        assert state.connected_at is not None
        assert state.last_message_ts is not None

        # After disconnect
        client.disconnect()
        assert not client.is_connected()

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_private_client_tracks_state(self, mock_ws_class):
        """Private client tracks connection state correctly."""
        client = PrivateWebSocketClient(
            api_key="test_key",
            api_secret="test_secret",
            testnet=True,
            on_execution=lambda x: None,
        )

        # Before connection
        assert not client.is_connected()

        # After connection
        client.connect()
        assert client.is_connected()
        state = client.get_connection_state()
        assert state.is_connected
        assert state.connected_at is not None
        assert state.last_message_ts is not None

        # After disconnect
        client.disconnect()
        assert not client.is_connected()


class TestPrivateWebSocketClientHandlers:
    """Test PrivateWebSocketClient handler callbacks (covers lines 416-441)."""

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_handle_execution_calls_callback(self, mock_ws_class):
        """_handle_execution calls on_execution and updates last_message_ts."""
        mock_callback = MagicMock()
        client = PrivateWebSocketClient(
            api_key="test_key",
            api_secret="test_secret",
            testnet=True,
            on_execution=mock_callback,
        )

        client.connect()
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        client._state.last_message_ts = past

        msg = {"topic": "execution", "data": [{"execId": "e1"}]}
        client._handle_execution(msg)

        assert client._state.last_message_ts > past
        mock_callback.assert_called_once_with(msg)
        client.disconnect()

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_handle_execution_callback_exception(self, mock_ws_class):
        """_handle_execution catches callback exceptions without crashing."""
        def bad_callback(msg):
            raise ValueError("callback error")

        client = PrivateWebSocketClient(
            api_key="test_key",
            api_secret="test_secret",
            testnet=True,
            on_execution=bad_callback,
        )

        client.connect()
        client._handle_execution({"topic": "execution", "data": []})
        # Should not raise
        assert client.is_connected()
        client.disconnect()

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_handle_order_calls_callback(self, mock_ws_class):
        """_handle_order calls on_order and updates last_message_ts."""
        mock_callback = MagicMock()
        client = PrivateWebSocketClient(
            api_key="test_key",
            api_secret="test_secret",
            testnet=True,
            on_order=mock_callback,
        )

        client.connect()
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        client._state.last_message_ts = past

        msg = {"topic": "order", "data": [{"orderId": "o1"}]}
        client._handle_order(msg)

        assert client._state.last_message_ts > past
        mock_callback.assert_called_once_with(msg)
        client.disconnect()

    @patch("bybit_adapter.ws_client.WebSocket")
    def test_handle_position_calls_callback(self, mock_ws_class):
        """_handle_position calls on_position and updates last_message_ts."""
        mock_callback = MagicMock()
        client = PrivateWebSocketClient(
            api_key="test_key",
            api_secret="test_secret",
            testnet=True,
            on_position=mock_callback,
        )

        client.connect()
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        client._state.last_message_ts = past

        msg = {"topic": "position", "data": [{"symbol": "BTCUSDT"}]}
        client._handle_position(msg)

        assert client._state.last_message_ts > past
        mock_callback.assert_called_once_with(msg)
        client.disconnect()
