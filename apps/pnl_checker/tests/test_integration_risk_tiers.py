"""Integration test: fetch → calculate with dynamic risk limit tiers.

Verifies that BybitFetcher.fetch_all correctly fetches and attaches
risk limit tiers, and that ``calculate`` uses them for IM/MM computation.
"""

from decimal import Decimal
from unittest.mock import MagicMock

from pnl_checker.calculator import calculate
from pnl_checker.fetcher import BybitFetcher
from gridcore.position import RiskConfig


# Dynamic tiers returned by mock Bybit API (2 tiers for simplicity)
_MOCK_RISK_LIMIT_TIERS = [
    {
        "riskLimitValue": "500000",
        "maintenanceMargin": "0.005",
        "mmDeduction": "0",
        "initialMargin": "0.01",
    },
    {
        "riskLimitValue": "2000000",
        "maintenanceMargin": "0.01",
        "mmDeduction": "2500",
        "initialMargin": "0.02",
    },
]


def _make_mock_client():
    """Create a BybitRestClient mock with all endpoints stubbed."""
    client = MagicMock()

    client.get_positions.return_value = [
        {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "size": "0.1",
            "avgPrice": "60000",
            "markPrice": "61000",
            "liqPrice": "54000",
            "leverage": "10",
            "positionValue": "6100",
            "positionIM": "610",
            "positionMM": "30.5",
            "unrealisedPnl": "100",
            "curRealisedPnl": "0",
            "cumRealisedPnl": "0",
            "positionIdx": 1,
        }
    ]

    client.get_tickers.return_value = {
        "lastPrice": "61000",
        "markPrice": "61000",
        "fundingRate": "0.0001",
    }

    client.get_wallet_balance.return_value = {
        "list": [
            {
                "totalEquity": "50000",
                "totalWalletBalance": "49000",
                "totalMarginBalance": "50000",
                "totalAvailableBalance": "48000",
                "totalPerpUPL": "100",
                "totalInitialMargin": "610",
                "totalMaintenanceMargin": "30.5",
                "coin": [
                    {
                        "coin": "USDT",
                        "walletBalance": "49000",
                        "unrealisedPnl": "100",
                        "cumRealisedPnl": "0",
                    }
                ],
            }
        ]
    }

    client.get_transaction_log_all.return_value = (
        [{"funding": "-0.05"}, {"funding": "-0.03"}],
        False,
    )

    client.get_risk_limit.return_value = _MOCK_RISK_LIMIT_TIERS

    return client


class TestEndToEndWithDynamicTiers:
    """Integration: fetch_all → calculate with dynamic risk limit tiers."""

    def test_tiers_passed_through_to_calculator(self):
        """Fetched tiers are attached to SymbolFetchResult and used in calculate."""
        client = _make_mock_client()
        fetcher = BybitFetcher(client)
        fetch_result = fetcher.fetch_all(["BTCUSDT"])

        # Verify tiers were fetched and attached
        assert fetch_result.symbols[0].risk_limit_tiers is not None
        tiers = fetch_result.symbols[0].risk_limit_tiers
        assert len(tiers) == 2
        # Last tier cap should be Infinity (parse_risk_limit_tiers behaviour)
        assert tiers[-1][0] == Decimal("Infinity")

        # Run calculator
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=8.0,
            min_total_margin=0.15,
        )
        calc_result = calculate(fetch_result, risk_config)

        assert len(calc_result.positions) == 1
        pos = calc_result.positions[0]

        # position_value = 0.1 * 60000 = 6000
        assert pos.position_value == Decimal("6000")

        # With dynamic tiers: tier 1 (max 500000, imr=0.01)
        # IM = 6000 * 0.01 = 60
        assert pos.initial_margin == Decimal("60")
        assert pos.imr_rate == Decimal("0.01")

        # MM = 6000 * 0.005 - 0 = 30
        assert pos.maintenance_margin == Decimal("30")
        assert pos.mmr_rate == Decimal("0.005")

    def test_risk_limit_failure_falls_back_to_hardcoded(self):
        """When get_risk_limit fails, calculator uses hardcoded fallback tiers."""
        client = _make_mock_client()
        # Make get_risk_limit raise an exception
        client.get_risk_limit.side_effect = ConnectionError("API unreachable")

        fetcher = BybitFetcher(client)
        fetch_result = fetcher.fetch_all(["BTCUSDT"])

        # risk_limit_tiers should be None (fetch failed gracefully)
        assert fetch_result.symbols[0].risk_limit_tiers is None

        # Calculator should still produce valid results using hardcoded tiers
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=8.0,
            min_total_margin=0.15,
        )
        calc_result = calculate(fetch_result, risk_config)

        assert len(calc_result.positions) == 1
        pos = calc_result.positions[0]

        # position_value = 0.1 * 60000 = 6000
        assert pos.position_value == Decimal("6000")

        # Hardcoded BTCUSDT tier 1: mmr=0.005, imr=0.01 (max 2,000,000)
        # MM = 6000 * 0.005 - 0 = 30
        assert pos.maintenance_margin == Decimal("30")
        assert pos.mmr_rate == Decimal("0.005")

        # IM = 6000 * 0.01 = 60
        assert pos.initial_margin == Decimal("60")
        assert pos.imr_rate == Decimal("0.01")

        # Account-level should still be populated
        assert calc_result.account is not None
        assert calc_result.account.total_im == Decimal("60")
        assert calc_result.account.total_mm == Decimal("30")

    def test_account_level_margins_use_dynamic_tiers(self):
        """Account-level IM/MM aggregation uses tier-based per-position values."""
        client = _make_mock_client()
        fetcher = BybitFetcher(client)
        fetch_result = fetcher.fetch_all(["BTCUSDT"])

        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=8.0,
            min_total_margin=0.15,
        )
        calc_result = calculate(fetch_result, risk_config)

        assert calc_result.account is not None
        # total_im should equal the single position's IM (60)
        assert calc_result.account.total_im == Decimal("60")
        # total_mm should equal the single position's MM (30)
        assert calc_result.account.total_mm == Decimal("30")
