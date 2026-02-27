"""Tests for risk limit info provider."""

import json
import logging
import threading
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
    (Decimal("200000"), Decimal("0.01"), Decimal("0"), Decimal("0.02")),
    (Decimal("1000000"), Decimal("0.025"), Decimal("3000"), Decimal("0.05")),
    (Decimal("Infinity"), Decimal("0.05"), Decimal("28000"), Decimal("0.1")),
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

    def test_imr_rate_survives_round_trip(self):
        """imr_rate survives serialization round-trip."""
        result = _tiers_from_dict(_tiers_to_dict(SAMPLE_TIERS))
        assert result[0][3] == Decimal("0.02")
        assert result[1][3] == Decimal("0.05")
        assert result[2][3] == Decimal("0.1")

    def test_backward_compat_old_cache_format(self):
        """Old cache format (3 keys, no imr_rate) loads with imr_rate=0."""
        old_format = [
            {"max_value": "200000", "mmr_rate": "0.01", "deduction": "0"},
            {"max_value": "Infinity", "mmr_rate": "0.025", "deduction": "3000"},
        ]
        result = _tiers_from_dict(old_format)
        assert len(result) == 2
        assert result[0] == (Decimal("200000"), Decimal("0.01"), Decimal("0"), Decimal("0"))
        assert result[1] == (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0"))


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
        """Provider uses resolved DEFAULT_CACHE_PATH when none given."""
        provider = RiskLimitProvider()
        assert provider.cache_path == DEFAULT_CACHE_PATH.resolve()

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

    def test_save_to_cache_corrupted_tiers_not_list(self, provider, cache_path):
        """Overwrites cache entry when existing tiers is not a list (corrupted)."""
        cache_path.write_text(json.dumps({
            "BTCUSDT": {"tiers": "not-a-list", "cached_at": "2025-01-01T00:00:00+00:00"},
        }))

        provider.save_to_cache("BTCUSDT", SAMPLE_TIERS)

        cache = json.loads(cache_path.read_text())
        assert isinstance(cache["BTCUSDT"]["tiers"], list)
        assert len(cache["BTCUSDT"]["tiers"]) == 3

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
                "initialMargin": "0.02",
            },
            {
                "riskLimitValue": "1000000",
                "maintenanceMargin": "0.025",
                "mmDeduction": "3000",
                "initialMargin": "0.05",
            },
        ]

        provider = RiskLimitProvider(cache_path=cache_path, rest_client=mock_client)
        result = provider.fetch_from_bybit("BTCUSDT")

        mock_client.get_risk_limit.assert_called_once_with(symbol="BTCUSDT")
        assert result is not None
        assert len(result) == 2
        assert result[0] == (Decimal("200000"), Decimal("0.01"), Decimal("0"), Decimal("0.02"))
        assert result[1] == (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0.05"))

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
            (Decimal("500000"), Decimal("0.005"), Decimal("0"), Decimal("0.01")),
            (Decimal("Infinity"), Decimal("0.01"), Decimal("2500"), Decimal("0.02")),
        ]

        with patch.object(provider, "fetch_from_bybit", return_value=fresh_tiers) as mock_fetch:
            result = provider.get("BTCUSDT")

        mock_fetch.assert_called_once_with("BTCUSDT")
        # Returns fresh API data, not stale cache
        assert len(result) == 2
        assert result[0][0] == Decimal("500000")

    def test_stale_cache_triggers_api_refresh(self, provider, cache_path):
        """Stale cache triggers API refresh and updates cache when force_fetch=False."""
        stale_time = datetime.now(timezone.utc) - provider.cache_ttl - timedelta(hours=2)
        stale_tiers: MMTiers = [
            (Decimal("200000"), Decimal("0.01"), Decimal("0"), Decimal("0.02")),
            (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0.05")),
        ]
        fresh_tiers: MMTiers = [
            (Decimal("500000"), Decimal("0.005"), Decimal("0"), Decimal("0.01")),
            (Decimal("Infinity"), Decimal("0.01"), Decimal("2500"), Decimal("0.02")),
        ]
        cache_path.write_text(json.dumps({
            "BTCUSDT": self._make_cache_entry(stale_tiers, cached_at=stale_time),
        }))

        with patch.object(provider, "fetch_from_bybit", return_value=fresh_tiers) as mock_fetch:
            result = provider.get("BTCUSDT", force_fetch=False)

        mock_fetch.assert_called_once_with("BTCUSDT")
        assert result == fresh_tiers

        cache = json.loads(cache_path.read_text())
        cached_tiers = _tiers_from_dict(cache["BTCUSDT"]["tiers"])
        refreshed_at = datetime.fromisoformat(cache["BTCUSDT"]["cached_at"])
        assert cached_tiers == fresh_tiers
        assert refreshed_at > stale_time

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

    # --- save_to_cache edge cases ---

    def test_save_rejects_oversized_cache(self, provider, cache_path, caplog):
        """Rejects cache files exceeding 10MB limit."""
        # Create a 10MB+ cache file
        large_cache = {f"SYM{i}": {
            "tiers": _tiers_to_dict(SAMPLE_TIERS),
            "cached_at": datetime.now(timezone.utc).isoformat(),
        } for i in range(50000)}
        cache_path.write_text(json.dumps(large_cache))

        with caplog.at_level(logging.WARNING):
            # Should not raise — ValueError is caught by save_to_cache wrapper
            provider.save_to_cache("BTCUSDT", SAMPLE_TIERS)

        assert any("exceeds" in r.message and "byte limit" in r.message for r in caplog.records)

    def test_save_to_cache_write_permission_error(self, tmp_path, caplog):
        """save_to_cache logs warning and doesn't crash on read-only directory."""
        read_only_dir = tmp_path / "readonly"
        read_only_dir.mkdir()
        cache_file = read_only_dir / "cache.json"
        read_only_dir.chmod(0o444)

        provider = RiskLimitProvider(cache_path=cache_file)

        with caplog.at_level(logging.WARNING):
            # Should not raise — permission error is caught and logged
            provider.save_to_cache("BTCUSDT", SAMPLE_TIERS)

        # Verify warning was logged
        assert any("Failed to write cache file" in r.message for r in caplog.records)

        # Restore permissions for cleanup, then verify no file was created
        read_only_dir.chmod(0o755)
        assert not cache_file.exists()

    def test_load_from_cache_rejects_symlink_path(self, tmp_path, caplog):
        """Cache reads are rejected when cache_path is a symlink."""
        target = tmp_path / "real_cache.json"
        target.write_text(json.dumps({
            "BTCUSDT": self._make_cache_entry(SAMPLE_TIERS),
        }))
        symlink = tmp_path / "cache_link.json"
        try:
            symlink.symlink_to(target)
        except OSError as e:
            pytest.skip(f"Symlinks not supported in test environment: {e}")

        provider = RiskLimitProvider(cache_path=symlink)
        with caplog.at_level(logging.WARNING):
            result = provider.load_from_cache("BTCUSDT")

        assert result is None
        assert any("must not be a symlink" in r.message for r in caplog.records)

    def test_save_to_cache_rejects_symlink_path(self, tmp_path, caplog):
        """Cache writes are rejected when cache_path is a symlink."""
        target = tmp_path / "real_cache.json"
        target.write_text("{}")
        original = target.read_text()
        symlink = tmp_path / "cache_link.json"
        try:
            symlink.symlink_to(target)
        except OSError as e:
            pytest.skip(f"Symlinks not supported in test environment: {e}")

        provider = RiskLimitProvider(cache_path=symlink)
        with caplog.at_level(logging.WARNING):
            provider.save_to_cache("BTCUSDT", SAMPLE_TIERS)

        assert target.read_text() == original
        assert any("must not be a symlink" in r.message for r in caplog.records)


class TestEdgeCases:
    """Edge case tests for risk limit parsing and caching."""

    def test_negative_risk_limit_value_rejected(self):
        """Negative riskLimitValue raises ValueError."""
        from gridcore.pnl import parse_risk_limit_tiers

        with pytest.raises(ValueError, match="Invalid riskLimitValue"):
            parse_risk_limit_tiers([
                {"riskLimitValue": "-100", "maintenanceMargin": "0.01",
                 "mmDeduction": "0", "initialMargin": "0.02"},
            ])

    def test_zero_risk_limit_value_rejected(self):
        """Zero riskLimitValue raises ValueError."""
        from gridcore.pnl import parse_risk_limit_tiers

        with pytest.raises(ValueError, match="Invalid riskLimitValue"):
            parse_risk_limit_tiers([
                {"riskLimitValue": "0", "maintenanceMargin": "0.01",
                 "mmDeduction": "0", "initialMargin": "0.02"},
            ])

    def test_nan_risk_limit_value_rejected(self):
        """NaN riskLimitValue raises ValueError."""
        from gridcore.pnl import parse_risk_limit_tiers

        with pytest.raises(ValueError, match="Invalid riskLimitValue"):
            parse_risk_limit_tiers([
                {"riskLimitValue": "NaN", "maintenanceMargin": "0.01",
                 "mmDeduction": "0", "initialMargin": "0.02"},
            ])

    def test_malformed_cached_at_returns_stale(self, tmp_path):
        """Malformed cached_at timestamp treats cache as stale."""
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({
            "BTCUSDT": {
                "tiers": _tiers_to_dict(SAMPLE_TIERS),
                "cached_at": "not-a-timestamp",
            },
        }))

        provider = RiskLimitProvider(cache_path=cache_path)
        # _is_cache_fresh should return False for malformed timestamp
        assert provider._is_cache_fresh("BTCUSDT") is False

    def test_empty_tiers_list_returns_none(self, tmp_path):
        """Empty tiers list in cache returns None from load_from_cache."""
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({
            "BTCUSDT": {
                "tiers": [],
                "cached_at": datetime.now(timezone.utc).isoformat(),
            },
        }))

        provider = RiskLimitProvider(cache_path=cache_path)
        result = provider.load_from_cache("BTCUSDT")
        assert result is None

    def test_cache_file_exceeds_size_limit(self, tmp_path):
        """Cache file exceeding 10MB raises ValueError during save."""
        cache_path = tmp_path / "cache.json"
        # Create a file just over 10MB
        cache_path.write_text("x" * 10_000_001)

        provider = RiskLimitProvider(cache_path=cache_path)
        with pytest.raises(ValueError, match="exceeds.*byte limit"):
            provider._save_to_cache_impl("BTCUSDT", SAMPLE_TIERS)

    def test_custom_max_cache_size_enforced(self, tmp_path, caplog):
        """Custom max_cache_size_bytes is enforced on save."""
        cache_path = tmp_path / "cache.json"
        # Create a small cache file that exceeds the custom limit
        cache_path.write_text("x" * 500)

        provider = RiskLimitProvider(
            cache_path=cache_path, max_cache_size_bytes=100
        )

        with caplog.at_level(logging.WARNING):
            # save_to_cache catches ValueError and logs warning
            provider.save_to_cache("BTCUSDT", SAMPLE_TIERS)

        assert any("exceeds" in r.message and "byte limit" in r.message for r in caplog.records)


