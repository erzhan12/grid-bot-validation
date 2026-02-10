"""Tests for instrument info provider."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from backtest.instrument_info import InstrumentInfo, InstrumentInfoProvider, DEFAULT_CACHE_PATH


class TestInstrumentInfo:
    """Tests for InstrumentInfo data class."""

    @pytest.fixture
    def info(self):
        """Sample instrument info."""
        return InstrumentInfo(
            symbol="BTCUSDT",
            qty_step=Decimal("0.001"),
            tick_size=Decimal("0.1"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal("1000"),
        )

    def test_round_qty_rounds_up(self, info):
        """round_qty rounds UP to nearest qty_step (ceil)."""
        # 0.0011 rounds up to 0.002
        assert info.round_qty(Decimal("0.0011")) == Decimal("0.002")

    def test_round_qty_exact_no_change(self, info):
        """round_qty leaves exact multiples unchanged."""
        assert info.round_qty(Decimal("0.003")) == Decimal("0.003")

    def test_round_price_nearest(self, info):
        """round_price rounds to nearest tick_size."""
        # 100.04 → 100.0, 100.06 → 100.1
        assert info.round_price(Decimal("100.04")) == Decimal("100.0")
        assert info.round_price(Decimal("100.06")) == Decimal("100.1")

    def test_round_price_exact_no_change(self, info):
        """round_price leaves exact multiples unchanged."""
        assert info.round_price(Decimal("100.3")) == Decimal("100.3")

    def test_to_dict(self, info):
        """to_dict serializes all fields as strings."""
        d = info.to_dict()
        assert d == {
            "symbol": "BTCUSDT",
            "qty_step": "0.001",
            "tick_size": "0.1",
            "min_qty": "0.001",
            "max_qty": "1000",
        }

    def test_from_dict_round_trip(self, info):
        """from_dict restores identical object from to_dict output."""
        restored = InstrumentInfo.from_dict(info.to_dict())
        assert restored.symbol == info.symbol
        assert restored.qty_step == info.qty_step
        assert restored.tick_size == info.tick_size
        assert restored.min_qty == info.min_qty
        assert restored.max_qty == info.max_qty


class TestInstrumentInfoProvider:
    """Tests for InstrumentInfoProvider."""

    @pytest.fixture
    def cache_path(self, tmp_path):
        """Temporary cache file path."""
        return tmp_path / "instruments_cache.json"

    @pytest.fixture
    def provider(self, cache_path):
        """Provider with temp cache path."""
        return InstrumentInfoProvider(cache_path=cache_path)

    @pytest.fixture
    def sample_info(self):
        """Sample InstrumentInfo for caching tests."""
        return InstrumentInfo(
            symbol="BTCUSDT",
            qty_step=Decimal("0.001"),
            tick_size=Decimal("0.1"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal("1000"),
        )

    def _make_cache_entry(self, info, cached_at=None):
        """Build cache dict entry with cached_at timestamp."""
        entry = info.to_dict()
        if cached_at is None:
            cached_at = datetime.now(timezone.utc)
        entry["cached_at"] = cached_at.isoformat()
        return entry

    def test_default_cache_path(self):
        """Provider uses DEFAULT_CACHE_PATH when none given."""
        provider = InstrumentInfoProvider()
        assert provider.cache_path == DEFAULT_CACHE_PATH

    def test_custom_cache_path(self, cache_path):
        """Provider uses provided cache path."""
        provider = InstrumentInfoProvider(cache_path=cache_path)
        assert provider.cache_path == cache_path

    # --- load_from_cache ---

    def test_load_from_cache_no_file(self, provider):
        """Returns None when cache file doesn't exist."""
        assert provider.load_from_cache("BTCUSDT") is None

    def test_load_from_cache_hit(self, provider, cache_path, sample_info):
        """Returns InstrumentInfo when symbol found in cache."""
        # Pre-populate cache
        cache_path.write_text(json.dumps({"BTCUSDT": self._make_cache_entry(sample_info)}))

        result = provider.load_from_cache("BTCUSDT")
        assert result is not None
        assert result.symbol == "BTCUSDT"
        assert result.qty_step == Decimal("0.001")

    def test_load_from_cache_miss(self, provider, cache_path, sample_info):
        """Returns None when symbol not in cache."""
        cache_path.write_text(json.dumps({"BTCUSDT": self._make_cache_entry(sample_info)}))

        assert provider.load_from_cache("ETHUSDT") is None

    def test_load_from_cache_corrupted(self, provider, cache_path):
        """Returns None when cache file is corrupted JSON."""
        cache_path.write_text("not valid json{{{")

        assert provider.load_from_cache("BTCUSDT") is None

    # --- save_to_cache ---

    def test_save_to_cache_creates_file(self, provider, cache_path, sample_info):
        """Creates cache file and writes instrument info."""
        provider.save_to_cache(sample_info)

        assert cache_path.exists()
        cache = json.loads(cache_path.read_text())
        assert "BTCUSDT" in cache
        assert cache["BTCUSDT"]["qty_step"] == "0.001"

    def test_save_to_cache_updates_existing(self, provider, cache_path, sample_info):
        """Adds to existing cache without overwriting other entries."""
        # Pre-populate with ETH
        eth_info = InstrumentInfo(
            symbol="ETHUSDT",
            qty_step=Decimal("0.01"),
            tick_size=Decimal("0.01"),
            min_qty=Decimal("0.01"),
            max_qty=Decimal("500"),
        )
        provider.save_to_cache(eth_info)
        provider.save_to_cache(sample_info)

        cache = json.loads(cache_path.read_text())
        assert "ETHUSDT" in cache
        assert "BTCUSDT" in cache

    def test_save_to_cache_creates_parent_dirs(self, tmp_path, sample_info):
        """Creates parent directories if they don't exist."""
        deep_path = tmp_path / "a" / "b" / "cache.json"
        provider = InstrumentInfoProvider(cache_path=deep_path)

        provider.save_to_cache(sample_info)

        assert deep_path.exists()

    # --- fetch_from_bybit ---

    @patch("backtest.instrument_info.InstrumentInfoProvider.fetch_from_bybit")
    def test_fetch_from_bybit_success(self, mock_fetch):
        """Returns InstrumentInfo on successful API call."""
        mock_fetch.return_value = InstrumentInfo(
            symbol="BTCUSDT",
            qty_step=Decimal("0.001"),
            tick_size=Decimal("0.1"),
            min_qty=Decimal("0.001"),
            max_qty=Decimal("100"),
        )

        provider = InstrumentInfoProvider()
        result = provider.fetch_from_bybit("BTCUSDT")

        assert result is not None
        assert result.symbol == "BTCUSDT"

    def test_fetch_from_bybit_api_error(self, provider):
        """Returns None when API returns error code."""
        mock_session = MagicMock()
        mock_session.get_instruments_info.return_value = {
            "retCode": 10001,
            "retMsg": "Invalid symbol",
        }

        with patch("pybit.unified_trading.HTTP", return_value=mock_session):
            result = provider.fetch_from_bybit("INVALID")

        assert result is None

    def test_fetch_from_bybit_no_instruments(self, provider):
        """Returns None when API returns empty instrument list."""
        mock_session = MagicMock()
        mock_session.get_instruments_info.return_value = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"list": []},
        }

        with patch("pybit.unified_trading.HTTP", return_value=mock_session):
            result = provider.fetch_from_bybit("BTCUSDT")

        assert result is None

    def test_fetch_from_bybit_parses_response(self, provider):
        """Parses lot and price filters from API response."""
        mock_session = MagicMock()
        mock_session.get_instruments_info.return_value = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "lotSizeFilter": {
                            "qtyStep": "0.001",
                            "minOrderQty": "0.001",
                            "maxOrderQty": "100",
                        },
                        "priceFilter": {
                            "tickSize": "0.10",
                        },
                    }
                ]
            },
        }

        with patch("pybit.unified_trading.HTTP", return_value=mock_session):
            result = provider.fetch_from_bybit("BTCUSDT")

        assert result is not None
        assert result.qty_step == Decimal("0.001")
        assert result.tick_size == Decimal("0.10")
        assert result.min_qty == Decimal("0.001")
        assert result.max_qty == Decimal("100")

    def test_fetch_from_bybit_zero_qty_step_returns_none(self, provider):
        """Returns None when API returns zero qty_step."""
        mock_session = MagicMock()
        mock_session.get_instruments_info.return_value = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "lotSizeFilter": {
                            "qtyStep": "0",
                            "minOrderQty": "0.001",
                            "maxOrderQty": "100",
                        },
                        "priceFilter": {"tickSize": "0.1"},
                    }
                ]
            },
        }

        with patch("pybit.unified_trading.HTTP", return_value=mock_session):
            result = provider.fetch_from_bybit("BTCUSDT")

        assert result is None

    def test_fetch_from_bybit_zero_tick_size_returns_none(self, provider):
        """Returns None when API returns zero tick_size."""
        mock_session = MagicMock()
        mock_session.get_instruments_info.return_value = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "lotSizeFilter": {
                            "qtyStep": "0.001",
                            "minOrderQty": "0.001",
                            "maxOrderQty": "100",
                        },
                        "priceFilter": {"tickSize": "0"},
                    }
                ]
            },
        }

        with patch("pybit.unified_trading.HTTP", return_value=mock_session):
            result = provider.fetch_from_bybit("BTCUSDT")

        assert result is None

    def test_fetch_from_bybit_network_exception(self, provider):
        """Returns None on network/connection error."""
        with patch("pybit.unified_trading.HTTP", side_effect=ConnectionError("timeout")):
            result = provider.fetch_from_bybit("BTCUSDT")

        assert result is None

    # --- get ---

    def test_get_returns_cached(self, provider, cache_path, sample_info):
        """get() returns fresh cached info without hitting API."""
        cache_path.write_text(json.dumps({"BTCUSDT": self._make_cache_entry(sample_info)}))

        with patch.object(provider, "fetch_from_bybit") as mock_fetch:
            result = provider.get("BTCUSDT")

        mock_fetch.assert_not_called()
        assert result.symbol == "BTCUSDT"

    def test_get_fetches_on_cache_miss(self, provider, sample_info):
        """get() calls API when cache misses, then caches result."""
        with patch.object(provider, "fetch_from_bybit", return_value=sample_info) as mock_fetch:
            result = provider.get("BTCUSDT")

        mock_fetch.assert_called_once_with("BTCUSDT")
        assert result.symbol == "BTCUSDT"
        # Verify it was cached
        assert provider.load_from_cache("BTCUSDT") is not None

    def test_get_force_fetch_skips_cache(self, provider, cache_path, sample_info):
        """get(force_fetch=True) calls API even when cache exists."""
        cache_path.write_text(json.dumps({"BTCUSDT": self._make_cache_entry(sample_info)}))

        with patch.object(provider, "fetch_from_bybit", return_value=sample_info) as mock_fetch:
            result = provider.get("BTCUSDT", force_fetch=True)

        mock_fetch.assert_called_once_with("BTCUSDT")
        assert result.symbol == "BTCUSDT"

    def test_get_api_fail_falls_back_to_cache(self, provider, cache_path, sample_info):
        """get() falls back to cache when API fails."""
        cache_path.write_text(json.dumps({"BTCUSDT": self._make_cache_entry(sample_info)}))

        with patch.object(provider, "fetch_from_bybit", return_value=None):
            result = provider.get("BTCUSDT", force_fetch=True)

        assert result.symbol == "BTCUSDT"
        assert result.qty_step == Decimal("0.001")

    def test_get_returns_defaults_when_nothing_available(self, provider):
        """get() returns defaults when both API and cache fail."""
        with patch.object(provider, "fetch_from_bybit", return_value=None):
            result = provider.get("BTCUSDT")

        assert result.symbol == "BTCUSDT"
        assert result.qty_step == Decimal("0.001")
        assert result.tick_size == Decimal("0.1")
        assert result.min_qty == Decimal("0.001")
        assert result.max_qty == Decimal("1000")

    # --- TTL ---

    def test_get_refreshes_stale_cache(self, provider, cache_path, sample_info):
        """get() calls API when cache is older than cache_ttl."""
        stale_time = datetime.now(timezone.utc) - provider.cache_ttl - timedelta(hours=1)
        cache_path.write_text(json.dumps({
            "BTCUSDT": self._make_cache_entry(sample_info, cached_at=stale_time),
        }))

        fresh_info = InstrumentInfo(
            symbol="BTCUSDT",
            qty_step=Decimal("0.01"),
            tick_size=Decimal("0.5"),
            min_qty=Decimal("0.01"),
            max_qty=Decimal("500"),
        )

        with patch.object(provider, "fetch_from_bybit", return_value=fresh_info) as mock_fetch:
            result = provider.get("BTCUSDT")

        mock_fetch.assert_called_once_with("BTCUSDT")
        # Returns fresh API data, not stale cache
        assert result.qty_step == Decimal("0.01")
        assert result.tick_size == Decimal("0.5")

    def test_get_uses_stale_cache_when_api_fails(self, provider, cache_path, sample_info):
        """get() falls back to stale cache when API is unavailable."""
        stale_time = datetime.now(timezone.utc) - provider.cache_ttl - timedelta(hours=1)
        cache_path.write_text(json.dumps({
            "BTCUSDT": self._make_cache_entry(sample_info, cached_at=stale_time),
        }))

        with patch.object(provider, "fetch_from_bybit", return_value=None):
            result = provider.get("BTCUSDT")

        # Falls back to stale cache rather than defaults
        assert result.symbol == "BTCUSDT"
        assert result.qty_step == Decimal("0.001")

    def test_get_treats_missing_cached_at_as_stale(self, provider, cache_path, sample_info):
        """get() treats cache entries without cached_at as stale."""
        # Raw entry without cached_at (e.g. old cache format)
        cache_path.write_text(json.dumps({"BTCUSDT": sample_info.to_dict()}))

        with patch.object(provider, "fetch_from_bybit", return_value=sample_info) as mock_fetch:
            result = provider.get("BTCUSDT")

        # Should try API because no cached_at means stale
        mock_fetch.assert_called_once_with("BTCUSDT")
