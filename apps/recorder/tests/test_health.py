"""Tests for recorder health logging."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grid_db import DatabaseFactory, DatabaseSettings
from bybit_adapter.ws_client import ConnectionState

from recorder.config import RecorderConfig
from recorder.recorder import Recorder


@pytest.fixture
def db():
    settings = DatabaseSettings()
    settings.db_type = "sqlite"
    settings.db_name = ":memory:"
    factory = DatabaseFactory(settings)
    factory.create_tables()
    return factory


@pytest.fixture
def fast_health_config():
    """Config with very short health log interval for testing."""
    return RecorderConfig(
        symbols=["BTCUSDT"],
        database_url="sqlite:///:memory:",
        testnet=True,
        batch_size=10,
        flush_interval=1.0,
        health_log_interval=0.2,  # 200ms for fast test
    )


class TestHealthLogging:
    """Tests for health log loop."""

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_health_log_runs_periodically(
        self, mock_rest_cls, mock_pub_cls, fast_health_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=fast_health_config, db=db)
        await recorder.start()

        # Wait for at least one health log cycle
        await asyncio.sleep(0.5)

        # Health task should be running
        assert recorder._health_task is not None
        assert not recorder._health_task.done()

        await recorder.stop()

        # After stop, health task should be cancelled
        assert recorder._health_task is None

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_stats_with_connected_ws(
        self, mock_rest_cls, mock_pub_cls, fast_health_config, db
    ):
        conn_state = ConnectionState(
            is_connected=True,
            reconnect_count=3,
        )
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = conn_state
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=fast_health_config, db=db)
        await recorder.start()

        stats = recorder.get_stats()
        assert stats["public_ws"]["connected"] is True
        assert stats["public_ws"]["reconnect_count"] == 3

        await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_stats_with_disconnected_ws(
        self, mock_rest_cls, mock_pub_cls, fast_health_config, db
    ):
        conn_state = ConnectionState(
            is_connected=False,
            reconnect_count=0,
        )
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = conn_state
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=fast_health_config, db=db)
        await recorder.start()

        stats = recorder.get_stats()
        assert stats["public_ws"]["connected"] is False

        await recorder.stop()

    @patch("recorder.recorder.PublicCollector")
    @patch("recorder.recorder.BybitRestClient")
    async def test_stats_with_no_connection_state(
        self, mock_rest_cls, mock_pub_cls, fast_health_config, db
    ):
        mock_pub = MagicMock()
        mock_pub.start = AsyncMock()
        mock_pub.stop = AsyncMock()
        mock_pub.get_connection_state.return_value = None
        mock_pub_cls.return_value = mock_pub

        recorder = Recorder(config=fast_health_config, db=db)
        await recorder.start()

        stats = recorder.get_stats()
        assert "public_ws" not in stats

        await recorder.stop()
