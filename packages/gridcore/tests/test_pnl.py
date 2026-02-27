"""Tests for pure PnL calculation functions."""

import pytest
from decimal import Decimal

from gridcore.pnl import (
    calc_unrealised_pnl,
    calc_unrealised_pnl_pct,
    calc_position_value,
    calc_initial_margin,
    calc_liq_ratio,
    calc_maintenance_margin,
    calc_imr_pct,
    calc_mmr_pct,
    parse_risk_limit_tiers,
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
    """Test standard ROE formula."""

    def test_long_profit_10x(self):
        """Long 10x leverage, price up 1%."""
        result = calc_unrealised_pnl_pct(
            "long", Decimal("50000"), Decimal("50500"), Decimal("10")
        )
        # (50500 - 50000) / 50000 * 10 * 100 = 10.0%
        assert result == Decimal("10")

    def test_short_profit_10x(self):
        """Short 10x leverage, price down 1%."""
        result = calc_unrealised_pnl_pct(
            "short", Decimal("50000"), Decimal("49500"), Decimal("10")
        )
        # (50000 - 49500) / 50000 * 10 * 100 = 10.0%
        assert result == Decimal("10")

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
        # (102 - 100) / 100 * 1 * 100 = 2.0%
        assert result == Decimal("2")


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

    def test_fallback_no_tiers(self):
        """Without tiers or symbol, falls back to position_value / leverage."""
        im, imr = calc_initial_margin(Decimal("5000"), Decimal("10"))
        assert im == Decimal("500")
        assert imr == Decimal("0.1")  # 1/10

    def test_zero_leverage_fallback(self):
        """Zero leverage returns zero when no tiers available."""
        im, imr = calc_initial_margin(Decimal("5000"), Decimal("0"))
        assert im == Decimal("0")
        assert imr == Decimal("0")

    def test_high_leverage_fallback(self):
        """100x leverage fallback."""
        im, imr = calc_initial_margin(Decimal("50000"), Decimal("100"))
        assert im == Decimal("500")
        assert imr == Decimal("0.01")  # 1/100

    def test_tier_based_btcusdt(self):
        """BTCUSDT tier 1: $5000 position → 1% IMR, $50 IM."""
        im, imr = calc_initial_margin(
            Decimal("5000"), Decimal("10"), symbol="BTCUSDT",
        )
        assert imr == Decimal("0.01")
        assert im == Decimal("50")  # 5000 * 0.01

    def test_tier_based_custom_tiers(self):
        """Explicit tiers override symbol lookup."""
        custom_tiers = [
            (Decimal("100000"), Decimal("0.005"), Decimal("0"), Decimal("0.02")),
            (Decimal("Infinity"), Decimal("0.01"), Decimal("500"), Decimal("0.05")),
        ]
        im, imr = calc_initial_margin(
            Decimal("50000"), Decimal("10"), tiers=custom_tiers,
        )
        assert imr == Decimal("0.02")
        assert im == Decimal("1000")  # 50000 * 0.02

    def test_tier_based_higher_tier(self):
        """Position in higher tier uses higher IMR."""
        custom_tiers = [
            (Decimal("100000"), Decimal("0.005"), Decimal("0"), Decimal("0.02")),
            (Decimal("Infinity"), Decimal("0.01"), Decimal("500"), Decimal("0.05")),
        ]
        im, imr = calc_initial_margin(
            Decimal("200000"), Decimal("10"), tiers=custom_tiers,
        )
        assert imr == Decimal("0.05")
        assert im == Decimal("10000")  # 200000 * 0.05

    def test_zero_position_value(self):
        """Zero position value returns zero."""
        im, imr = calc_initial_margin(Decimal("0"), Decimal("10"), symbol="BTCUSDT")
        assert im == Decimal("0")
        assert imr == Decimal("0")


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


class TestCalcMaintenanceMargin:
    """Test tier-based maintenance margin calculation."""

    def test_btcusdt_tier1(self):
        """BTCUSDT tier 1: $1M position → 0.5% MMR, $5k MM."""
        mm, mmr = calc_maintenance_margin(Decimal("1000000"), "BTCUSDT")
        assert mmr == Decimal("0.005")
        assert mm == Decimal("5000")  # 1_000_000 * 0.005 - 0

    def test_btcusdt_tier2(self):
        """BTCUSDT tier 2: $5M position → 1% MMR, $40k MM."""
        mm, mmr = calc_maintenance_margin(Decimal("5000000"), "BTCUSDT")
        assert mmr == Decimal("0.01")
        assert mm == Decimal("40000")  # 5_000_000 * 0.01 - 10_000

    def test_btcusdt_tier1_boundary(self):
        """BTCUSDT exactly at tier 1 max ($2M) stays in tier 1."""
        mm, mmr = calc_maintenance_margin(Decimal("2000000"), "BTCUSDT")
        assert mmr == Decimal("0.005")
        assert mm == Decimal("10000")  # 2_000_000 * 0.005 - 0

    def test_btcusdt_tier2_boundary(self):
        """BTCUSDT just above tier 1 ($2M + 1) goes to tier 2."""
        mm, mmr = calc_maintenance_margin(Decimal("2000001"), "BTCUSDT")
        assert mmr == Decimal("0.01")
        # 2_000_001 * 0.01 - 10_000 = 10_000.01
        assert mm == Decimal("10000.01")

    def test_ethusdt_tier1(self):
        """ETHUSDT tier 1: $500k position → 0.5% MMR."""
        mm, mmr = calc_maintenance_margin(Decimal("500000"), "ETHUSDT")
        assert mmr == Decimal("0.005")
        assert mm == Decimal("2500")  # 500_000 * 0.005 - 0

    def test_ethusdt_tier2(self):
        """ETHUSDT tier 2: $3M position → 1% MMR."""
        mm, mmr = calc_maintenance_margin(Decimal("3000000"), "ETHUSDT")
        assert mmr == Decimal("0.01")
        assert mm == Decimal("25000")  # 3_000_000 * 0.01 - 5_000

    def test_unknown_symbol_uses_default(self):
        """Unknown symbol falls back to default tiers (1% tier 1)."""
        mm, mmr = calc_maintenance_margin(Decimal("500000"), "XYZUSDT")
        assert mmr == Decimal("0.01")
        assert mm == Decimal("5000")  # 500_000 * 0.01 - 0

    def test_zero_position(self):
        """Zero position value returns zero."""
        mm, mmr = calc_maintenance_margin(Decimal("0"), "BTCUSDT")
        assert mm == Decimal("0")
        assert mmr == Decimal("0")

    def test_negative_position(self):
        """Negative position value returns zero."""
        mm, mmr = calc_maintenance_margin(Decimal("-100"), "BTCUSDT")
        assert mm == Decimal("0")
        assert mmr == Decimal("0")


class TestCalcImrPct:
    """Test account IMR% calculation."""

    def test_basic(self):
        """IMR% = total_IM / margin_balance * 100."""
        result = calc_imr_pct(Decimal("1000"), Decimal("10000"))
        assert result == Decimal("10")  # 1000/10000 * 100

    def test_zero_margin_balance(self):
        """Zero margin balance returns 0."""
        result = calc_imr_pct(Decimal("1000"), Decimal("0"))
        assert result == Decimal("0")

    def test_negative_margin_balance(self):
        """Negative margin balance returns 0."""
        result = calc_imr_pct(Decimal("1000"), Decimal("-500"))
        assert result == Decimal("0")

    def test_small_margin(self):
        """Small IM relative to balance."""
        result = calc_imr_pct(Decimal("51"), Decimal("10000"))
        assert result == Decimal("0.51")


class TestCalcMmrPct:
    """Test account MMR% calculation."""

    def test_basic(self):
        """MMR% = total_MM / margin_balance * 100."""
        result = calc_mmr_pct(Decimal("500"), Decimal("10000"))
        assert result == Decimal("5")  # 500/10000 * 100

    def test_zero_margin_balance(self):
        """Zero margin balance returns 0."""
        result = calc_mmr_pct(Decimal("500"), Decimal("0"))
        assert result == Decimal("0")

    def test_liquidation_threshold(self):
        """MMR% = 100% means liquidation."""
        result = calc_mmr_pct(Decimal("10000"), Decimal("10000"))
        assert result == Decimal("100")


class TestCalcMaintenanceMarginCustomTiers:
    """Test calc_maintenance_margin with explicit tiers parameter."""

    CUSTOM_TIERS = [
        (Decimal("200000"), Decimal("0.01"), Decimal("0"), Decimal("0.02")),
        (Decimal("1000000"), Decimal("0.025"), Decimal("3000"), Decimal("0.05")),
        (Decimal("Infinity"), Decimal("0.05"), Decimal("28000"), Decimal("0.1")),
    ]

    def test_custom_tiers_override_symbol(self):
        """Explicit tiers override symbol-based lookup."""
        # With symbol alone, BTCUSDT tier 1 is 0.5% MMR
        mm_symbol, mmr_symbol = calc_maintenance_margin(Decimal("100000"), "BTCUSDT")
        assert mmr_symbol == Decimal("0.005")

        # With custom tiers, tier 1 is 1% MMR
        mm_custom, mmr_custom = calc_maintenance_margin(
            Decimal("100000"), "BTCUSDT", tiers=self.CUSTOM_TIERS
        )
        assert mmr_custom == Decimal("0.01")
        assert mm_custom == Decimal("1000")  # 100_000 * 0.01 - 0

    def test_custom_tiers_tier2(self):
        """Position hitting second custom tier."""
        mm, mmr = calc_maintenance_margin(
            Decimal("500000"), "BTCUSDT", tiers=self.CUSTOM_TIERS
        )
        assert mmr == Decimal("0.025")
        assert mm == Decimal("9500")  # 500_000 * 0.025 - 3_000

    def test_custom_tiers_none_falls_back(self):
        """tiers=None uses hardcoded tables."""
        mm, mmr = calc_maintenance_margin(
            Decimal("1000000"), "BTCUSDT", tiers=None
        )
        assert mmr == Decimal("0.005")
        assert mm == Decimal("5000")  # 1_000_000 * 0.005 - 0


class TestParseRiskLimitTiers:
    """Test parse_risk_limit_tiers() Bybit API converter."""

    def test_single_tier(self):
        """Single tier gets Infinity cap."""
        api_tiers = [
            {
                "riskLimitValue": "200000",
                "maintenanceMargin": "0.01",
                "mmDeduction": "0",
                "initialMargin": "0.02",
            }
        ]
        result = parse_risk_limit_tiers(api_tiers)
        assert len(result) == 1
        assert result[0] == (Decimal("Infinity"), Decimal("0.01"), Decimal("0"), Decimal("0.02"))

    def test_multi_tier_sorting(self):
        """Multiple tiers sorted by riskLimitValue, last gets Infinity."""
        api_tiers = [
            {
                "riskLimitValue": "1000000",
                "maintenanceMargin": "0.025",
                "mmDeduction": "3000",
                "initialMargin": "0.05",
            },
            {
                "riskLimitValue": "200000",
                "maintenanceMargin": "0.01",
                "mmDeduction": "0",
                "initialMargin": "0.02",
            },
        ]
        result = parse_risk_limit_tiers(api_tiers)
        assert len(result) == 2
        assert result[0] == (Decimal("200000"), Decimal("0.01"), Decimal("0"), Decimal("0.02"))
        assert result[1] == (Decimal("Infinity"), Decimal("0.025"), Decimal("3000"), Decimal("0.05"))

    def test_empty_input_raises(self):
        """Empty input raises ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            parse_risk_limit_tiers([])

    def test_empty_mm_deduction_defaults_to_zero(self):
        """Empty or missing mmDeduction defaults to 0."""
        api_tiers = [
            {
                "riskLimitValue": "200000",
                "maintenanceMargin": "0.01",
                "mmDeduction": "",
                "initialMargin": "0.02",
            }
        ]
        result = parse_risk_limit_tiers(api_tiers)
        assert result[0][2] == Decimal("0")

    def test_missing_mm_deduction_defaults_to_zero(self):
        """Missing mmDeduction key defaults to 0."""
        api_tiers = [
            {
                "riskLimitValue": "200000",
                "maintenanceMargin": "0.01",
            }
        ]
        result = parse_risk_limit_tiers(api_tiers)
        assert result[0][2] == Decimal("0")

    def test_missing_initial_margin_defaults_to_zero(self):
        """Missing initialMargin key defaults to 0 (backward compat)."""
        api_tiers = [
            {
                "riskLimitValue": "200000",
                "maintenanceMargin": "0.01",
                "mmDeduction": "0",
            }
        ]
        result = parse_risk_limit_tiers(api_tiers)
        assert result[0][3] == Decimal("0")

    def test_initial_margin_extracted(self):
        """initialMargin is extracted as 4th element."""
        api_tiers = [
            {
                "riskLimitValue": "200000",
                "maintenanceMargin": "0.01",
                "mmDeduction": "0",
                "initialMargin": "0.02",
            }
        ]
        result = parse_risk_limit_tiers(api_tiers)
        assert result[0][3] == Decimal("0.02")

    def test_integration_with_calc_maintenance_margin(self):
        """Parsed tiers work correctly with calc_maintenance_margin."""
        api_tiers = [
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
        tiers = parse_risk_limit_tiers(api_tiers)

        # Position in tier 1
        mm, mmr = calc_maintenance_margin(Decimal("100000"), tiers=tiers)
        assert mmr == Decimal("0.01")
        assert mm == Decimal("1000")

        # Position in tier 2 (above 200k, Infinity cap)
        mm, mmr = calc_maintenance_margin(Decimal("500000"), tiers=tiers)
        assert mmr == Decimal("0.025")
        assert mm == Decimal("9500")  # 500_000 * 0.025 - 3_000

    def test_integration_with_calc_initial_margin(self):
        """Parsed tiers work correctly with calc_initial_margin."""
        api_tiers = [
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
        tiers = parse_risk_limit_tiers(api_tiers)

        # Position in tier 1
        im, imr = calc_initial_margin(Decimal("100000"), Decimal("10"), tiers=tiers)
        assert imr == Decimal("0.02")
        assert im == Decimal("2000")  # 100_000 * 0.02

        # Position in tier 2 (above 200k)
        im, imr = calc_initial_margin(Decimal("500000"), Decimal("10"), tiers=tiers)
        assert imr == Decimal("0.05")
        assert im == Decimal("25000")  # 500_000 * 0.05
