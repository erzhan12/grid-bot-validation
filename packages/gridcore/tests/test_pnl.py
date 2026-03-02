"""Tests for pure PnL calculation functions."""

from decimal import Decimal

import pytest

from gridcore.pnl import (
    calc_unrealised_pnl,
    calc_unrealised_pnl_pct,
    calc_position_value,
    calc_initial_margin,
    calc_liq_ratio,
    calc_maintenance_margin,
    _find_matching_tier,
    parse_risk_limit_tiers,
    MMTiers,
    MM_TIERS_BTCUSDT,
)


class TestCalcUnrealisedPnl:
    """Test unrealized PnL calculation (absolute)."""

    def test_long_profit(self):
        """Long position in profit: current > entry."""
        result = calc_unrealised_pnl("long", Decimal("50000"), Decimal("51000"), Decimal("0.1"))
        assert result == Decimal("100")  # (51000 - 50000) * 0.1

    def test_long_loss(self):
        """Long position in loss: current < entry."""
        result = calc_unrealised_pnl("long", Decimal("50000"), Decimal("49000"), Decimal("0.1"))
        assert result == Decimal("-100")

    def test_short_profit(self):
        """Short position in profit: current < entry."""
        result = calc_unrealised_pnl("short", Decimal("50000"), Decimal("49000"), Decimal("0.1"))
        assert result == Decimal("100")

    def test_short_loss(self):
        """Short position in loss: current > entry."""
        result = calc_unrealised_pnl("short", Decimal("50000"), Decimal("51000"), Decimal("0.1"))
        assert result == Decimal("-100")

    def test_breakeven(self):
        """Position at breakeven: current == entry."""
        result = calc_unrealised_pnl("long", Decimal("50000"), Decimal("50000"), Decimal("0.1"))
        assert result == Decimal("0")

    def test_zero_size(self):
        """Zero size returns zero PnL."""
        result = calc_unrealised_pnl("long", Decimal("50000"), Decimal("51000"), Decimal("0"))
        assert result == Decimal("0")


class TestCalcUnrealisedPnlPct:
    """Test bbu2 ROE formula."""

    def test_long_profit_10x(self):
        """Long 10x leverage, price up 1%."""
        result = calc_unrealised_pnl_pct(
            "long", Decimal("50000"), Decimal("50500"), Decimal("10")
        )
        # (1/50000 - 1/50500) * 50000 * 100 * 10 ≈ 9.90099%
        assert abs(result - Decimal("9.900990099009901")) < Decimal("0.001")

    def test_short_profit_10x(self):
        """Short 10x leverage, price down 1%."""
        result = calc_unrealised_pnl_pct(
            "short", Decimal("50000"), Decimal("49500"), Decimal("10")
        )
        # (1/49500 - 1/50000) * 50000 * 100 * 10 ≈ 10.1010%
        assert abs(result - Decimal("10.10101010101010")) < Decimal("0.001")

    def test_zero_entry(self):
        """Zero entry price returns 0."""
        result = calc_unrealised_pnl_pct(
            "long", Decimal("0"), Decimal("50000"), Decimal("10")
        )
        assert result == Decimal("0")

    def test_zero_current(self):
        """Zero current price returns 0."""
        result = calc_unrealised_pnl_pct(
            "long", Decimal("50000"), Decimal("0"), Decimal("10")
        )
        assert result == Decimal("0")

    def test_1x_leverage(self):
        """1x leverage, long, price up 2%."""
        result = calc_unrealised_pnl_pct(
            "long", Decimal("100"), Decimal("102"), Decimal("1")
        )
        # (1/100 - 1/102) * 100 * 100 * 1 ≈ 1.9608%
        assert abs(result - Decimal("1.96078431372549")) < Decimal("0.001")


