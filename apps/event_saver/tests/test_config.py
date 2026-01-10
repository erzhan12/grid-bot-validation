"""Tests for EventSaverConfig."""

import pytest

from event_saver.config import EventSaverConfig


class TestEventSaverConfig:
    """Test EventSaverConfig settings."""

    def test_default_values(self, monkeypatch):
        """Test default configuration values."""
        # Clear any env vars that might affect config
        monkeypatch.delenv("EVENTSAVER_SYMBOLS", raising=False)
        monkeypatch.delenv("EVENTSAVER_TESTNET", raising=False)
        monkeypatch.delenv("EVENTSAVER_BATCH_SIZE", raising=False)
        monkeypatch.delenv("EVENTSAVER_FLUSH_INTERVAL", raising=False)
        monkeypatch.delenv("EVENTSAVER_GAP_THRESHOLD_SECONDS", raising=False)
        monkeypatch.delenv("EVENTSAVER_DATABASE_URL", raising=False)

        config = EventSaverConfig()

        # Check defaults
        assert config.get_symbols() == []
        assert config.testnet is True
        assert config.batch_size == 100
        assert config.flush_interval == 5.0
        assert config.gap_threshold_seconds == 5.0
        assert "sqlite" in config.database_url

    def test_symbols_from_env(self, monkeypatch):
        """Test symbols parsed from comma-separated env var."""
        monkeypatch.setenv("EVENTSAVER_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT")

        config = EventSaverConfig()

        assert config.get_symbols() == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    def test_single_symbol(self, monkeypatch):
        """Test single symbol without comma."""
        monkeypatch.setenv("EVENTSAVER_SYMBOLS", "BTCUSDT")

        config = EventSaverConfig()

        assert config.get_symbols() == ["BTCUSDT"]

    def test_empty_symbols(self, monkeypatch):
        """Test empty symbols string."""
        monkeypatch.setenv("EVENTSAVER_SYMBOLS", "")

        config = EventSaverConfig()

        assert config.get_symbols() == []

    def test_testnet_false(self, monkeypatch):
        """Test testnet can be disabled."""
        monkeypatch.setenv("EVENTSAVER_TESTNET", "false")

        config = EventSaverConfig()

        assert config.testnet is False

    def test_batch_size_override(self, monkeypatch):
        """Test batch_size can be overridden."""
        monkeypatch.setenv("EVENTSAVER_BATCH_SIZE", "500")

        config = EventSaverConfig()

        assert config.batch_size == 500

    def test_flush_interval_override(self, monkeypatch):
        """Test flush_interval can be overridden."""
        monkeypatch.setenv("EVENTSAVER_FLUSH_INTERVAL", "10.5")

        config = EventSaverConfig()

        assert config.flush_interval == 10.5

    def test_gap_threshold_override(self, monkeypatch):
        """Test gap_threshold_seconds can be overridden."""
        monkeypatch.setenv("EVENTSAVER_GAP_THRESHOLD_SECONDS", "15.0")

        config = EventSaverConfig()

        assert config.gap_threshold_seconds == 15.0

    def test_database_url_default(self, monkeypatch):
        """Test database_url has default sqlite value."""
        monkeypatch.delenv("EVENTSAVER_DATABASE_URL", raising=False)

        config = EventSaverConfig()

        assert "sqlite" in config.database_url

    def test_database_url_override(self, monkeypatch):
        """Test database_url can be overridden."""
        monkeypatch.setenv("EVENTSAVER_DATABASE_URL", "postgresql://user:pass@localhost/db")

        config = EventSaverConfig()

        assert config.database_url == "postgresql://user:pass@localhost/db"

    def test_symbols_whitespace_handling(self, monkeypatch):
        """Test symbols with extra whitespace are trimmed."""
        monkeypatch.setenv("EVENTSAVER_SYMBOLS", " BTCUSDT , ETHUSDT , SOLUSDT ")

        config = EventSaverConfig()

        assert config.get_symbols() == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    def test_symbols_lowercase_converted(self, monkeypatch):
        """Test lowercase symbols are converted to uppercase."""
        monkeypatch.setenv("EVENTSAVER_SYMBOLS", "btcusdt,ethusdt")

        config = EventSaverConfig()

        assert config.get_symbols() == ["BTCUSDT", "ETHUSDT"]
