"""Tests for PnL calculator."""

from decimal import Decimal

from pnl_checker.calculator import (
    _detect_risk_rule,
    calculate,
)
from pnl_checker.fetcher import (
    FetchResult,
    SymbolFetchResult,
    PositionData,
    TickerData,
    FundingData,
    WalletData,
)
from gridcore.position import RiskConfig


class TestDetectRiskRule:
    """Test risk rule detection from multipliers."""

    def test_none(self):
        assert _detect_risk_rule({"Buy": 1.0, "Sell": 1.0}) == "none"

    def test_high_liq_sell(self):
        assert "high_liq_risk" in _detect_risk_rule({"Buy": 1.0, "Sell": 1.5})

    def test_high_liq_buy(self):
        assert "high_liq_risk" in _detect_risk_rule({"Buy": 1.5, "Sell": 1.0})

    def test_buy_double(self):
        assert "position_ratio" in _detect_risk_rule({"Buy": 2.0, "Sell": 1.0})


class TestCalculate:
    """Test full calculation pipeline."""

    def _make_fetch_result(self) -> FetchResult:
        """Create a sample FetchResult for testing."""
        long_pos = PositionData(
            symbol="BTCUSDT",
            side="Buy",
            size=Decimal("0.01"),
            avg_price=Decimal("50000"),
            mark_price=Decimal("51000"),
            liq_price=Decimal("45000"),
            leverage=Decimal("10"),
            position_value=Decimal("510"),
            position_im=Decimal("51"),
            position_mm=Decimal("5.1"),
            unrealised_pnl=Decimal("10"),
            cur_realised_pnl=Decimal("0"),
            cum_realised_pnl=Decimal("0"),
            position_idx=1,
        )
        short_pos = PositionData(
            symbol="BTCUSDT",
            side="Sell",
            size=Decimal("0.01"),
            avg_price=Decimal("52000"),
            mark_price=Decimal("51000"),
            liq_price=Decimal("58000"),
            leverage=Decimal("10"),
            position_value=Decimal("510"),
            position_im=Decimal("51"),
            position_mm=Decimal("5.1"),
            unrealised_pnl=Decimal("10"),
            cur_realised_pnl=Decimal("0"),
            cum_realised_pnl=Decimal("0"),
            position_idx=2,
        )

        return FetchResult(
            symbols=[
                SymbolFetchResult(
                    symbol="BTCUSDT",
                    positions=[long_pos, short_pos],
                    ticker=TickerData(
                        symbol="BTCUSDT",
                        last_price=Decimal("51000"),
                        mark_price=Decimal("51000"),
                        funding_rate=Decimal("0.0001"),
                    ),
                    funding=FundingData(
                        symbol="BTCUSDT",
                        cumulative_funding=Decimal("-0.05"),
                        transaction_count=5,
                    ),
                ),
            ],
            wallet=WalletData(
                total_equity=Decimal("10000"),
                total_wallet_balance=Decimal("9980"),
                total_margin_balance=Decimal("10000"),
                total_available_balance=Decimal("9900"),
                total_perp_upl=Decimal("20"),
                total_initial_margin=Decimal("102"),
                total_maintenance_margin=Decimal("10.2"),
                usdt_wallet_balance=Decimal("9980"),
                usdt_unrealised_pnl=Decimal("20"),
                usdt_cum_realised_pnl=Decimal("0"),
            ),
        )

    def test_calculate_returns_two_positions(self):
        """Calculate should return results for both long and short."""
        fetch = self._make_fetch_result()
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=8.0,
            min_total_margin=0.15,
        )
        result = calculate(fetch, risk_config)
        assert len(result.positions) == 2

    def test_long_unrealised_pnl_mark(self):
        """Long PnL at mark price: (51000 - 50000) * 0.01 = 10."""
        fetch = self._make_fetch_result()
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=8.0,
            min_total_margin=0.15,
        )
        result = calculate(fetch, risk_config)
        long_calc = next(p for p in result.positions if p.direction == "long")
        assert long_calc.unrealised_pnl_mark == Decimal("10")

    def test_short_unrealised_pnl_mark(self):
        """Short PnL at mark price: (52000 - 51000) * 0.01 = 10."""
        fetch = self._make_fetch_result()
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=8.0,
            min_total_margin=0.15,
        )
        result = calculate(fetch, risk_config)
        short_calc = next(p for p in result.positions if p.direction == "short")
        assert short_calc.unrealised_pnl_mark == Decimal("10")

    def test_funding_snapshot(self):
        """Funding snapshot: 0.01 * 51000 * 0.0001 = 0.051."""
        fetch = self._make_fetch_result()
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=8.0,
            min_total_margin=0.15,
        )
        result = calculate(fetch, risk_config)
        long_calc = next(p for p in result.positions if p.direction == "long")
        # 0.01 * 51000 * 0.0001 = 0.051 (size * mark * rate = funding per 8h)
        assert long_calc.funding_snapshot == Decimal("0.051")