class TestCalcPositionValue:
    """Test position value calculation."""

    def test_basic(self):
        """Position value = size * entry_price."""
        result = calc_position_value(Decimal("0.1"), Decimal("50000"))
        assert result == Decimal("5000")

    def test_zero_size(self):
        """Zero size returns zero."""
        result = calc_position_value(Decimal("0"), Decimal("50000"))
        assert result == Decimal("0")

    def test_small_position(self):
        """Small LTC position: 0.1 * 52.81 = 5.281."""
        result = calc_position_value(Decimal("0.1"), Decimal("52.81"))
        assert result == Decimal("5.281")


class TestCalcInitialMargin:
    """Test initial margin calculation."""

    def test_basic(self):
        """Margin = position_value / leverage."""
        result = calc_initial_margin(Decimal("5000"), Decimal("10"))
        assert result == Decimal("500")

    def test_zero_leverage(self):
        """Zero leverage returns zero."""
        result = calc_initial_margin(Decimal("5000"), Decimal("0"))
        assert result == Decimal("0")

    def test_high_leverage(self):
        """100x leverage."""
        result = calc_initial_margin(Decimal("50000"), Decimal("100"))
        assert result == Decimal("500")


class TestCalcLiqRatio:
    """Test liquidation ratio calculation."""

    def test_basic(self):
        """Ratio = liq_price / current_price."""
        result = calc_liq_ratio(Decimal("45000"), Decimal("50000"))
        assert result == 0.9

    def test_zero_current_price(self):
        """Zero current price returns 0.0."""
        result = calc_liq_ratio(Decimal("45000"), Decimal("0"))
        assert result == 0.0

    def test_zero_liq_price(self):
        """Zero liq price returns 0.0."""
        result = calc_liq_ratio(Decimal("0"), Decimal("50000"))
        assert result == 0.0

    def test_liq_above_price(self):
        """Short position: liq price above current."""
        result = calc_liq_ratio(Decimal("55000"), Decimal("50000"))
        assert result == 1.1


class TestFindMatchingTier:
    """Test tier lookup via binary search."""

    def test_first_tier(self):
        """Small position matches first tier."""
        tier = _find_matching_tier(Decimal("100000"), MM_TIERS_BTCUSDT)
        assert tier is not None
        assert tier[0] == Decimal("2000000")
        assert tier[1] == Decimal("0.005")  # mmr_rate

    def test_exact_boundary(self):
        """Position exactly at tier boundary matches that tier."""
        tier = _find_matching_tier(Decimal("2000000"), MM_TIERS_BTCUSDT)
        assert tier is not None
        assert tier[0] == Decimal("2000000")

    def test_above_first_tier(self):
        """Position above first tier matches second tier."""
        tier = _find_matching_tier(Decimal("2000001"), MM_TIERS_BTCUSDT)
        assert tier is not None
        assert tier[0] == Decimal("10000000")
        assert tier[1] == Decimal("0.01")

    def test_infinity_tier(self):
        """Very large position matches Infinity tier."""
        tier = _find_matching_tier(Decimal("999999999"), MM_TIERS_BTCUSDT)
        assert tier is not None
        assert tier[0] == Decimal("Infinity")
        assert tier[1] == Decimal("0.15")

    def test_zero_value(self):
        """Zero position value matches first tier."""
        tier = _find_matching_tier(Decimal("0"), MM_TIERS_BTCUSDT)
        assert tier is not None
        assert tier[0] == Decimal("2000000")


