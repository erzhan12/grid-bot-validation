"""Tests for InstrumentInfoProvider (relocated to bybit_adapter in 0090)."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from bybit_adapter.instrument_info import (
    DEFAULT_CACHE_PATH,
    InstrumentInfo,
    InstrumentInfoProvider,
)


@pytest.fixture
def cache_path(tmp_path):
    """Temporary cache file path."""
    return tmp_path / "instruments_cache.json"


@pytest.fixture
def provider(cache_path):
    """Provider with temp cache path."""
    return InstrumentInfoProvider(cache_path=cache_path)


@pytest.fixture
def sample_info():
    """Sample InstrumentInfo for caching tests."""
    return InstrumentInfo(
        symbol="BTCUSDT",
        qty_step=Decimal("0.001"),
        tick_size=Decimal("0.1"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("1000"),
    )


def _make_cache_entry(info: InstrumentInfo, cached_at=None) -> dict:
    """Build a cache dict entry with cached_at timestamp."""
    entry = info.to_dict()
    if cached_at is None:
        cached_at = datetime.now(timezone.utc)
    entry["cached_at"] = cached_at.isoformat()
    return entry


class TestDefaultCachePath:
    """Provider uses expected default cache path."""

    def test_default_cache_path(self):
        """DEFAULT_CACHE_PATH is the expected constant."""
        assert DEFAULT_CACHE_PATH == Path("conf/instruments_cache.json")
        provider = InstrumentInfoProvider()
        assert provider.cache_path == DEFAULT_CACHE_PATH


class TestRequireLive:
    """Tests for require_live=True behavior."""

    def test_require_live_no_cache_api_fails_raises(self, provider):
        """require_live=True + no cache + API fail → ValueError, not default."""
        with patch.object(provider, "fetch_from_bybit", return_value=None):
            with pytest.raises(ValueError, match="require_live"):
                provider.get("BTCUSDT", require_live=True)

    def test_require_live_stale_cache_api_fails_returns_cached(
        self, provider, cache_path, sample_info
    ):
        """require_live=True + API fail + stale cache → returns stale cached value."""
        stale_time = datetime.now(timezone.utc) - provider.cache_ttl - timedelta(hours=1)
        cache_path.write_text(
            json.dumps({"BTCUSDT": _make_cache_entry(sample_info, cached_at=stale_time)})
        )

        with patch.object(provider, "fetch_from_bybit", return_value=None):
            result = provider.get("BTCUSDT", require_live=True)

        assert result is not None
        assert result.symbol == "BTCUSDT"
        assert result.qty_step == Decimal("0.001")

    def test_require_false_no_cache_api_fails_returns_default(self, provider):
        """require_live=False (default) + no cache + API fail → returns synthetic defaults."""
        with patch.object(provider, "fetch_from_bybit", return_value=None):
            result = provider.get("BTCUSDT", require_live=False)

        assert result.symbol == "BTCUSDT"
        assert result.tick_size == Decimal("0.1")
        assert result.qty_step == Decimal("0.001")

    def test_require_live_fresh_cache_returns_cached(self, provider, cache_path, sample_info):
        """require_live=True with fresh cache returns cached value without error."""
        cache_path.write_text(
            json.dumps({"BTCUSDT": _make_cache_entry(sample_info)})
        )

        with patch.object(provider, "fetch_from_bybit") as mock_fetch:
            result = provider.get("BTCUSDT", require_live=True)

        mock_fetch.assert_not_called()
        assert result.symbol == "BTCUSDT"

    def test_require_live_api_succeeds_returns_live(self, provider, sample_info):
        """require_live=True + API succeeds → returns API result, no error."""
        with patch.object(provider, "fetch_from_bybit", return_value=sample_info):
            result = provider.get("BTCUSDT", require_live=True)

        assert result.symbol == "BTCUSDT"
