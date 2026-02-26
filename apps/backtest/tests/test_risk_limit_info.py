"""Tests for risk limit info provider."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from backtest.risk_limit_info import (
    RiskLimitProvider,
    DEFAULT_CACHE_PATH,
    _tiers_to_dict,
    _tiers_from_dict,
)
from gridcore.pnl import MMTiers


# Sample tiers for testing
SAMPLE_TIERS: MMTiers = [
    (Decimal("200000"), Decimal("0.01"), Decimal("0")),
    (Decimal("1000000"), Decimal("0.025"), Decimal("3000")),
    (Decimal("Infinity"), Decimal("0.05"), Decimal("28000")),
]


class TestTiersSerialization:
    """Tests for tier serialization helpers."""

    def test_round_trip(self):
        """to_dict → from_dict produces identical tiers."""
        result = _tiers_from_dict(_tiers_to_dict(SAMPLE_TIERS))
        assert result == SAMPLE_TIERS

    def test_infinity_survives_round_trip(self):
        """Infinity cap survives JSON serialization."""
        result = _tiers_from_dict(_tiers_to_dict(SAMPLE_TIERS))
        assert result[-1][0] == Decimal("Infinity")


class TestRiskLimitProvider:
    """Tests for RiskLimitProvider."""

    @pytest.fixture
    def cache_path(self, tmp_path):
        """Temporary cache file path."""
        return tmp_path / "risk_limits_cache.json"

    @pytest.fixture
    def provider(self, cache_path):
        """Provider with temp cache path."""
        return RiskLimitProvider(cache_path=cache_path)

    def _make_cache_entry(self, tiers, cached_at=None):
        """Build cache dict entry with cached_at timestamp."""
        if cached_at is None:
            cached_at = datetime.now(timezone.utc)
        return {
            "tiers": _tiers_to_dict(tiers),
            "cached_at": cached_at.isoformat(),
        }

    def test_default_cache_path(self):
        """Provider uses DEFAULT_CACHE_PATH when none given."""
        provider = RiskLimitProvider()
        assert provider.cache_path == DEFAULT_CACHE_PATH

    # --- load_from_cache ---

    def test_load_from_cache_no_file(self, provider):
        """Returns None when cache file doesn't exist."""
        assert provider.load_from_cache("BTCUSDT") is None

    def test_load_from_cache_hit(self, provider, cache_path):
        """Returns MMTiers when symbol found in cache."""
        cache_path.write_text(json.dumps({
            "BTCUSDT": self._make_cache_entry(SAMPLE_TIERS),
        }))

        result = provider.load_from_cache("BTCUSDT")
        assert result is not None
        assert len(result) == 3
        assert result[0][1] == Decimal("0.01")

    def test_load_from_cache_miss(self, provider, cache_path):
        """Returns None when symbol not in cache."""
        cache_path.write_text(json.dumps({
            "BTCUSDT": self._make_cache_entry(SAMPLE_TIERS),
        }))

        assert provider.load_from_cache("ETHUSDT") is None

    def test_load_from_cache_corrupted(self, provider, cache_path):
        """Returns None when cache file is corrupted JSON."""
        cache_path.write_text("not valid json{{{")

        assert provider.load_from_cache("BTCUSDT") is None

    # --- save_to_cache ---

    def test_save_to_cache_creates_file(self, provider, cache_path):
        """Creates cache file and writes tiers."""
        provider.save_to_cache("BTCUSDT", SAMPLE_TIERS)

        assert cache_path.exists()
        cache = json.loads(cache_path.read_text())
        assert "BTCUSDT" in cache
        assert len(cache["BTCUSDT"]["tiers"]) == 3

    def test_save_to_cache_updates_existing(self, provider, cache_path):
        """Adds to existing cache without overwriting other entries."""
        provider.save_to_cache("ETHUSDT", SAMPLE_TIERS)
        provider.save_to_cache("BTCUSDT", SAMPLE_TIERS)

        cache = json.loads(cache_path.read_text())
        assert "ETHUSDT" in cache
        assert "BTCUSDT" in cache

    def test_save_to_cache_creates_parent_dirs(self, tmp_path):
        """Creates parent directories if they don't exist."""
        deep_path = tmp_path / "a" / "b" / "cache.json"
        provider = RiskLimitProvider(cache_path=deep_path)

        provider.save_to_cache("BTCUSDT", SAMPLE_TIERS)

        assert deep_path.exists()

    # --- fetch_from_bybit ---

    def test_fetch_from_bybit_no_client(self, provider):
        """Returns None when no rest_client is configured."""
        assert provider._rest_client is None
        result = provider.fetch_from_bybit("BTCUSDT")
        assert result is None

    def test_fetch_from_bybit_with_rest_client(self, cache_path):
        """Fetches and parses tiers via injected BybitRestClient."""
        mock_client = MagicMock()
        mock_client.get_risk_limit.return_value = [
            {
                "riskLimitValue": "200000",
                "maintenanceMargin": "0.01",
                "mmDeduction": "0",
            },
            {
                "riskLimitValue": "1000000",
                "maintenanceMargin": "0.025",
                "mmDeduction": "3000",
            },
        ]

        provider = RiskLimitProvider(cache_path=cache_path, rest_client=mock_client)
        result = provider.fetch_from_bybit("BTCUSDT")

        mock_client.get_risk_limit.assert_called_once_with(symbol="BTCUSDT")
        assert result is not None
        assert len(result) == 2
        assert result[0] == (Decimal("200000"), Decimal("0.01"), Decimal("0"))
        assert result[1] == (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"))

    def test_fetch_from_bybit_empty_list(self, cache_path):
        """Returns None when API returns empty tier list."""
        mock_client = MagicMock()
        mock_client.get_risk_limit.return_value = []

        provider = RiskLimitProvider(cache_path=cache_path, rest_client=mock_client)
        result = provider.fetch_from_bybit("BTCUSDT")

        assert result is None

    def test_fetch_from_bybit_client_exception(self, cache_path):
        """Returns None when rest_client raises an exception."""
        mock_client = MagicMock()
        mock_client.get_risk_limit.side_effect = ConnectionError("timeout")

        provider = RiskLimitProvider(cache_path=cache_path, rest_client=mock_client)
        result = provider.fetch_from_bybit("BTCUSDT")

        assert result is None

    # --- get ---

    def test_get_returns_cached(self, provider, cache_path):
        """get() returns fresh cached tiers without hitting API."""
        cache_path.write_text(json.dumps({
            "BTCUSDT": self._make_cache_entry(SAMPLE_TIERS),
        }))

        with patch.object(provider, "fetch_from_bybit") as mock_fetch:
            result = provider.get("BTCUSDT")

        mock_fetch.assert_not_called()
        assert len(result) == 3

    def test_get_fetches_on_cache_miss(self, provider):
        """get() calls API when cache misses, then caches result."""
        with patch.object(provider, "fetch_from_bybit", return_value=SAMPLE_TIERS) as mock_fetch:
            result = provider.get("BTCUSDT")

        mock_fetch.assert_called_once_with("BTCUSDT")
        assert len(result) == 3
        # Verify it was cached
        assert provider.load_from_cache("BTCUSDT") is not None

    def test_get_force_fetch_skips_cache(self, provider, cache_path):
        """get(force_fetch=True) calls API even when cache exists."""
        cache_path.write_text(json.dumps({
            "BTCUSDT": self._make_cache_entry(SAMPLE_TIERS),
        }))

        with patch.object(provider, "fetch_from_bybit", return_value=SAMPLE_TIERS) as mock_fetch:
            result = provider.get("BTCUSDT", force_fetch=True)

        mock_fetch.assert_called_once_with("BTCUSDT")
        assert len(result) == 3

    def test_get_api_fail_falls_back_to_cache(self, provider, cache_path):
        """get() falls back to cache when API fails."""
        cache_path.write_text(json.dumps({
            "BTCUSDT": self._make_cache_entry(SAMPLE_TIERS),
        }))

        with patch.object(provider, "fetch_from_bybit", return_value=None):
            result = provider.get("BTCUSDT", force_fetch=True)

        assert len(result) == 3

    def test_get_returns_hardcoded_fallback(self, provider):
        """get() returns hardcoded tiers when both API and cache fail."""
        with patch.object(provider, "fetch_from_bybit", return_value=None):
            result = provider.get("BTCUSDT")

        # Should return hardcoded BTCUSDT tiers (7 tiers)
        assert len(result) == 7

    def test_get_returns_default_fallback_for_unknown_symbol(self, provider):
        """get() returns default tiers for unknown symbol when all fails."""
        with patch.object(provider, "fetch_from_bybit", return_value=None):
            result = provider.get("XYZUSDT")

        # Should return default tiers (5 tiers)
        assert len(result) == 5

    # --- TTL ---

    def test_get_refreshes_stale_cache(self, provider, cache_path):
        """get() calls API when cache is older than cache_ttl."""
        stale_time = datetime.now(timezone.utc) - provider.cache_ttl - timedelta(hours=1)
        cache_path.write_text(json.dumps({
            "BTCUSDT": self._make_cache_entry(SAMPLE_TIERS, cached_at=stale_time),
        }))

        fresh_tiers: MMTiers = [
            (Decimal("500000"), Decimal("0.005"), Decimal("0")),
            (Decimal("Infinity"), Decimal("0.01"), Decimal("2500")),
        ]

        with patch.object(provider, "fetch_from_bybit", return_value=fresh_tiers) as mock_fetch:
            result = provider.get("BTCUSDT")

        mock_fetch.assert_called_once_with("BTCUSDT")
        # Returns fresh API data, not stale cache
        assert len(result) == 2
        assert result[0][0] == Decimal("500000")

    def test_get_uses_stale_cache_when_api_fails(self, provider, cache_path):
        """get() falls back to stale cache when API is unavailable."""
        stale_time = datetime.now(timezone.utc) - provider.cache_ttl - timedelta(hours=1)
        cache_path.write_text(json.dumps({
            "BTCUSDT": self._make_cache_entry(SAMPLE_TIERS, cached_at=stale_time),
        }))

        with patch.object(provider, "fetch_from_bybit", return_value=None):
            result = provider.get("BTCUSDT")

        # Falls back to stale cache rather than hardcoded
        assert len(result) == 3
