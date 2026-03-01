"""Tests for position tracker."""

from decimal import Decimal

import pytest

from backtest.position_tracker import BacktestPositionTracker, PositionState


class TestBacktestPositionTracker:
    """Tests for BacktestPositionTracker."""

    def test_init_long_position(self):
        """Initialize long position tracker."""
        tracker = BacktestPositionTracker(direction="long")

        assert tracker.direction == "long"
        assert tracker.state.size == Decimal("0")
        assert tracker.state.avg_entry_price == Decimal("0")
        assert not tracker.has_position

    def test_init_short_position(self):
        """Initialize short position tracker."""
        tracker = BacktestPositionTracker(direction="short")

        assert tracker.direction == "short"
        assert tracker.state.size == Decimal("0")
        assert not tracker.has_position

    def test_init_invalid_direction_raises(self):
        """Invalid direction raises ValueError."""
        with pytest.raises(ValueError):
            BacktestPositionTracker(direction="invalid")

    def test_long_buy_opens_position(self, long_position_tracker):
        """Long position: Buy opens/adds to position."""
        tracker = long_position_tracker

        realized = tracker.process_fill(
            side="Buy",
            qty=Decimal("0.1"),
            price=Decimal("100000"),
        )

        assert realized == Decimal("0")  # No realized PnL when opening
        assert tracker.state.size == Decimal("0.1")
        assert tracker.state.avg_entry_price == Decimal("100000")
        assert tracker.has_position

    def test_long_sell_closes_position_profit(self, long_position_tracker):
        """Long position: Sell closes with profit."""
        tracker = long_position_tracker

        # Open long at 100000
        tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("100000"))

        # Close at 101000 (profit)
        realized = tracker.process_fill(side="Sell", qty=Decimal("0.1"), price=Decimal("101000"))

        # PnL = (101000 - 100000) * 0.1 = 100
        assert realized == Decimal("100")
        assert tracker.state.size == Decimal("0")
        assert tracker.state.avg_entry_price == Decimal("0")
        assert tracker.state.realized_pnl == Decimal("100")
        assert not tracker.has_position

    def test_long_sell_closes_position_loss(self, long_position_tracker):
        """Long position: Sell closes with loss."""
        tracker = long_position_tracker

        # Open long at 100000
        tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("100000"))

        # Close at 99000 (loss)
        realized = tracker.process_fill(side="Sell", qty=Decimal("0.1"), price=Decimal("99000"))

        # PnL = (99000 - 100000) * 0.1 = -100
        assert realized == Decimal("-100")
        assert tracker.state.size == Decimal("0")
        assert tracker.state.avg_entry_price == Decimal("0")
        assert tracker.state.realized_pnl == Decimal("-100")
        assert not tracker.has_position

    def test_short_sell_opens_position(self, short_position_tracker):
        """Short position: Sell opens position."""
        tracker = short_position_tracker

        realized = tracker.process_fill(
            side="Sell",
            qty=Decimal("0.1"),
            price=Decimal("100000"),
        )

        assert realized == Decimal("0")
        assert tracker.state.size == Decimal("0.1")
        assert tracker.state.avg_entry_price == Decimal("100000")

    def test_short_buy_closes_position_profit(self, short_position_tracker):
        """Short position: Buy closes with profit (price dropped)."""
        tracker = short_position_tracker

        # Open short at 100000
        tracker.process_fill(side="Sell", qty=Decimal("0.1"), price=Decimal("100000"))

        # Close at 99000 (profit for short)
        realized = tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("99000"))

        # PnL = (100000 - 99000) * 0.1 = 100
        assert realized == Decimal("100")
        assert tracker.state.size == Decimal("0")
        assert tracker.state.avg_entry_price == Decimal("0")
        assert tracker.state.realized_pnl == Decimal("100")
        assert not tracker.has_position

    def test_short_buy_closes_position_loss(self, short_position_tracker):
        """Short position: Buy closes with loss (price rose)."""
        tracker = short_position_tracker

        # Open short at 100000
        tracker.process_fill(side="Sell", qty=Decimal("0.1"), price=Decimal("100000"))

        # Close at 101000 (loss for short)
        realized = tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("101000"))

        # PnL = (100000 - 101000) * 0.1 = -100
        assert realized == Decimal("-100")
        assert tracker.state.size == Decimal("0")
        assert tracker.state.avg_entry_price == Decimal("0")
        assert tracker.state.realized_pnl == Decimal("-100")
        assert not tracker.has_position

    def test_add_to_position_weighted_avg(self, long_position_tracker):
        """Adding to position updates weighted average entry."""
        tracker = long_position_tracker

        # Buy 0.1 at 100000
        tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("100000"))

        # Buy 0.1 at 102000
        tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("102000"))

        # Avg = (100000 * 0.1 + 102000 * 0.1) / 0.2 = 101000
        assert tracker.state.size == Decimal("0.2")
        assert tracker.state.avg_entry_price == Decimal("101000")

    def test_partial_close(self, long_position_tracker):
        """Partially closing position."""
        tracker = long_position_tracker

        # Open 0.2 at 100000
        tracker.process_fill(side="Buy", qty=Decimal("0.2"), price=Decimal("100000"))

        # Close 0.1 at 101000
        realized = tracker.process_fill(side="Sell", qty=Decimal("0.1"), price=Decimal("101000"))

        assert realized == Decimal("100")  # (101000 - 100000) * 0.1
        assert tracker.state.size == Decimal("0.1")
        assert tracker.state.avg_entry_price == Decimal("100000")  # Unchanged

    def test_unrealized_pnl_long(self, long_position_tracker):
        """Calculate unrealized PnL for long position."""
        tracker = long_position_tracker

        tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("100000"))

        # Price at 101000
        unrealized = tracker.calculate_unrealized_pnl(Decimal("101000"))
        assert unrealized == Decimal("100")  # (101000 - 100000) * 0.1

        # Price at 99000
        unrealized = tracker.calculate_unrealized_pnl(Decimal("99000"))
        assert unrealized == Decimal("-100")  # (99000 - 100000) * 0.1

    def test_unrealized_pnl_short(self, short_position_tracker):
        """Calculate unrealized PnL for short position."""
        tracker = short_position_tracker

        tracker.process_fill(side="Sell", qty=Decimal("0.1"), price=Decimal("100000"))

        # Price at 99000 (profit for short)
        unrealized = tracker.calculate_unrealized_pnl(Decimal("99000"))
        assert unrealized == Decimal("100")

        # Price at 101000 (loss for short)
        unrealized = tracker.calculate_unrealized_pnl(Decimal("101000"))
        assert unrealized == Decimal("-100")

    def test_commission_tracking(self):
        """Commission is tracked on fills."""
        tracker = BacktestPositionTracker(
            direction="long",
            commission_rate=Decimal("0.001"),  # 0.1%
        )

        tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("100000"))

        # Commission = 0.1 * 100000 * 0.001 = 10
        assert tracker.state.commission_paid == Decimal("10")

    def test_funding_long_pays(self, long_position_tracker):
        """Long position pays funding when rate > 0."""
        tracker = long_position_tracker

        tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("100000"))

        # Position value = 0.1 * 100000 = 10000
        # Funding = 10000 * 0.0001 = 1
        # Long pays, so return negative
        payment = tracker.apply_funding(Decimal("0.0001"), Decimal("100000"))

        assert payment == Decimal("-1")

    def test_funding_short_receives(self, short_position_tracker):
        """Short position receives funding when rate > 0."""
        tracker = short_position_tracker

        tracker.process_fill(side="Sell", qty=Decimal("0.1"), price=Decimal("100000"))

        payment = tracker.apply_funding(Decimal("0.0001"), Decimal("100000"))

        assert payment == Decimal("1")  # Short receives

    def test_get_total_pnl(self, long_position_tracker):
        """Total PnL combines realized, unrealized, commission, funding."""
        tracker = long_position_tracker

        tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("100000"))
        tracker.process_fill(side="Sell", qty=Decimal("0.1"), price=Decimal("101000"))  # +100 realized

        tracker.calculate_unrealized_pnl(Decimal("101000"))  # 0 unrealized (closed)

        # Total = 100 (realized) - commission - funding
        total = tracker.get_total_pnl()
        # Commission ~4 (0.1 * 100000 * 0.0002 + 0.1 * 101000 * 0.0002)
        assert total < Decimal("100")  # Less than raw realized due to commission

    def test_unrealized_pnl_percent_long_profit(self, long_position_tracker):
        """Calculate unrealized PnL % for long position in profit."""
        tracker = long_position_tracker

        # Open long at 50000
        tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("50000"))

        # Price at 51000 (2% price increase, 20% ROE with 10x leverage)
        leverage = Decimal("10")
        pnl_percent = tracker.calculate_unrealized_pnl_percent(Decimal("51000"), leverage)

        # Formula: (close - entry) / entry * leverage * 100
        # (51000 - 50000) / 50000 * 10 * 100 = 20.0%
        expected = (
            (Decimal("51000") - Decimal("50000"))
            / Decimal("50000")
            * leverage
            * Decimal("100")
        )
        assert pnl_percent == expected
        assert pnl_percent > Decimal("0")  # Profit
        assert tracker.state.unrealized_pnl_percent == pnl_percent

    def test_unrealized_pnl_percent_long_loss(self, long_position_tracker):
        """Calculate unrealized PnL % for long position in loss."""
        tracker = long_position_tracker

        # Open long at 50000
        tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("50000"))

        # Price at 49000 (price dropped)
        leverage = Decimal("10")
        pnl_percent = tracker.calculate_unrealized_pnl_percent(Decimal("49000"), leverage)

        # Should be negative (loss)
        assert pnl_percent < Decimal("0")

    def test_unrealized_pnl_percent_short_profit(self, short_position_tracker):
        """Calculate unrealized PnL % for short position in profit."""
        tracker = short_position_tracker

        # Open short at 50000
        tracker.process_fill(side="Sell", qty=Decimal("0.1"), price=Decimal("50000"))

        # Price at 49000 (price dropped - profit for short)
        leverage = Decimal("10")
        pnl_percent = tracker.calculate_unrealized_pnl_percent(Decimal("49000"), leverage)

        # Formula: (entry - close) / entry * leverage * 100
        # (50000 - 49000) / 50000 * 10 * 100 = 20.0%
        expected = (
            (Decimal("50000") - Decimal("49000"))
            / Decimal("50000")
            * leverage
            * Decimal("100")
        )
        assert pnl_percent == expected
        assert pnl_percent > Decimal("0")  # Profit
        assert tracker.state.unrealized_pnl_percent == pnl_percent

    def test_unrealized_pnl_percent_short_loss(self, short_position_tracker):
        """Calculate unrealized PnL % for short position in loss."""
        tracker = short_position_tracker

        # Open short at 50000
        tracker.process_fill(side="Sell", qty=Decimal("0.1"), price=Decimal("50000"))

        # Price at 51000 (price went up - loss for short)
        leverage = Decimal("10")
        pnl_percent = tracker.calculate_unrealized_pnl_percent(Decimal("51000"), leverage)

        # Should be negative (loss)
        assert pnl_percent < Decimal("0")

    def test_unrealized_pnl_percent_empty_position(self, long_position_tracker):
        """Unrealized PnL % is 0 for empty position."""
        tracker = long_position_tracker

        pnl_percent = tracker.calculate_unrealized_pnl_percent(
            Decimal("50000"), Decimal("10")
        )

        assert pnl_percent == Decimal("0")
        assert tracker.state.unrealized_pnl_percent == Decimal("0")


