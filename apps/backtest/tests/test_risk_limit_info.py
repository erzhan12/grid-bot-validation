"""Tests for risk limit info provider."""

import gc
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
    DEFAULT_ALLOWED_CACHE_ROOT,
    _tiers_to_dict,
    _tiers_from_dict,
)
from gridcore.pnl import MMTiers, MM_TIERS, parse_risk_limit_tiers


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
        return RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)

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

    def test_load_from_cache_missing_tier_keys_returns_none(self, provider, cache_path, caplog):
        """Missing required tier keys are treated as invalid cache data."""
        cache_path.write_text(json.dumps({
            "BTCUSDT": {
                "tiers": [{"max_value": "200000"}],  # missing mmr_rate/deduction
                "cached_at": datetime.now(timezone.utc).isoformat(),
            },
        }))

        with caplog.at_level(logging.WARNING):
            result = provider.load_from_cache("BTCUSDT")

        assert result is None
        assert any("Invalid cache data for BTCUSDT" in r.message for r in caplog.records)

    def test_load_from_cache_invalid_decimal_returns_none(self, provider, cache_path, caplog):
        """Invalid decimal strings are treated as invalid cache data."""
        cache_path.write_text(json.dumps({
            "BTCUSDT": {
                "tiers": [{
                    "max_value": "200000",
                    "mmr_rate": "not-a-decimal",
                    "deduction": "0",
                    "imr_rate": "0.02",
                }],
                "cached_at": datetime.now(timezone.utc).isoformat(),
            },
        }))

        with caplog.at_level(logging.WARNING):
            result = provider.load_from_cache("BTCUSDT")

        assert result is None
        assert any("Invalid cache data for BTCUSDT" in r.message for r in caplog.records)

    def test_load_from_cache_oversized_logs_size_error(self, provider, cache_path, caplog):
        """Oversized cache file logs size-specific warning and returns None."""
        cache_path.write_text("x" * (provider.max_cache_size_bytes + 1))

        with caplog.at_level(logging.WARNING):
            result = provider.load_from_cache("BTCUSDT")

        assert result is None
        assert any("Cache file exceeds" in r.message for r in caplog.records)

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
        provider = RiskLimitProvider(cache_path=deep_path, allowed_cache_root=None)

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

    def test_save_to_cache_non_dict_root_overwrites(self, provider, cache_path, caplog):
        """Non-dict cache root is treated as corrupted and overwritten."""
        cache_path.write_text(json.dumps([{"unexpected": "list-root"}]))

        with caplog.at_level(logging.WARNING):
            provider.save_to_cache("BTCUSDT", SAMPLE_TIERS)

        cache = json.loads(cache_path.read_text())
        assert isinstance(cache, dict)
        assert "BTCUSDT" in cache
        assert any("Invalid cache root" in r.message for r in caplog.records)

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

        provider = RiskLimitProvider(cache_path=cache_path, rest_client=mock_client, allowed_cache_root=None)
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

        provider = RiskLimitProvider(cache_path=cache_path, rest_client=mock_client, allowed_cache_root=None)
        result = provider.fetch_from_bybit("BTCUSDT")

        assert result is None

    def test_fetch_from_bybit_client_exception(self, cache_path):
        """Returns None when rest_client raises an exception."""
        mock_client = MagicMock()
        mock_client.get_risk_limit.side_effect = ConnectionError("timeout")

        provider = RiskLimitProvider(cache_path=cache_path, rest_client=mock_client, allowed_cache_root=None)
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

        provider = RiskLimitProvider(cache_path=cache_file, allowed_cache_root=None)

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

        provider = RiskLimitProvider(cache_path=symlink, allowed_cache_root=None)
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

        provider = RiskLimitProvider(cache_path=symlink, allowed_cache_root=None)
        with caplog.at_level(logging.WARNING):
            provider.save_to_cache("BTCUSDT", SAMPLE_TIERS)

        assert target.read_text() == original
        assert any("must not be a symlink" in r.message for r in caplog.records)

    def test_save_to_cache_rejects_symlink_lock_path(self, tmp_path, caplog):
        """Cache writes are rejected when sidecar lock path is a symlink."""
        cache_path = tmp_path / "cache.json"
        lock_target = tmp_path / "real_lock_target"
        lock_target.write_text("lock")
        lock_symlink = cache_path.with_suffix(f"{cache_path.suffix}.lock")
        try:
            lock_symlink.symlink_to(lock_target)
        except OSError as e:
            pytest.skip(f"Symlinks not supported in test environment: {e}")

        provider = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
        with caplog.at_level(logging.WARNING):
            provider.save_to_cache("BTCUSDT", SAMPLE_TIERS)

        assert not cache_path.exists()
        assert any("Cache lock path must not be a symlink" in r.message for r in caplog.records)

    def test_close_makes_provider_unusable(self, tmp_path):
        """A closed provider raises RuntimeError on further use."""
        cache_path = tmp_path / "cache.json"
        provider = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
        provider.save_to_cache("BTCUSDT", SAMPLE_TIERS)

        provider.close()

        with pytest.raises(RuntimeError, match="closed"):
            provider.load_from_cache("BTCUSDT")
        with pytest.raises(RuntimeError, match="closed"):
            provider.save_to_cache("ETHUSDT", SAMPLE_TIERS)
        with pytest.raises(RuntimeError, match="closed"):
            provider.get("BTCUSDT")


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

    def test_api_returns_infinity_last_tier(self):
        """parse_risk_limit_tiers handles last tier already having riskLimitValue='Infinity'."""
        from gridcore.pnl import parse_risk_limit_tiers

        api_tiers = [
            {"riskLimitValue": "200000", "maintenanceMargin": "0.01",
             "mmDeduction": "0", "initialMargin": "0.02"},
            {"riskLimitValue": "Infinity", "maintenanceMargin": "0.025",
             "mmDeduction": "3000", "initialMargin": "0.05"},
        ]
        result = parse_risk_limit_tiers(api_tiers)

        assert len(result) == 2
        assert result[0] == (Decimal("200000"), Decimal("0.01"), Decimal("0"), Decimal("0.02"))
        # Last tier should keep Infinity without any issue
        assert result[1] == (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0.05"))
        assert result[-1][0] == Decimal("Infinity")

    def test_malformed_cached_at_returns_stale(self, tmp_path):
        """Malformed cached_at timestamp treats cache as stale."""
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({
            "BTCUSDT": {
                "tiers": _tiers_to_dict(SAMPLE_TIERS),
                "cached_at": "not-a-timestamp",
            },
        }))

        provider = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
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

        provider = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
        result = provider.load_from_cache("BTCUSDT")
        assert result is None

    def test_cache_file_exceeds_size_limit(self, tmp_path):
        """Cache file exceeding 10MB raises ValueError during save."""
        cache_path = tmp_path / "cache.json"
        # Create a file just over 10MB
        cache_path.write_text("x" * 10_000_001)

        provider = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
        with pytest.raises(ValueError, match="exceeds.*byte limit"):
            provider._save_to_cache_impl("BTCUSDT", SAMPLE_TIERS)

    def test_custom_max_cache_size_enforced(self, tmp_path, caplog):
        """Custom max_cache_size_bytes is enforced on save."""
        cache_path = tmp_path / "cache.json"
        # Create a small cache file that exceeds the custom limit
        cache_path.write_text("x" * 500)

        provider = RiskLimitProvider(
            cache_path=cache_path, max_cache_size_bytes=100, allowed_cache_root=None
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
        provider = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
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

    def test_concurrent_writes_across_instances_no_corruption(self, tmp_path):
        """Two provider instances sharing one cache path coordinate writes safely."""
        cache_path = tmp_path / "cache.json"
        provider_a = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
        provider_b = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
        errors: list[Exception] = []

        tiers_a: MMTiers = [
            (Decimal("100000"), Decimal("0.01"), Decimal("0"), Decimal("0.02")),
            (Decimal("Infinity"), Decimal("0.025"), Decimal("1500"), Decimal("0.05")),
        ]
        tiers_b: MMTiers = [
            (Decimal("500000"), Decimal("0.005"), Decimal("0"), Decimal("0.01")),
            (Decimal("Infinity"), Decimal("0.01"), Decimal("2500"), Decimal("0.02")),
        ]

        def write_symbol(provider, symbol, tiers):
            try:
                for _ in range(20):
                    provider.save_to_cache(symbol, tiers)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=write_symbol, args=(provider_a, "AAAUSDT", tiers_a))
        t2 = threading.Thread(target=write_symbol, args=(provider_b, "BBBUSDT", tiers_b))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == [], f"Thread errors: {errors}"
        cache = json.loads(cache_path.read_text())
        assert "AAAUSDT" in cache
        assert "BBBUSDT" in cache

    def test_concurrent_cache_writes(self, tmp_path):
        """Multiple provider instances writing concurrently produce a valid, non-corrupt cache."""
        cache_path = tmp_path / "cache.json"
        num_providers = 4
        writes_per_provider = 15
        providers = [RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None) for _ in range(num_providers)]
        errors: list[Exception] = []

        all_tiers: list[MMTiers] = [
            [
                (Decimal(str(100000 * (i + 1))), Decimal("0.01"), Decimal("0"), Decimal("0.02")),
                (Decimal("Infinity"), Decimal("0.025"), Decimal("1500"), Decimal("0.05")),
            ]
            for i in range(num_providers)
        ]

        def write_for_provider(idx: int):
            try:
                symbol = f"SYM{idx}USDT"
                for _ in range(writes_per_provider):
                    providers[idx].save_to_cache(symbol, all_tiers[idx])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_for_provider, args=(i,)) for i in range(num_providers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"

        # Cache must be valid JSON with all symbols present
        cache = json.loads(cache_path.read_text())
        assert isinstance(cache, dict)
        for i in range(num_providers):
            symbol = f"SYM{i}USDT"
            assert symbol in cache, f"{symbol} missing from cache"
            assert isinstance(cache[symbol]["tiers"], list)
            assert len(cache[symbol]["tiers"]) == 2

    def test_lock_registry_released_when_instances_deleted(self, tmp_path):
        """In-process lock entry is cleaned up when providers are deleted."""
        import backtest.risk_limit_info as risk_limit_info_module

        cache_path = tmp_path / "cache.json"
        key = str(cache_path.resolve())
        provider_a = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
        provider_b = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)

        assert key in risk_limit_info_module._IN_PROCESS_LOCKS
        assert risk_limit_info_module._IN_PROCESS_LOCKS[key][1] >= 2

        del provider_a
        del provider_b
        gc.collect()

        assert key not in risk_limit_info_module._IN_PROCESS_LOCKS

    def test_close_then_new_provider_keeps_lock_registry_consistent(self, tmp_path):
        """Closed providers cannot be reused; new providers share one lock entry."""
        import backtest.risk_limit_info as risk_limit_info_module

        cache_path = tmp_path / "cache.json"
        key = str(cache_path.resolve())
        provider_a = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
        provider_b = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
        assert key in risk_limit_info_module._IN_PROCESS_LOCKS
        assert risk_limit_info_module._IN_PROCESS_LOCKS[key][1] >= 2

        provider_a.close()
        assert risk_limit_info_module._IN_PROCESS_LOCKS[key][1] == 1

        with pytest.raises(RuntimeError, match="closed"):
            provider_a.save_to_cache("AAAUSDT", SAMPLE_TIERS)

        provider_c = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
        assert risk_limit_info_module._IN_PROCESS_LOCKS[key][1] == 2

        provider_b.save_to_cache("BBBUSDT", SAMPLE_TIERS)
        provider_c.save_to_cache("CCCUSDT", SAMPLE_TIERS)

        cache = json.loads(cache_path.read_text())
        assert "BBBUSDT" in cache
        assert "CCCUSDT" in cache


    def test_concurrent_reads_with_one_write_no_corruption(self, tmp_path):
        """Multiple readers with one writer don't corrupt data or raise errors."""
        cache_path = tmp_path / "cache.json"
        # Seed the cache with initial data
        cache_path.write_text(json.dumps({
            "BTCUSDT": {
                "tiers": _tiers_to_dict(SAMPLE_TIERS),
                "cached_at": datetime.now(timezone.utc).isoformat(),
            },
        }))
        provider = RiskLimitProvider(cache_path=cache_path, allowed_cache_root=None)
        errors: list[Exception] = []

        write_tiers: MMTiers = [
            (Decimal("500000"), Decimal("0.005"), Decimal("0"), Decimal("0.01")),
            (Decimal("Infinity"), Decimal("0.01"), Decimal("2500"), Decimal("0.02")),
        ]

        def reader(symbol: str):
            try:
                for _ in range(30):
                    result = provider.load_from_cache(symbol)
                    # Result is either valid tiers or None (never corrupt)
                    if result is not None:
                        assert isinstance(result, list)
                        assert all(len(t) == 4 for t in result)
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for _ in range(30):
                    provider.save_to_cache("ETHUSDT", write_tiers)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=reader, args=("BTCUSDT",)),
            threading.Thread(target=reader, args=("BTCUSDT",)),
            threading.Thread(target=reader, args=("ETHUSDT",)),
            threading.Thread(target=writer),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"

        # Cache file should be valid JSON with both symbols
        cache = json.loads(cache_path.read_text())
        assert isinstance(cache, dict)
        assert "BTCUSDT" in cache
        assert "ETHUSDT" in cache