class TestConcurrentCacheAccess:
    """Tests for concurrent cache file access."""

    def test_concurrent_writes_no_corruption(self, tmp_path):
        """Two threads writing to the same cache file don't corrupt data."""
        cache_path = tmp_path / "cache.json"
        provider = RiskLimitProvider(cache_path=cache_path)
        errors: list[Exception] = []

        tiers_a: MMTiers = [
            (Decimal("100000"), Decimal("0.01"), Decimal("0"), Decimal("0.02")),
            (Decimal("Infinity"), Decimal("0.025"), Decimal("1500"), Decimal("0.05")),
        ]
        tiers_b: MMTiers = [
            (Decimal("500000"), Decimal("0.005"), Decimal("0"), Decimal("0.01")),
            (Decimal("Infinity"), Decimal("0.01"), Decimal("2500"), Decimal("0.02")),
        ]

        def write_symbol(symbol, tiers):
            try:
                for _ in range(20):
                    provider.save_to_cache(symbol, tiers)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=write_symbol, args=("AAAUSDT", tiers_a))
        t2 = threading.Thread(target=write_symbol, args=("BBBUSDT", tiers_b))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # No unhandled exceptions in threads
        assert errors == [], f"Thread errors: {errors}"

        # Cache file should be valid JSON
        cache = json.loads(cache_path.read_text())
        assert isinstance(cache, dict)


class TestPathTraversalValidation:
    """Tests for cache_path resolution to prevent directory traversal."""

    def test_path_with_traversal_is_resolved(self, tmp_path):
        """Path with '..' components is resolved to absolute canonical path."""
        traversal_path = tmp_path / "a" / ".." / "cache.json"
        provider = RiskLimitProvider(cache_path=traversal_path)
        # Should be resolved: tmp_path/cache.json (no ".." in result)
        assert ".." not in str(provider.cache_path)
        assert provider.cache_path == (tmp_path / "cache.json").resolve()

    def test_cache_path_is_always_absolute(self, tmp_path, monkeypatch):
        """Relative cache paths are resolved to absolute."""
        monkeypatch.chdir(tmp_path)
        provider = RiskLimitProvider(cache_path=Path("relative/cache.json"))
        assert provider.cache_path.is_absolute()
