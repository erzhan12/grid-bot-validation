"""Tests for pure PnL calculation functions."""

from decimal import Decimal

from gridcore.pnl import (
    calc_unrealised_pnl,
    calc_unrealised_pnl_pct,
    calc_position_value,
    calc_initial_margin,
    calc_liq_ratio,
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