class TestMaxCacheSizeValidation:
    """Tests for max_cache_size_bytes constructor validation."""

    def test_zero_max_cache_size_means_no_limit(self, tmp_path):
        """max_cache_size_bytes=0 is accepted as 'no size limit'."""
        provider = RiskLimitProvider(cache_path=tmp_path / "c.json", max_cache_size_bytes=0, allowed_cache_root=None)
        assert provider.max_cache_size_bytes == 0

    def test_negative_max_cache_size_raises(self, tmp_path):
        """Negative max_cache_size_bytes raises ValueError."""
        with pytest.raises(ValueError, match="max_cache_size_bytes must be non-negative"):
            RiskLimitProvider(cache_path=tmp_path / "c.json", max_cache_size_bytes=-100, allowed_cache_root=None)

    def test_positive_max_cache_size_accepted(self, tmp_path):
        """Positive max_cache_size_bytes is accepted."""
        provider = RiskLimitProvider(cache_path=tmp_path / "c.json", max_cache_size_bytes=1, allowed_cache_root=None)
        assert provider.max_cache_size_bytes == 1


class TestPathTraversalValidation:
    """Tests for cache_path resolution to prevent directory traversal."""

    def test_path_with_traversal_is_resolved(self, tmp_path):
        """Path with '..' components is resolved to absolute canonical path."""
        traversal_path = tmp_path / "a" / ".." / "cache.json"
        provider = RiskLimitProvider(cache_path=traversal_path, allowed_cache_root=None)
        # Should be resolved: tmp_path/cache.json (no ".." in result)
        assert ".." not in str(provider.cache_path)
        assert provider.cache_path == (tmp_path / "cache.json").resolve()

    def test_cache_path_is_always_absolute(self, tmp_path, monkeypatch):
        """Relative cache paths are resolved to absolute."""
        monkeypatch.chdir(tmp_path)
        provider = RiskLimitProvider(cache_path=Path("relative/cache.json"), allowed_cache_root=None)
        assert provider.cache_path.is_absolute()

    def test_path_outside_allowed_root_rejected(self, tmp_path):
        """Cache path outside allowed_cache_root raises ValueError."""
        allowed_root = tmp_path / "allowed"
        allowed_root.mkdir()
        outside_path = tmp_path / "outside" / "cache.json"
        with pytest.raises(ValueError, match="outside allowed directory"):
            RiskLimitProvider(cache_path=outside_path, allowed_cache_root=allowed_root)

    def test_path_inside_allowed_root_accepted(self, tmp_path):
        """Cache path inside allowed_cache_root is accepted."""
        allowed_root = tmp_path / "allowed"
        allowed_root.mkdir()
        inside_path = allowed_root / "cache.json"
        provider = RiskLimitProvider(cache_path=inside_path, allowed_cache_root=allowed_root)
        assert provider.cache_path == inside_path.resolve()

    def test_default_allowed_root_is_conf_dir(self):
        """Default allowed_cache_root matches the conf/ directory."""
        assert DEFAULT_ALLOWED_CACHE_ROOT == DEFAULT_CACHE_PATH.parent


