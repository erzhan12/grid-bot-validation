"""Integration tests for risk limit tier fallback chain and cache TTL."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from backtest.risk_limit_info import RiskLimitProvider, _tiers_to_dict
from gridcore.pnl import MMTiers, MM_TIERS, MM_TIERS_DEFAULT


# Realistic API tiers (different from hardcoded to verify which source is used)
API_TIERS: MMTiers = [
    (Decimal("500000"), Decimal("0.005"), Decimal("0"), Decimal("0.01")),
    (Decimal("Infinity"), Decimal("0.01"), Decimal("2500"), Decimal("0.02")),
]


class TestFallbackChain:
    """Verify full fallback: API -> cache -> hardcoded."""

    @pytest.fixture
    def cache_path(self, tmp_path):
        return tmp_path / "cache.json"

    def _mock_client(self, tiers=None, error=None):
        """Create mock BybitRestClient."""
        client = MagicMock()
        if error:
            client.get_risk_limit.side_effect = error
        elif tiers is not None:
            client.get_risk_limit.return_value = [
                {
                    "riskLimitValue": str(t[0]) if t[0] != Decimal("Infinity") else "999999999",
                    "maintenanceMargin": str(t[1]),
                    "mmDeduction": str(t[2]),
                    "initialMargin": str(t[3]),
                }
                for t in tiers
            ]
        else:
            client.get_risk_limit.return_value = []
        return client

    def test_api_success_returns_api_tiers(self, cache_path):
        """When API succeeds, returns API tiers and caches them."""
        client = self._mock_client(tiers=API_TIERS)
        provider = RiskLimitProvider(cache_path=cache_path, rest_client=client, allowed_cache_root=None)

        result = provider.get("BTCUSDT")

        assert len(result) == 2
        assert result[0][0] == Decimal("500000")
        # Verify tiers were cached
        assert cache_path.exists()

    def test_api_failure_falls_back_to_cache(self, cache_path):
        """When API fails but cache exists, returns cached tiers."""
        # Pre-populate cache
        cache_path.write_text(json.dumps({
            "BTCUSDT": {
                "tiers": _tiers_to_dict(API_TIERS),
                "cached_at": datetime.now(timezone.utc).isoformat(),
            },
        }))

        client = self._mock_client(error=ConnectionError("timeout"))
        provider = RiskLimitProvider(cache_path=cache_path, rest_client=client, allowed_cache_root=None)

        result = provider.get("BTCUSDT", force_fetch=True)

        assert len(result) == 2
        assert result[0][0] == Decimal("500000")

    def test_api_and_cache_failure_falls_back_to_hardcoded(self, cache_path):
        """When both API and cache fail, returns hardcoded tiers."""
        client = self._mock_client(error=ConnectionError("timeout"))
        provider = RiskLimitProvider(cache_path=cache_path, rest_client=client, allowed_cache_root=None)

        result = provider.get("BTCUSDT")

        # Should match hardcoded BTCUSDT tiers
        assert result == MM_TIERS["BTCUSDT"]
        assert len(result) == 7

    def test_unknown_symbol_falls_back_to_default_hardcoded(self, cache_path):
        """Unknown symbol with no API/cache gets default hardcoded tiers."""
        client = self._mock_client(error=ConnectionError("timeout"))
        provider = RiskLimitProvider(cache_path=cache_path, rest_client=client, allowed_cache_root=None)

        result = provider.get("XYZUSDT")

        assert result == MM_TIERS_DEFAULT
        assert len(result) == 5

    def test_no_client_configured_uses_cache(self, cache_path):
        """When no rest_client configured, API step is skipped."""
        cache_path.write_text(json.dumps({
            "BTCUSDT": {
                "tiers": _tiers_to_dict(API_TIERS),
                "cached_at": datetime.now(timezone.utc).isoformat(),
            },
        }))

        provider = RiskLimitProvider(cache_path=cache_path, rest_client=None, allowed_cache_root=None)
        result = provider.get("BTCUSDT")

        assert len(result) == 2


class TestCacheTTL:
    """Verify cache TTL (time-to-live) behavior."""

    @pytest.fixture
    def cache_path(self, tmp_path):
        return tmp_path / "cache.json"

    def test_fresh_cache_skips_api(self, cache_path):
        """Fresh cache (within TTL) skips API call."""
        cache_path.write_text(json.dumps({
            "BTCUSDT": {
                "tiers": _tiers_to_dict(API_TIERS),
                "cached_at": datetime.now(timezone.utc).isoformat(),
            },
        }))

        client = MagicMock()
        provider = RiskLimitProvider(cache_path=cache_path, rest_client=client, allowed_cache_root=None)
        result = provider.get("BTCUSDT")

        client.get_risk_limit.assert_not_called()
        assert len(result) == 2

    def test_stale_cache_triggers_api_refresh(self, cache_path):
        """Stale cache (past TTL) triggers API fetch."""
        stale_time = datetime.now(timezone.utc) - timedelta(hours=25)
        cache_path.write_text(json.dumps({
            "BTCUSDT": {
                "tiers": _tiers_to_dict(API_TIERS),
                "cached_at": stale_time.isoformat(),
            },
        }))

        fresh_tiers: MMTiers = [
            (Decimal("300000"), Decimal("0.008"), Decimal("0"), Decimal("0.015")),
            (Decimal("Infinity"), Decimal("0.015"), Decimal("2100"), Decimal("0.03")),
        ]

        client = MagicMock()
        client.get_risk_limit.return_value = [
            {
                "riskLimitValue": "300000",
                "maintenanceMargin": "0.008",
                "mmDeduction": "0",
                "initialMargin": "0.015",
            },
            {
                "riskLimitValue": "999999999",
                "maintenanceMargin": "0.015",
                "mmDeduction": "2100",
                "initialMargin": "0.03",
            },
        ]

        provider = RiskLimitProvider(cache_path=cache_path, rest_client=client, allowed_cache_root=None)
        result = provider.get("BTCUSDT")

        client.get_risk_limit.assert_called_once_with(symbol="BTCUSDT")
        assert result[0][0] == Decimal("300000")

    def test_stale_cache_used_when_api_fails(self, cache_path):
        """Stale cache is used as fallback when API refresh fails."""
        stale_time = datetime.now(timezone.utc) - timedelta(hours=25)
        cache_path.write_text(json.dumps({
            "BTCUSDT": {
                "tiers": _tiers_to_dict(API_TIERS),
                "cached_at": stale_time.isoformat(),
            },
        }))

        client = MagicMock()
        client.get_risk_limit.side_effect = ConnectionError("unreachable")

        provider = RiskLimitProvider(cache_path=cache_path, rest_client=client, allowed_cache_root=None)
        result = provider.get("BTCUSDT")

        # Stale cache preferred over hardcoded
        assert len(result) == 2
        assert result[0][0] == Decimal("500000")

    def test_custom_ttl_respected(self, cache_path):
        """Custom cache_ttl is respected for freshness check."""
        # Cache 2 hours old
        cached_time = datetime.now(timezone.utc) - timedelta(hours=2)
        cache_path.write_text(json.dumps({
            "BTCUSDT": {
                "tiers": _tiers_to_dict(API_TIERS),
                "cached_at": cached_time.isoformat(),
            },
        }))

        client = MagicMock()

        # With 1-hour TTL, cache should be stale
        provider = RiskLimitProvider(
            cache_path=cache_path, rest_client=client,
            cache_ttl=timedelta(hours=1),
            allowed_cache_root=None,
        )
        # API returns empty → falls back to stale cache
        client.get_risk_limit.return_value = []
        result = provider.get("BTCUSDT")

        client.get_risk_limit.assert_called_once()
        assert len(result) == 2  # stale cache used as fallback
