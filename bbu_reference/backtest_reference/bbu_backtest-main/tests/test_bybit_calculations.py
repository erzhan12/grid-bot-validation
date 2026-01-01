"""
Test Bybit Calculations

Comprehensive test suite for validating all Bybit calculation formulas
against known values and expected behaviors.
"""

from datetime import datetime, timezone

import pytest

from src.bybit_calculations import BybitCalculator
from src.enums import Direction, MarginMode


class TestBybitCalculator:
    """Test all BybitCalculator methods with comprehensive scenarios"""

    def setup_method(self):
        """Set up test fixtures"""
        self.calculator = BybitCalculator()

    def test_position_value_calculation(self):
        """Test position value calculation: Contract Quantity × Mark Price"""
        # Basic test cases
        assert self.calculator.calculate_position_value(1.0, 50000) == 50000
        assert self.calculator.calculate_position_value(0.1, 45000) == 4500
        assert self.calculator.calculate_position_value(2.5, 30000) == 75000

        # Edge cases
        assert self.calculator.calculate_position_value(0, 50000) == 0
        assert self.calculator.calculate_position_value(1, 0) == 0

        # Very small amounts
        assert abs(self.calculator.calculate_position_value(0.001, 50000) - 50) < 0.001

    def test_initial_margin_calculation(self):
        """Test initial margin calculation: Position Value / Leverage"""
        # Basic test cases
        assert self.calculator.calculate_initial_margin(50000, 10) == 5000
        assert self.calculator.calculate_initial_margin(100000, 5) == 20000
        assert self.calculator.calculate_initial_margin(30000, 2) == 15000

        # High leverage
        assert self.calculator.calculate_initial_margin(100000, 50) == 2000
        assert self.calculator.calculate_initial_margin(100000, 100) == 1000

        # Test zero leverage validation
        with pytest.raises(ValueError, match="Leverage must be positive"):
            self.calculator.calculate_initial_margin(100000, 0)

        # Test negative leverage validation
        with pytest.raises(ValueError, match="Leverage must be positive"):
            self.calculator.calculate_initial_margin(100000, -10)

    def test_maintenance_margin_tier_selection(self):
        """Test maintenance margin tier selection based on position value"""
        # BTCUSDT tiers
        # Tier 1: 0 - 2M, MMR: 0.5%, Deduction: 0
        tier = self.calculator.get_maintenance_margin_tier(1000000, "BTCUSDT")
        assert tier["mmr"] == 0.005
        assert tier["deduction"] == 0

        # Tier 2: 2M - 10M, MMR: 1%, Deduction: 10k
        tier = self.calculator.get_maintenance_margin_tier(5000000, "BTCUSDT")
        assert tier["mmr"] == 0.01
        assert tier["deduction"] == 10000

        # Tier 3: 10M - 20M, MMR: 2.5%, Deduction: 160k
        tier = self.calculator.get_maintenance_margin_tier(15000000, "BTCUSDT")
        assert tier["mmr"] == 0.025
        assert tier["deduction"] == 160000

        # ETHUSDT tiers
        tier = self.calculator.get_maintenance_margin_tier(500000, "ETHUSDT")
        assert tier["mmr"] == 0.005
        assert tier["deduction"] == 0

        # Unknown symbol should use default tiers
        tier = self.calculator.get_maintenance_margin_tier(500000, "UNKNOWN")
        assert tier["mmr"] == 0.01  # Default first tier MMR

    def test_maintenance_margin_calculation(self):
        """Test maintenance margin calculation with tier system"""
        # BTCUSDT - Tier 1 (0.5% MMR, 0 deduction)
        mm, mmr = self.calculator.calculate_maintenance_margin(1000000, "BTCUSDT")
        expected_mm = (1000000 * 0.005) - 0
        assert abs(mm - expected_mm) < 0.01
        assert mmr == 0.005

        # BTCUSDT - Tier 2 (1% MMR, 10k deduction)
        mm, mmr = self.calculator.calculate_maintenance_margin(5000000, "BTCUSDT")
        expected_mm = (5000000 * 0.01) - 10000
        assert abs(mm - expected_mm) < 0.01
        assert mmr == 0.01

        # Edge case: ensure MM cannot be negative
        mm, mmr = self.calculator.calculate_maintenance_margin(100, "BTCUSDT")
        assert mm >= 0

    def test_liquidation_price_isolated_long(self):
        """Test isolated margin long liquidation price calculation"""
        # Test case: Long position, 50k entry, 10x leverage
        liq_price = self.calculator.calculate_liquidation_price(
            direction=Direction.LONG,
            entry_price=50000,
            contract_qty=1.0,
            leverage=10,
            margin_mode=MarginMode.ISOLATED,
            symbol="BTCUSDT"
        )

        # For isolated long: Liq = Entry × (Leverage - 1) / (Leverage - 1 + MMR × Leverage)
        # Position value = 1.0 × 50000 = 50000 (Tier 1: MMR = 0.5%)
        # Liq = 50000 × (10 - 1) / (10 - 1 + 0.005 × 10)
        # Liq = 50000 × 9 / (9 + 0.05) = 50000 × 9 / 9.05
        expected_liq = 50000 * 9 / 9.05
        assert abs(liq_price - expected_liq) < 1.0

        # Ensure liquidation price is less than entry price for long
        assert liq_price < 50000

    def test_liquidation_price_isolated_short(self):
        """Test isolated margin short liquidation price calculation"""
        # Test case: Short position, 50k entry, 10x leverage
        liq_price = self.calculator.calculate_liquidation_price(
            direction=Direction.SHORT,
            entry_price=50000,
            contract_qty=1.0,
            leverage=10,
            margin_mode=MarginMode.ISOLATED,
            symbol="BTCUSDT"
        )

        # For isolated short: Liq = Entry × (Leverage + 1) / (Leverage + 1 - MMR × Leverage)
        # Liq = 50000 × (10 + 1) / (10 + 1 - 0.005 × 10)
        # Liq = 50000 × 11 / (11 - 0.05) = 50000 × 11 / 10.95
        expected_liq = 50000 * 11 / 10.95
        assert abs(liq_price - expected_liq) < 1.0

        # Ensure liquidation price is greater than entry price for short
        assert liq_price > 50000

    def test_liquidation_price_cross_margin(self):
        """Test cross margin liquidation price calculation"""
        # Test case: Long position with cross margin
        liq_price = self.calculator.calculate_liquidation_price(
            direction=Direction.LONG,
            entry_price=50000,
            contract_qty=1.0,
            leverage=10,
            margin_mode=MarginMode.CROSS,
            available_balance=10000,
            symbol="BTCUSDT"
        )

        # Cross margin long: Liq = (Qty × Entry - Available + MM) / Qty
        position_value = 1.0 * 50000  # 50000
        mm, _ = self.calculator.calculate_maintenance_margin(position_value, "BTCUSDT")
        expected_liq = (1.0 * 50000 - 10000 + mm) / 1.0
        assert abs(liq_price - expected_liq) < 1.0

    def test_bankruptcy_price_calculation(self):
        """Test bankruptcy price calculation"""
        # Long position bankruptcy price
        bankruptcy_long = self.calculator.calculate_bankruptcy_price(
            direction=Direction.LONG,
            entry_price=50000,
            contract_qty=1.0,
            leverage=10
        )
        # Long bankruptcy = Entry × (1 - 1/Leverage) = 50000 × (1 - 0.1) = 45000
        expected_long = 50000 * (1 - 1 / 10)
        assert abs(bankruptcy_long - expected_long) < 0.01

        # Short position bankruptcy price
        bankruptcy_short = self.calculator.calculate_bankruptcy_price(
            direction=Direction.SHORT,
            entry_price=50000,
            contract_qty=1.0,
            leverage=10
        )
        # Short bankruptcy = Entry × (1 + 1/Leverage) = 50000 × (1 + 0.1) = 55000
        expected_short = 50000 * (1 + 1 / 10)
        assert abs(bankruptcy_short - expected_short) < 0.01

    def test_unrealized_pnl_calculation(self):
        """Test unrealized PnL calculation"""
        # Long position with profit
        long_profit = self.calculator.calculate_unrealized_pnl(
            direction=Direction.LONG,
            contract_qty=1.0,
            entry_price=50000,
            mark_price=55000
        )
        # Long PnL = (Mark - Entry) × Qty = (55000 - 50000) × 1.0 = 5000
        assert abs(long_profit - 5000) < 0.01

        # Long position with loss
        long_loss = self.calculator.calculate_unrealized_pnl(
            direction=Direction.LONG,
            contract_qty=1.0,
            entry_price=50000,
            mark_price=45000
        )
        # Long PnL = (45000 - 50000) × 1.0 = -5000
        assert abs(long_loss - (-5000)) < 0.01

        # Short position with profit
        short_profit = self.calculator.calculate_unrealized_pnl(
            direction=Direction.SHORT,
            contract_qty=1.0,
            entry_price=50000,
            mark_price=45000
        )
        # Short PnL = (Entry - Mark) × Qty = (50000 - 45000) × 1.0 = 5000
        assert abs(short_profit - 5000) < 0.01

        # Short position with loss
        short_loss = self.calculator.calculate_unrealized_pnl(
            direction=Direction.SHORT,
            contract_qty=1.0,
            entry_price=50000,
            mark_price=55000
        )
        # Short PnL = (50000 - 55000) × 1.0 = -5000
        assert abs(short_loss - (-5000)) < 0.01

    def test_realized_pnl_calculation(self):
        """Test realized PnL calculation including fees"""
        # Long position closing with profit
        realized_pnl = self.calculator.calculate_realized_pnl(
            direction=Direction.LONG,
            contract_qty=1.0,
            entry_price=50000,
            exit_price=55000,
            commission_paid=33  # Entry fee + Exit fee
        )
        # Gross PnL = (55000 - 50000) × 1.0 = 5000
        # Net PnL = 5000 - 33 = 4967
        assert abs(realized_pnl - 4967) < 0.01

        # Short position closing with profit
        realized_pnl = self.calculator.calculate_realized_pnl(
            direction=Direction.SHORT,
            contract_qty=1.0,
            entry_price=50000,
            exit_price=45000,
            commission_paid=28.5
        )
        # Gross PnL = (50000 - 45000) × 1.0 = 5000
        # Net PnL = 5000 - 28.5 = 4971.5
        assert abs(realized_pnl - 4971.5) < 0.01

    def test_funding_payment_calculation(self):
        """Test funding payment calculation"""
        # Positive funding rate (traders pay)
        funding = self.calculator.calculate_funding_payment(100000, 0.0001)
        # Funding = Position Value × Rate = 100000 × 0.0001 = 10
        assert abs(funding - 10) < 0.01

        # Negative funding rate (traders receive)
        funding = self.calculator.calculate_funding_payment(100000, -0.0001)
        assert abs(funding - (-10)) < 0.01

        # Zero funding rate
        funding = self.calculator.calculate_funding_payment(100000, 0)
        assert funding == 0

    def test_order_cost_calculation(self):
        """Test order cost calculation"""
        # Taker order
        cost = self.calculator.calculate_order_cost(100000, 10, is_maker=False)
        # Initial margin = 100000 / 10 = 10000
        # Taker fee = 100000 × 0.0006 = 60
        # Total cost = 10000 + 60 = 10060
        expected_cost = 10000 + 60
        assert abs(cost - expected_cost) < 0.01

        # Maker order
        cost = self.calculator.calculate_order_cost(100000, 10, is_maker=True)
        # Initial margin = 100000 / 10 = 10000
        # Maker fee = 100000 × 0.0001 = 10
        # Total cost = 10000 + 10 = 10010
        expected_cost = 10000 + 10
        assert abs(cost - expected_cost) < 0.01

    def test_margin_ratio_calculation(self):
        """Test margin ratio calculation"""
        # Healthy position
        margin_ratio = self.calculator.calculate_margin_ratio(
            unrealized_pnl=1000,
            wallet_balance=10000,
            position_value=50000,
            symbol="BTCUSDT"
        )
        # MM = (50000 × 0.005) - 0 = 250
        # Margin Balance = 10000 + 1000 = 11000
        # Margin Ratio = (11000 - 250) / 50000 = 10750 / 50000 = 0.215
        expected_ratio = (11000 - 250) / 50000
        assert abs(margin_ratio - expected_ratio) < 0.001

        # Position at risk (negative margin ratio)
        margin_ratio = self.calculator.calculate_margin_ratio(
            unrealized_pnl=-9000,
            wallet_balance=10000,
            position_value=50000,
            symbol="BTCUSDT"
        )
        # Margin Balance = 10000 - 9000 = 1000
        # Margin Ratio = (1000 - 250) / 50000 = 750 / 50000 = 0.015 (at risk)
        expected_ratio = (1000 - 250) / 50000
        assert abs(margin_ratio - expected_ratio) < 0.001

    def test_position_risk_assessment(self):
        """Test position risk assessment"""
        # Safe position
        assert not self.calculator.is_position_at_risk(
            unrealized_pnl=1000,
            wallet_balance=10000,
            position_value=50000,
            symbol="BTCUSDT",
            risk_threshold=0.02
        )

        # At-risk position
        assert self.calculator.is_position_at_risk(
            unrealized_pnl=-9000,
            wallet_balance=10000,
            position_value=50000,
            symbol="BTCUSDT",
            risk_threshold=0.02
        )

    def test_next_funding_time_calculation(self):
        """Test next funding time calculation (00:00, 08:00, 16:00 UTC)"""
        # Test at various times
        test_time = datetime(2024, 1, 1, 5, 30, 0, tzinfo=timezone.utc)  # 05:30 UTC
        next_funding = self.calculator.get_next_funding_time(test_time)
        expected = datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc)  # Next is 08:00
        assert next_funding.replace(tzinfo=timezone.utc) == expected

        test_time = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)  # 10:00 UTC
        next_funding = self.calculator.get_next_funding_time(test_time)
        expected = datetime(2024, 1, 1, 16, 0, 0, tzinfo=timezone.utc)  # Next is 16:00
        assert next_funding.replace(tzinfo=timezone.utc) == expected

        test_time = datetime(2024, 1, 1, 18, 0, 0, tzinfo=timezone.utc)  # 18:00 UTC
        next_funding = self.calculator.get_next_funding_time(test_time)
        expected = datetime(2024, 1, 2, 0, 0, 0, tzinfo=timezone.utc)  # Next day 00:00
        assert next_funding.replace(tzinfo=timezone.utc) == expected

        # Test month boundary (regression test for date arithmetic bug)
        test_time = datetime(2024, 1, 31, 23, 0, 0, tzinfo=timezone.utc)  # Jan 31, 23:00 UTC
        next_funding = self.calculator.get_next_funding_time(test_time)
        expected = datetime(2024, 2, 1, 0, 0, 0, tzinfo=timezone.utc)  # Feb 1, 00:00
        assert next_funding.replace(tzinfo=timezone.utc) == expected

        # Test leap year boundary
        test_time = datetime(2024, 2, 29, 20, 0, 0, tzinfo=timezone.utc)  # Feb 29, 20:00 UTC (leap year)
        next_funding = self.calculator.get_next_funding_time(test_time)
        expected = datetime(2024, 3, 1, 0, 0, 0, tzinfo=timezone.utc)  # Mar 1, 00:00
        assert next_funding.replace(tzinfo=timezone.utc) == expected

        # Test year boundary
        test_time = datetime(2024, 12, 31, 23, 0, 0, tzinfo=timezone.utc)  # Dec 31, 23:00 UTC
        next_funding = self.calculator.get_next_funding_time(test_time)
        expected = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)  # Jan 1, 00:00
        assert next_funding.replace(tzinfo=timezone.utc) == expected

    def test_comprehensive_position_summary(self):
        """Test comprehensive position summary calculation"""
        summary = self.calculator.calculate_position_summary(
            direction=Direction.LONG,
            contract_qty=1.0,
            entry_price=50000,
            mark_price=55000,
            leverage=10,
            wallet_balance=10000,
            symbol="BTCUSDT",
            margin_mode=MarginMode.ISOLATED,
            funding_rate=0.0001
        )

        # Verify all key fields are present
        required_fields = [
            'direction', 'size', 'entry_price', 'mark_price', 'leverage',
            'position_value', 'initial_margin', 'maintenance_margin',
            'unrealized_pnl', 'liquidation_price', 'bankruptcy_price',
            'margin_ratio', 'funding_payment', 'roe_percentage'
        ]

        for field in required_fields:
            assert field in summary, f"Missing field: {field}"

        # Verify specific calculations
        assert summary['direction'] == 'long'
        assert summary['size'] == 1.0
        assert summary['entry_price'] == 50000
        assert summary['mark_price'] == 55000
        assert summary['position_value'] == 55000  # 1.0 × 55000
        assert summary['initial_margin'] == 5500   # 55000 / 10
        assert summary['unrealized_pnl'] == 5000   # (55000 - 50000) × 1.0
        assert summary['funding_payment'] == 5.5   # 55000 × 0.0001
        assert summary['roe_percentage'] == (5000 / 5500) * 100  # ROE

    def test_high_leverage_scenarios(self):
        """Test calculations with high leverage (50x, 100x)"""
        # 50x leverage liquidation price
        liq_price = self.calculator.calculate_liquidation_price(
            direction=Direction.LONG,
            entry_price=50000,
            contract_qty=1.0,
            leverage=50,
            margin_mode=MarginMode.ISOLATED,
            symbol="BTCUSDT"
        )

        # Should be much closer to entry price with high leverage
        assert liq_price < 50000
        assert (50000 - liq_price) < 1000  # Less than $1000 difference

        # 100x leverage - extremely tight liquidation
        liq_price = self.calculator.calculate_liquidation_price(
            direction=Direction.LONG,
            entry_price=50000,
            contract_qty=1.0,
            leverage=100,
            margin_mode=MarginMode.ISOLATED,
            symbol="BTCUSDT"
        )

        assert liq_price < 50000
        assert (50000 - liq_price) < 500  # Less than $500 difference

    def test_different_symbols_margin_tiers(self):
        """Test maintenance margin calculations for different symbols"""
        # BTCUSDT vs ETHUSDT should have different tier structures
        btc_mm, _ = self.calculator.calculate_maintenance_margin(1000000, "BTCUSDT")
        eth_mm, _ = self.calculator.calculate_maintenance_margin(1000000, "ETHUSDT")

        # Both should be positive but potentially different due to different tiers
        assert btc_mm > 0
        assert eth_mm > 0

        # Unknown symbol should use default tiers
        unknown_mm, _ = self.calculator.calculate_maintenance_margin(1000000, "UNKNOWN")
        assert unknown_mm > 0

    def test_edge_cases(self):
        """Test edge cases and boundary conditions"""
        # Zero position size
        pnl = self.calculator.calculate_unrealized_pnl(Direction.LONG, 0, 50000, 55000)
        assert pnl == 0

        # Very small position
        pnl = self.calculator.calculate_unrealized_pnl(Direction.LONG, 0.001, 50000, 55000)
        assert abs(pnl - 5) < 0.01  # (55000 - 50000) × 0.001 = 5

        # Very large position (should move to higher MM tier)
        position_value = 50000000  # 50M - high tier
        mm, mmr = self.calculator.calculate_maintenance_margin(position_value, "BTCUSDT")
        assert mmr > 0.01  # Should be in higher tier with MMR > 1%

        # Infinite wallet balance (edge case for margin ratio)
        ratio = self.calculator.calculate_margin_ratio(0, float('inf'), 100000, "BTCUSDT")
        assert ratio == float('inf')


if __name__ == "__main__":
    pytest.main([__file__, "-v"])