class TestFullFallbackChainIntegration:
    """Integration test verifying the complete fallback chain in one scenario."""

    def test_full_flow_api_cache_stale_fallback(self, tmp_path):
        """Verify: API→cache→cache hit→force_fetch bypass→stale cache fallback.

        Steps:
        1. API returns tiers → tiers are cached
        2. Subsequent call uses cache (no API call)
        3. force_fetch=True bypasses cache and calls API
        4. When API fails, stale cache is used as fallback
        """
        cache_path = tmp_path / "cache.json"

        api_tiers_raw = [
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
        expected_parsed: MMTiers = [
            (Decimal("200000"), Decimal("0.01"), Decimal("0"), Decimal("0.02")),
            (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0.05")),
        ]

        mock_client = MagicMock()
        mock_client.get_risk_limit.return_value = api_tiers_raw

        provider = RiskLimitProvider(
            cache_path=cache_path,
            rest_client=mock_client,
            cache_ttl=timedelta(hours=24),
            allowed_cache_root=None,
        )

        # Step 1: First call — cache is empty, should hit API and cache result
        result = provider.get("BTCUSDT")
        assert result == expected_parsed
        mock_client.get_risk_limit.assert_called_once_with(symbol="BTCUSDT")
        assert cache_path.exists()
        cached_data = json.loads(cache_path.read_text())
        assert "BTCUSDT" in cached_data

        # Step 2: Second call — cache is fresh, should NOT call API again
        mock_client.reset_mock()
        result2 = provider.get("BTCUSDT")
        assert result2 == expected_parsed
        mock_client.get_risk_limit.assert_not_called()

        # Step 3: force_fetch=True — should bypass cache and call API
        mock_client.reset_mock()
        result3 = provider.get("BTCUSDT", force_fetch=True)
        assert result3 == expected_parsed
        mock_client.get_risk_limit.assert_called_once_with(symbol="BTCUSDT")

        # Step 4: Make cache stale, then make API fail → should use stale cache
        stale_time = datetime.now(timezone.utc) - timedelta(hours=25)
        cached_data["BTCUSDT"]["cached_at"] = stale_time.isoformat()
        cache_path.write_text(json.dumps(cached_data))

        mock_client.reset_mock()
        mock_client.get_risk_limit.side_effect = ConnectionError("API down")
        result4 = provider.get("BTCUSDT")
        # Should fall back to stale cache rather than hardcoded
        assert result4 == expected_parsed
        mock_client.get_risk_limit.assert_called_once_with(symbol="BTCUSDT")


class TestMalformedApiResponses:
    """Tests for malformed API response handling in parse_risk_limit_tiers."""

    def test_non_list_input_raises(self):
        """parse_risk_limit_tiers rejects non-list input."""
        with pytest.raises(TypeError):
            parse_risk_limit_tiers({"not": "a list"})

    def test_missing_maintenance_margin_raises(self):
        """Missing maintenanceMargin field raises ValueError."""
        with pytest.raises(ValueError, match="maintenanceMargin"):
            parse_risk_limit_tiers([{"riskLimitValue": "1000000"}])

    def test_missing_risk_limit_value_raises(self):
        """Missing riskLimitValue field raises ValueError."""
        with pytest.raises(ValueError, match="riskLimitValue"):
            parse_risk_limit_tiers([{"maintenanceMargin": "0.01"}])

    def test_invalid_mmr_format_raises(self):
        """Non-numeric maintenanceMargin raises ValueError."""
        with pytest.raises(ValueError):
            parse_risk_limit_tiers([{
                "riskLimitValue": "1000000",
                "maintenanceMargin": "not_a_number",
                "initialMargin": "0.01",
            }])

    def test_mmr_out_of_range_raises(self):
        """MMR rate outside [0, 1] raises ValueError."""
        with pytest.raises(ValueError, match="outside valid range"):
            parse_risk_limit_tiers([{
                "riskLimitValue": "1000000",
                "maintenanceMargin": "1.5",
                "initialMargin": "2.0",
            }])

    def test_negative_deduction_raises(self):
        """Negative mmDeduction raises ValueError."""
        with pytest.raises(ValueError, match="negative"):
            parse_risk_limit_tiers([{
                "riskLimitValue": "1000000",
                "maintenanceMargin": "0.01",
                "mmDeduction": "-100",
                "initialMargin": "0.02",
            }])

    def test_imr_out_of_range_raises(self):
        """IMR rate outside [0, 1] raises ValueError."""
        with pytest.raises(ValueError, match="outside valid range"):
            parse_risk_limit_tiers([{
                "riskLimitValue": "1000000",
                "maintenanceMargin": "0.01",
                "initialMargin": "1.5",
            }])

    def test_empty_deduction_defaults_to_zero(self):
        """Empty mmDeduction string defaults to 0."""
        result = parse_risk_limit_tiers([{
            "riskLimitValue": "1000000",
            "maintenanceMargin": "0.005",
            "mmDeduction": "",
            "initialMargin": "0.01",
        }])
        assert result[0][2] == Decimal("0")

    def test_missing_deduction_defaults_to_zero(self):
        """Missing mmDeduction key defaults to 0."""
        result = parse_risk_limit_tiers([{
            "riskLimitValue": "1000000",
            "maintenanceMargin": "0.005",
            "initialMargin": "0.01",
        }])
        assert result[0][2] == Decimal("0")

    def test_missing_initial_margin_defaults_to_double_mmr(self):
        """Missing initialMargin defaults to 2x maintenanceMargin."""
        result = parse_risk_limit_tiers([{
            "riskLimitValue": "1000000",
            "maintenanceMargin": "0.005",
        }])
        assert result[0][3] == Decimal("0.01")

    def test_duplicate_boundaries_raises(self):
        """Duplicate riskLimitValue boundaries raise ValueError."""
        with pytest.raises(ValueError, match="[Dd]uplicate"):
            parse_risk_limit_tiers([
                {
                    "riskLimitValue": "1000000",
                    "maintenanceMargin": "0.005",
                    "initialMargin": "0.01",
                },
                {
                    "riskLimitValue": "1000000",
                    "maintenanceMargin": "0.01",
                    "initialMargin": "0.02",
                },
            ])

    def test_single_tier_gets_infinity_cap(self):
        """A single valid tier gets Infinity as its cap."""
        result = parse_risk_limit_tiers([{
            "riskLimitValue": "999999999",
            "maintenanceMargin": "0.005",
            "initialMargin": "0.01",
        }])
        assert len(result) == 1
        assert result[0][0] == Decimal("Infinity")


class TestCacheCorruptionRecovery:
    """Tests for cache corruption recovery scenarios."""

    @pytest.fixture
    def cache_path(self, tmp_path):
        return tmp_path / "cache.json"

    def test_corrupt_cache_recovers_on_next_save(self, cache_path):
        """Corrupt cache file is overwritten on next successful save."""
        cache_path.write_text("{{{{not json at all")

        mock_client = MagicMock()
        mock_client.get_risk_limit.return_value = [
            {
                "riskLimitValue": "500000",
                "maintenanceMargin": "0.005",
                "mmDeduction": "0",
                "initialMargin": "0.01",
            },
            {
                "riskLimitValue": "999999999",
                "maintenanceMargin": "0.01",
                "mmDeduction": "2500",
                "initialMargin": "0.02",
            },
        ]

        provider = RiskLimitProvider(
            cache_path=cache_path, rest_client=mock_client, allowed_cache_root=None,
        )
        result = provider.get("BTCUSDT")

        assert len(result) == 2
        # Cache should now be valid JSON
        cached = json.loads(cache_path.read_text())
        assert "BTCUSDT" in cached

    def test_truncated_json_falls_back_gracefully(self, cache_path):
        """Truncated JSON cache falls back to API or hardcoded."""
        cache_path.write_text('{"BTCUSDT": {"tiers": [')

        provider = RiskLimitProvider(
            cache_path=cache_path, rest_client=None, allowed_cache_root=None,
        )
        result = provider.get("BTCUSDT")

        # Should fall back to hardcoded
        assert result == MM_TIERS["BTCUSDT"]

    def test_non_dict_root_recovery(self, cache_path):
        """Cache file with non-dict root (e.g. a list) is treated as corrupt."""
        cache_path.write_text('[1, 2, 3]')

        provider = RiskLimitProvider(
            cache_path=cache_path, rest_client=None, allowed_cache_root=None,
        )
        result = provider.get("BTCUSDT")

        # Should fall back to hardcoded
        assert result == MM_TIERS["BTCUSDT"]

    def test_wrong_type_entry_ignored(self, cache_path):
        """Cache entry that's not a dict is ignored."""
        cache_path.write_text(json.dumps({"BTCUSDT": "not a dict"}))

        provider = RiskLimitProvider(
            cache_path=cache_path, rest_client=None, allowed_cache_root=None,
        )
        result = provider.get("BTCUSDT")

        # Should fall back to hardcoded
        assert result == MM_TIERS["BTCUSDT"]

    def test_missing_tiers_key_falls_back(self, cache_path):
        """Cache entry without 'tiers' key falls back."""
        cache_path.write_text(json.dumps({
            "BTCUSDT": {"cached_at": "2024-01-01T00:00:00+00:00"},
        }))

        provider = RiskLimitProvider(
            cache_path=cache_path, rest_client=None, allowed_cache_root=None,
        )
        result = provider.get("BTCUSDT")

        # Should fall back to hardcoded
        assert result == MM_TIERS["BTCUSDT"]