class TestCalcMaintenanceMargin:
    """Test tiered maintenance margin calculation."""

    def test_small_btc_position(self):
        """Small BTC position uses 0.5% tier (first tier, no deduction)."""
        mm, mmr = calc_maintenance_margin(Decimal("10000"), symbol="BTCUSDT")
        # MM = 10000 * 0.005 - 0 = 50
        assert mm == Decimal("50")
        assert mmr == Decimal("0.005")

    def test_large_btc_position(self):
        """Large BTC position hits higher tier with deduction."""
        mm, mmr = calc_maintenance_margin(Decimal("5000000"), symbol="BTCUSDT")
        # Tier: max=10M, mmr=0.01, deduction=10000
        # MM = 5000000 * 0.01 - 10000 = 40000
        assert mm == Decimal("40000")
        assert mmr == Decimal("0.01")

    def test_zero_position(self):
        """Zero position returns zero."""
        mm, mmr = calc_maintenance_margin(Decimal("0"))
        assert mm == Decimal("0")
        assert mmr == Decimal("0")

    def test_negative_position_raises(self):
        """Negative position value raises ValueError."""
        with pytest.raises(ValueError, match="Negative position_value"):
            calc_maintenance_margin(Decimal("-100"))

    def test_unknown_symbol_uses_default(self):
        """Unknown symbol falls back to MM_TIERS_DEFAULT."""
        mm, mmr = calc_maintenance_margin(Decimal("100000"), symbol="XYZUSDT")
        # Default first tier: mmr=0.01, deduction=0
        # MM = 100000 * 0.01 - 0 = 1000
        assert mm == Decimal("1000")
        assert mmr == Decimal("0.01")

    def test_explicit_tiers_override_symbol(self):
        """Explicit tiers parameter overrides symbol lookup."""
        custom_tiers: MMTiers = [
            (Decimal("Infinity"), Decimal("0.02"), Decimal("0"), Decimal("0.04")),
        ]
        mm, mmr = calc_maintenance_margin(Decimal("50000"), symbol="BTCUSDT", tiers=custom_tiers)
        assert mm == Decimal("1000")  # 50000 * 0.02
        assert mmr == Decimal("0.02")

    def test_deduction_clamps_to_zero(self):
        """MM never goes negative (clamped to zero)."""
        # Create tier where deduction > position_value * mmr
        custom_tiers: MMTiers = [
            (Decimal("Infinity"), Decimal("0.005"), Decimal("1000"), Decimal("0.01")),
        ]
        mm, mmr = calc_maintenance_margin(Decimal("100"), tiers=custom_tiers)
        # MM = 100 * 0.005 - 1000 = -999.5 → clamped to 0
        assert mm == Decimal("0")
        assert mmr == Decimal("0.005")


class TestParseRiskLimitTiers:
    """Test Bybit API tier format parsing."""

    def test_basic_parsing(self):
        """Parse minimal valid API tier data."""
        api_tiers = [
            {"riskLimitValue": "200000", "maintenanceMargin": "0.005", "mmDeduction": "0", "initialMargin": "0.01"},
            {"riskLimitValue": "1000000", "maintenanceMargin": "0.01", "mmDeduction": "1000", "initialMargin": "0.02"},
        ]
        result = parse_risk_limit_tiers(api_tiers)
        assert len(result) == 2
        assert result[0] == (Decimal("200000"), Decimal("0.005"), Decimal("0"), Decimal("0.01"))
        # Last tier gets Infinity cap
        assert result[1][0] == Decimal("Infinity")
        assert result[1][1] == Decimal("0.01")

    def test_empty_raises(self):
        """Empty tier list raises ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            parse_risk_limit_tiers([])

    def test_unsorted_input_gets_sorted(self):
        """Out-of-order tiers are sorted by riskLimitValue."""
        api_tiers = [
            {"riskLimitValue": "1000000", "maintenanceMargin": "0.01", "mmDeduction": "1000"},
            {"riskLimitValue": "200000", "maintenanceMargin": "0.005", "mmDeduction": "0"},
        ]
        result = parse_risk_limit_tiers(api_tiers)
        assert result[0][0] == Decimal("200000")
        assert result[1][0] == Decimal("Infinity")

    def test_missing_deduction_defaults_to_zero(self):
        """Missing mmDeduction defaults to 0."""
        api_tiers = [
            {"riskLimitValue": "200000", "maintenanceMargin": "0.005"},
        ]
        result = parse_risk_limit_tiers(api_tiers)
        assert result[0][2] == Decimal("0")  # deduction

    def test_non_dict_elements_raises(self):
        """Non-dict elements raise ValueError, not AttributeError."""
        with pytest.raises(ValueError, match="must contain dict objects"):
            parse_risk_limit_tiers([123, 456])
