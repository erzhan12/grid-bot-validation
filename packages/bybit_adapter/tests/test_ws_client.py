"""Tests for WebSocket client disconnect/reconnect detection."""

import pytest
from datetime import datetime, UTC
from unittest.mock import MagicMock, patch
import threading
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