class TestMarginCalculation:
    """Tests for IM/MM calculation in BacktestPositionTracker."""

    def test_margin_calculated_on_unrealized_pnl(self):
        """Margin fields populated when calculate_unrealized_pnl is called."""
        tracker = BacktestPositionTracker(
            direction="long", leverage=10, symbol="BTCUSDT",
        )
        tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("50000"))
        tracker.calculate_unrealized_pnl(Decimal("51000"))

        # position_value = size * entry = 0.1 * 50000 = 5000
        assert tracker.state.position_value == Decimal("5000")
        # IM = 5000 * 0.01 = 50 (BTCUSDT tier 1: imr_rate=0.01)
        assert tracker.state.initial_margin == Decimal("50")
        assert tracker.state.imr_rate == Decimal("0.01")
        # MM = 5000 * 0.005 - 0 = 25 (BTCUSDT tier 1: ≤2M, 0.5%)
        assert tracker.state.maintenance_margin.normalize() == Decimal("25")
        assert tracker.state.mmr_rate == Decimal("0.005")

    def test_margin_zero_when_no_position(self):
        """Margin fields are zero when position is empty."""
        tracker = BacktestPositionTracker(
            direction="long", leverage=10, symbol="BTCUSDT",
        )
        tracker.calculate_unrealized_pnl(Decimal("50000"))

        assert tracker.state.position_value == Decimal("0")
        assert tracker.state.initial_margin == Decimal("0")
        assert tracker.state.maintenance_margin == Decimal("0")

    def test_margin_reset_after_close(self):
        """Margin fields reset to zero after position is closed."""
        tracker = BacktestPositionTracker(
            direction="long", leverage=10, symbol="BTCUSDT",
        )
        tracker.process_fill(side="Buy", qty=Decimal("0.1"), price=Decimal("50000"))
        tracker.calculate_unrealized_pnl(Decimal("51000"))
        assert tracker.state.initial_margin > 0

        # Close position
        tracker.process_fill(side="Sell", qty=Decimal("0.1"), price=Decimal("51000"))
        tracker.calculate_unrealized_pnl(Decimal("51000"))

        assert tracker.state.position_value == Decimal("0")
        assert tracker.state.initial_margin == Decimal("0")
        assert tracker.state.maintenance_margin == Decimal("0")

    def test_margin_with_custom_tiers(self):
        """Custom tiers override symbol-based lookup."""
        custom_tiers = [
            (Decimal("100000"), Decimal("0.02"), Decimal("0"), Decimal("0.04")),
            (Decimal("Infinity"), Decimal("0.05"), Decimal("3000"), Decimal("0.1")),
        ]
        tracker = BacktestPositionTracker(
            direction="short", leverage=5, tiers=custom_tiers, symbol="XYZUSDT",
        )
        tracker.process_fill(side="Sell", qty=Decimal("1"), price=Decimal("1000"))
        tracker.calculate_unrealized_pnl(Decimal("900"))

        # position_value = 1 * 1000 = 1000
        assert tracker.state.position_value == Decimal("1000")
        # IM = 1000 * 0.04 = 40 (tier 1: imr_rate=0.04)
        assert tracker.state.initial_margin == Decimal("40")
        assert tracker.state.imr_rate == Decimal("0.04")
        # MM = 1000 * 0.02 - 0 = 20 (tier 1: ≤100000)
        assert tracker.state.maintenance_margin == Decimal("20")
        assert tracker.state.mmr_rate == Decimal("0.02")

    def test_margin_uses_default_leverage(self):
        """Default leverage is 10."""
        tracker = BacktestPositionTracker(direction="long")
        assert tracker.leverage == Decimal("10")
