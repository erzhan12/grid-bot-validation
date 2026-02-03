"""
Unit tests for Position module.
"""

import pytest
from decimal import Decimal
from gridcore.position import PositionState, PositionRiskManager, RiskConfig, Position


class TestPositionState:
    """Test PositionState dataclass."""

    def test_position_state_initialization(self):
        """PositionState initializes with correct defaults."""
        pos = PositionState(direction=Position.DIRECTION_LONG)
        assert pos.direction == 'long'
        assert pos.size == Decimal('0')
        assert pos.entry_price is None
        assert pos.unrealized_pnl == Decimal('0')

    def test_position_state_with_values(self):
        """PositionState holds position data."""
        pos = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.5'),
            entry_price=Decimal('100000.0'),
            unrealized_pnl=Decimal('50.0'),
            margin=Decimal('500.0'),
            liquidation_price=Decimal('110000.0'),
            leverage=10
        )
        assert pos.direction == 'short'
        assert pos.size == Decimal('1.5')
        assert pos.leverage == 10


class TestPositionRiskManager:
    """Test PositionRiskManager."""

    def test_risk_manager_initialization(self):
        """RiskManager initializes correctly."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_manager = PositionRiskManager('long', risk_config)
        assert long_manager.direction == 'long'
        assert long_manager.amount_multiplier['Buy'] == 1.0
        assert long_manager.amount_multiplier['Sell'] == 1.0

    def test_reset_amount_multiplier(self):
        """Reset multiplier returns to 1.0."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_manager = PositionRiskManager('long', risk_config)

        # Modify multipliers
        long_manager.amount_multiplier['Buy'] = 2.0
        long_manager.amount_multiplier['Sell'] = 0.5

        # Reset
        long_manager.reset_amount_multiplier()
        assert long_manager.amount_multiplier['Buy'] == 1.0
        assert long_manager.amount_multiplier['Sell'] == 1.0

    def test_calculate_amount_multiplier_long_position(self):
        """Calculate multipliers for long position."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('50000.0'),
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('150000.0'),
            margin=Decimal('2.0'),
            leverage=10
        )

        long_multipliers = long_manager.calculate_amount_multiplier(
            position,
            opposite,
            last_close=100000.0
        )

        # Should return multipliers dict
        assert 'Buy' in long_multipliers
        assert 'Sell' in long_multipliers
        assert isinstance(long_multipliers['Buy'], float)
        assert isinstance(long_multipliers['Sell'], float)

    def test_calculate_amount_multiplier_short_position(self):
        """Calculate multipliers for short position."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, short_manager = PositionRiskManager.create_linked_pair(risk_config)

        short_position_state = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('150000.0'),
            margin=Decimal('2.0'),
            leverage=10
        )

        long_position_state = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('50000.0'),
            margin=Decimal('2.0'),
            leverage=10
        )

        short_multipliers = short_manager.calculate_amount_multiplier(
            short_position_state,
            long_position_state,
            last_close=100000.0
        )

        # Should return multipliers dict
        assert 'Buy' in short_multipliers
        assert 'Sell' in short_multipliers


class TestRiskConfig:
    """Test RiskConfig dataclass."""

    def test_risk_config_creation(self):
        """RiskConfig creates with all parameters."""
        config = RiskConfig(
            min_liq_ratio=0.7,
            max_liq_ratio=1.3,
            max_margin=5.0,
            min_total_margin=1.0,
            increase_same_position_on_low_margin=True
        )

        assert config.min_liq_ratio == 0.7
        assert config.max_liq_ratio == 1.3
        assert config.max_margin == 5.0
        assert config.min_total_margin == 1.0
        assert config.increase_same_position_on_low_margin is True


class TestPositionRiskManagerRules:
    """Rule-level tests for PositionRiskManager risk management logic."""

    def test_calculate_amount_multiplier_without_opposite_raises_error(self):
        """Calling calculate_amount_multiplier without set_opposite raises ValueError."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_manager = PositionRiskManager('long', risk_config)
        # Don't set opposite - should raise error

        long_position_state = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('3.37'),
            entry_price=Decimal('3100.0'),
            liquidation_price=Decimal('0.0'),
            margin=Decimal('0.51'),
            leverage=10
        )

        short_position_state = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('4.62'),
            entry_price=Decimal('3102.0'),
            liquidation_price=Decimal('17553.0'),
            margin=Decimal('0.71'),
            leverage=10
        )

        # Should raise ValueError with helpful message
        try:
            long_manager.calculate_amount_multiplier(
                long_position_state, short_position_state, last_close=100000.0
            )
            assert False, "Expected ValueError but none was raised"
        except ValueError as e:
            assert "requires opposite position to be linked" in str(e)
            assert "set_opposite()" in str(e)
            assert "create_linked_pair()" in str(e)

    def test_create_linked_pair_helper(self):
        """create_linked_pair creates properly linked positions."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )

        long_mgr, short_mgr = PositionRiskManager.create_linked_pair(risk_config)

        # Verify directions
        assert long_mgr.direction == 'long'
        assert short_mgr.direction == 'short'

        # Verify both have references to each other
        assert long_mgr._opposite is short_mgr
        assert short_mgr._opposite is long_mgr

        # Verify both can be used without errors
        long_position_state = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('3.36'),
            entry_price=Decimal('3100.0'),
            liquidation_price=Decimal('0.0'),
            margin=Decimal('0.51'),
            leverage=10
        )

        short_position_state = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('4.62'),
            entry_price=Decimal('3102.0'),
            liquidation_price=Decimal('17553.0'),
            margin=Decimal('0.71'),
            leverage=10
        )

        # Should not raise error
        long_multipliers = long_mgr.calculate_amount_multiplier(
            long_position_state, short_position_state, last_close=3300.0
        )
        assert 'Buy' in long_multipliers
        assert 'Sell' in long_multipliers

    def test_high_liquidation_ratio_long_decreases_position(self):
        """High liquidation ratio for long position decreases long (increases sell multiplier)."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # High liquidation risk: liq_ratio > 1.05 * min_liq_ratio (0.84)
        long_position_state = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('2.5'),
            entry_price=Decimal('3300.0'),
            liquidation_price=Decimal('2635.0'),
            margin=Decimal('0.39'),
            leverage=10
        )

        # Short position exists (realistic hedged scenario)
        short_position_state = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('4.62'),
            entry_price=Decimal('3102.0'),
            liquidation_price=Decimal('17553.0'),
            margin=Decimal('0.71'),
            leverage=10
        )

        long_multipliers = long_manager.calculate_amount_multiplier(
            long_position_state, short_position_state, last_close=3100.0
        )

        # Should increase sell multiplier to decrease long position
        assert long_multipliers['Sell'] == 1.5
        assert long_multipliers['Buy'] == 1.0

    def test_moderate_liquidation_ratio_long_increases_opposite(self):
        """Moderate liquidation risk for long increases opposite (short) position."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        # Use helper to create properly linked positions
        long_manager, short_manager = PositionRiskManager.create_linked_pair(risk_config)

        # Moderate liquidation risk: liq_ratio > min_liq_ratio (0.8) but <= 1.05 * min_liq_ratio
        # Long position with moderate liquidation risk
        long_position_state = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('2.5'),
            entry_price=Decimal('3200.0'),
            liquidation_price=Decimal('2511.0'),  # liq_ratio = 0.81
            margin=Decimal('0.45'),
            leverage=10
        )

        # Short position exists but is smaller (realistic hedged scenario)
        short_position_state = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.8'),
            entry_price=Decimal('3102.0'),
            liquidation_price=Decimal('3565.0'),  # Safe liq ratio for short
            margin=Decimal('0.28'),
            leverage=10
        )
        long_multipliers = long_manager.calculate_amount_multiplier(
            long_position_state, short_position_state, last_close=3100.0
        )

        # Long position should not modify itself
        assert long_multipliers['Buy'] == 1.0
        assert long_multipliers['Sell'] == 1.0

        # Instead, it should modify the opposite (short) position's multipliers
        short_multipliers = short_manager.get_amount_multiplier()
        assert short_multipliers['Buy'] == 0.5  # Reduces short closing (increases short)
        assert short_multipliers['Sell'] == 1.0

    def test_high_liquidation_ratio_short_decreases_position(self):
        """High liquidation risk for short position decreases short (increases buy multiplier)."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, short_manager = PositionRiskManager.create_linked_pair(risk_config)

        # High liquidation risk: liq_ratio > 0.95 * max_liq_ratio (1.14)
        short_position_state = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('4.2'),
            entry_price=Decimal('3102.0'),
            liquidation_price=Decimal('3565.0'),  # liq_ratio = 1.15
            margin=Decimal('0.65'),
            leverage=10
        )

        # Long position exists (realistic hedged scenario)
        long_position_state = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('2.1'),
            entry_price=Decimal('3200.0'),
            liquidation_price=Decimal('2480.0'),
            margin=Decimal('0.33'),
            leverage=10
        )
        short_multipliers = short_manager.calculate_amount_multiplier(
            short_position_state, long_position_state, last_close=3100.0
        )

        # Should increase buy multiplier to decrease short position
        assert short_multipliers['Buy'] == 1.5
        assert short_multipliers['Sell'] == 1.0

    def test_low_total_margin_with_equal_positions(self):
        """Low total margin with equal positions triggers adjustment."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.5,
            increase_same_position_on_low_margin=False
        )
        long_manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # Equal positions (ratio ~1.0) but low total margin
        # liq_ratio = 2325 / 3100 = 0.75 (below min 0.8, safe)
        long_position_state = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('2.0'),
            entry_price=Decimal('3200.0'),
            liquidation_price=Decimal('2325.0'),  # Safe liq ratio
            margin=Decimal('0.4'),  # Low margin
            leverage=10
        )

        short_position_state = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('2.0'),
            entry_price=Decimal('3100.0'),
            liquidation_price=Decimal('4030.0'),  # Safe liq ratio
            margin=Decimal('0.4'),  # Equal margin (ratio = 1.0), total = 0.8 < 1.5
            leverage=10
        )

        long_multipliers = long_manager.calculate_amount_multiplier(
            long_position_state, short_position_state, last_close=3100.0
        )

        # Should reduce opposite side (sell) to increase long position
        assert long_multipliers['Sell'] == 0.5
        assert long_multipliers['Buy'] == 1.0

    def test_low_total_margin_increase_same_position(self):
        """Low total margin with increase_same_position_on_low_margin=True doubles same side."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.5,
            increase_same_position_on_low_margin=True
        )
        long_manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # Equal positions but low total margin
        # liq_ratio = 2325 / 3100 = 0.75 (below min 0.8, safe)
        long_position_state = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('2.0'),
            entry_price=Decimal('3200.0'),
            liquidation_price=Decimal('2325.0'),  # Safe liq ratio
            margin=Decimal('0.4'),
            leverage=10
        )

        short_position_state = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('2.0'),
            entry_price=Decimal('3100.0'),
            liquidation_price=Decimal('4030.0'),  # Safe liq ratio
            margin=Decimal('0.4'),  # Equal margin (ratio = 1.0), total = 0.8 < 1.5
            leverage=10
        )

        long_multipliers = long_manager.calculate_amount_multiplier(
            long_position_state, short_position_state, last_close=3100.0
        )

        # Should double buy multiplier to increase long position
        assert long_multipliers['Buy'] == 2.0
        assert long_multipliers['Sell'] == 1.0

    def test_small_long_position_losing_increases_long(self):
        """Small long position that's losing increases long multiplier."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # Small position (ratio < 0.5) and losing (price below entry)
        # liq_ratio = 70000 / 95000 = 0.737 (below min 0.8, safe)
        long_position_state = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.5'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('70000.0'),  # Safe liq ratio
            margin=Decimal('0.4'),
            leverage=10
        )

        short_position_state = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('125000.0'),  # Safe liq ratio
            margin=Decimal('2.0'),  # Much larger (ratio = 0.2)
            leverage=10
        )

        long_multipliers = long_manager.calculate_amount_multiplier(
            long_position_state, short_position_state, last_close=95000.0,  # Price below entry (losing)
        )

        # Should increase buy multiplier
        assert long_multipliers['Buy'] == 2.0
        assert long_multipliers['Sell'] == 1.0

    def test_very_small_long_position_increases_long(self):
        """Very small long position (ratio < 0.20) increases long multiplier."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # Very small position (ratio < 0.20)
        # liq_ratio = 70000 / 100000 = 0.7 (below min 0.8, safe)
        long_position_state = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.1'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('70000.0'),  # Safe liq ratio
            margin=Decimal('0.2'),
            leverage=10
        )

        short_position_state = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('130000.0'),  # Safe liq ratio
            margin=Decimal('2.0'),  # Much larger (ratio = 0.1)
            leverage=10
        )

        long_multipliers = long_manager.calculate_amount_multiplier(
            long_position_state, short_position_state, last_close=100000.0
        )

        # Should increase buy multiplier
        assert long_multipliers['Buy'] == 2.0
        assert long_multipliers['Sell'] == 1.0

    def test_large_short_position_losing_increases_short(self):
        """Large short position that's losing increases short multiplier."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, short_manager = PositionRiskManager.create_linked_pair(risk_config)

        # Large position (ratio > 2.0) and losing (price above entry)
        short_position_state = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('2.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('110000.0'),
            margin=Decimal('4.0'),
            leverage=10
        )

        long_position_state = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.5'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('90000.0'),
            margin=Decimal('1.0'),  # Much smaller (ratio = 4.0)
            leverage=10
        )

        short_multipliers = short_manager.calculate_amount_multiplier(
            short_position_state, long_position_state, last_close=105000.0,  # Price above entry (losing)
                    )

        # Should increase sell multiplier
        assert short_multipliers['Sell'] == 2.0
        assert short_multipliers['Buy'] == 1.0

    def test_very_large_short_position_increases_short(self):
        """Very large short position (ratio > 5.0) increases short multiplier."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, short_manager = PositionRiskManager.create_linked_pair(risk_config)

        # Very large position (ratio > 5.0)
        short_position_state = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('5.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('110000.0'),
            margin=Decimal('4.0'),
            leverage=10
        )

        long_position_state = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.5'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('90000.0'),
            margin=Decimal('0.4'),  # Much smaller (ratio = 10.0)
            leverage=10
        )

        short_multipliers = short_manager.calculate_amount_multiplier(
            short_position_state, long_position_state, last_close=100000.0
        )

        # Should increase sell multiplier
        assert short_multipliers['Sell'] == 2.0
        assert short_multipliers['Buy'] == 1.0

    def test_moderate_liquidation_ratio_short_increases_opposite(self):
        """Moderate liquidation risk for short position increases opposite (long) position."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        # Use helper to create properly linked positions
        long_manager, short_manager = PositionRiskManager.create_linked_pair(risk_config)

        # Moderate liquidation risk scenario
        # liq_ratio = 108000 / 100000 = 1.08 (between 0 and 1.2)
        # Not high enough to trigger emergency (< 0.95 * 1.2 = 1.14)
        # But moderate risk exists
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('108000.0'),  # Moderate risk
            margin=Decimal('3.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('90000.0'),
            margin=Decimal('3.0'),
            leverage=10
        )

        short_multipliers = short_manager.calculate_amount_multiplier(
            position, opposite, last_close=100000.0
        )

        # Short position should not modify itself
        assert short_multipliers['Sell'] == 1.0
        assert short_multipliers['Buy'] == 1.0

        # Instead, it should modify the opposite (long) position's multipliers
        # This is the logic from bbu2-master/position.py:81-86
        long_multipliers = long_manager.get_amount_multiplier()
        assert long_multipliers['Sell'] == 0.5  # Reduces long closing (increases long)
        assert long_multipliers['Buy'] == 1.0


    def test_create_linked_pair_with_separate_configs(self):
        """create_linked_pair works with separate long and short configs."""
        long_config = RiskConfig(
            min_liq_ratio=0.7,
            max_liq_ratio=1.3,
            max_margin=5.0,
            min_total_margin=1.0
        )
        short_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )

        long_mgr, short_mgr = PositionRiskManager.create_linked_pair(long_config, short_config)

        # Verify configs were applied
        assert long_mgr.risk_config is long_config
        assert short_mgr.risk_config is short_config

        # Verify they're still linked
        assert long_mgr._opposite is short_mgr
        assert short_mgr._opposite is long_mgr


class TestPositionStateEdgeCases:
    """Edge case tests for PositionState dataclass."""

    def test_position_state_default_position_value(self):
        """PositionState has default position_value of 0."""
        pos = PositionState(direction=Position.DIRECTION_LONG)
        assert pos.position_value == Decimal('0')

    def test_position_state_zero_values(self):
        """PositionState handles zero values correctly."""
        pos = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('0'),
            entry_price=Decimal('0'),
            unrealized_pnl=Decimal('0'),
            margin=Decimal('0'),
            liquidation_price=Decimal('0'),
            leverage=1,
            position_value=Decimal('0')
        )
        assert pos.size == Decimal('0')
        assert pos.entry_price == Decimal('0')

    def test_position_state_negative_unrealized_pnl(self):
        """PositionState can hold negative unrealized PnL (losing position)."""
        pos = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            unrealized_pnl=Decimal('-500.0'),
            margin=Decimal('1000.0'),
            liquidation_price=Decimal('90000.0'),
            leverage=10
        )
        assert pos.unrealized_pnl == Decimal('-500.0')


class TestPositionConstants:
    """Tests for Position class constants."""

    def test_side_constants(self):
        """SIDE_BUY and SIDE_SELL have correct values."""
        assert Position.SIDE_BUY == 'Buy'
        assert Position.SIDE_SELL == 'Sell'

    def test_direction_constants(self):
        """DIRECTION_LONG and DIRECTION_SHORT have correct values."""
        assert Position.DIRECTION_LONG == 'long'
        assert Position.DIRECTION_SHORT == 'short'

    def test_constants_used_in_initialization(self):
        """Constants are used correctly in Position initialization."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr = Position(Position.DIRECTION_LONG, risk_config)
        short_mgr = Position(Position.DIRECTION_SHORT, risk_config)

        assert long_mgr.direction == 'long'
        assert short_mgr.direction == 'short'
        assert Position.SIDE_BUY in long_mgr.amount_multiplier
        assert Position.SIDE_SELL in long_mgr.amount_multiplier


class TestPositionDirectMethods:
    """Direct tests for set/get methods."""

    def test_set_amount_multiplier_buy(self):
        """set_amount_multiplier sets Buy multiplier correctly."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        mgr = Position(Position.DIRECTION_LONG, risk_config)

        mgr.set_amount_multiplier(Position.SIDE_BUY, 2.5)
        assert mgr.amount_multiplier[Position.SIDE_BUY] == 2.5
        assert mgr.amount_multiplier[Position.SIDE_SELL] == 1.0  # Unchanged

    def test_set_amount_multiplier_sell(self):
        """set_amount_multiplier sets Sell multiplier correctly."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        mgr = Position(Position.DIRECTION_SHORT, risk_config)

        mgr.set_amount_multiplier(Position.SIDE_SELL, 0.3)
        assert mgr.amount_multiplier[Position.SIDE_SELL] == 0.3
        assert mgr.amount_multiplier[Position.SIDE_BUY] == 1.0  # Unchanged

    def test_get_amount_multiplier_returns_dict(self):
        """get_amount_multiplier returns the multiplier dictionary."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        mgr = Position(Position.DIRECTION_LONG, risk_config)
        mgr.set_amount_multiplier(Position.SIDE_BUY, 1.5)
        mgr.set_amount_multiplier(Position.SIDE_SELL, 0.5)

        result = mgr.get_amount_multiplier()
        assert result == {'Buy': 1.5, 'Sell': 0.5}
        assert result is mgr.amount_multiplier  # Same object

    def test_set_opposite_links_positions(self):
        """set_opposite correctly links positions."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr = Position(Position.DIRECTION_LONG, risk_config)
        short_mgr = Position(Position.DIRECTION_SHORT, risk_config)

        assert long_mgr._opposite is None
        assert short_mgr._opposite is None

        long_mgr.set_opposite(short_mgr)
        short_mgr.set_opposite(long_mgr)

        assert long_mgr._opposite is short_mgr
        assert short_mgr._opposite is long_mgr


class TestCalculateAmountMultiplierEdgeCases:
    """Edge case tests for calculate_amount_multiplier."""

    def test_entry_price_zero_returns_default(self):
        """Entry price of 0 returns default multipliers (early return)."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('0'),  # Zero entry price
            liquidation_price=Decimal('50000.0'),
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('2.0')
        )

        result = long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)
        assert result == {'Buy': 1.0, 'Sell': 1.0}

    def test_entry_price_none_returns_default(self):
        """Entry price of None returns default multipliers (early return)."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=None,  # None entry price
            liquidation_price=Decimal('50000.0'),
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('2.0')
        )

        result = long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)
        assert result == {'Buy': 1.0, 'Sell': 1.0}

    def test_last_close_zero_handles_division(self):
        """last_close of 0 causes division error in unrealized PnL calculation."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('50000.0'),
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('2.0')
        )

        # last_close=0 causes ZeroDivisionError in unrealized_pnl_pct calculation
        # (1 / entry_price - 1 / last_close) → 1/0 = ZeroDivisionError
        with pytest.raises(ZeroDivisionError):
            long_mgr.calculate_amount_multiplier(position, opposite, last_close=0.0)

    def test_opposite_margin_zero_uses_default(self):
        """opposite_margin of 0 uses 0.0001 default to avoid division error."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('50000.0'),
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('0')  # Zero margin
        )

        # Should not raise division by zero
        result = long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)
        assert 'Buy' in result
        assert 'Sell' in result
        # Position ratio should be very large (2.0 / 0.0001 = 20000)
        assert long_mgr.position_ratio == 2.0 / 0.0001

    def test_caller_must_reset_before_calculate(self):
        """Caller must reset multipliers before calculate_amount_multiplier.

        Matches bbu2 pattern: reset once, then calculate both directions.
        calculate_amount_multiplier does NOT reset internally.
        """
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, short_mgr = Position.create_linked_pair(risk_config)

        # Manually set multipliers to non-default values
        long_mgr.set_amount_multiplier(Position.SIDE_BUY, 5.0)
        long_mgr.set_amount_multiplier(Position.SIDE_SELL, 5.0)
        short_mgr.set_amount_multiplier(Position.SIDE_BUY, 5.0)
        short_mgr.set_amount_multiplier(Position.SIDE_SELL, 5.0)

        # Reset BOTH before calculating (caller responsibility)
        long_mgr.reset_amount_multiplier()
        short_mgr.reset_amount_multiplier()

        # Use a neutral position that won't trigger any rules
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('50000.0'),  # Safe liq ratio
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('2.0')
        )

        long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        # Both should still be at 1.0 (reset by caller, no rules triggered)
        assert short_mgr.amount_multiplier == {'Buy': 1.0, 'Sell': 1.0}

    def test_positive_unrealized_pnl_long(self):
        """Long position with positive PnL (profitable) and ratio > 0.2 doesn't trigger rules."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        # Position ratio 0.4/1.0 = 0.4 (between 0.2 and 0.5) and PROFITABLE
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.5'),
            entry_price=Decimal('95000.0'),  # Entry below current price
            liquidation_price=Decimal('50000.0'),  # Safe liq ratio
            margin=Decimal('0.4'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('1.0')  # ratio = 0.4 (> 0.2, so very small rule doesn't trigger)
        )

        result = long_mgr.calculate_amount_multiplier(
            position, opposite, last_close=100000.0  # Price above entry (profitable)
        )

        # ratio = 0.4 is between 0.2 and 0.5
        # Small losing rule requires ratio < 0.5 AND unrealized_pnl_pct < 0
        # This is profitable so small losing rule doesn't trigger
        # Very small rule requires ratio < 0.2, but 0.4 > 0.2
        assert result == {'Buy': 1.0, 'Sell': 1.0}


class TestPositionEqualityBoundaries:
    """Tests for position equality boundary conditions."""

    def _create_test_setup(self, long_margin, short_margin, min_total_margin=0.5):
        """Helper to create test setup with specified margins."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=min_total_margin
        )
        long_mgr, short_mgr = Position.create_linked_pair(risk_config)

        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('50000.0'),  # Safe liq ratio (0.5)
            margin=Decimal(str(long_margin)),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal(str(short_margin))
        )

        return long_mgr, position, opposite

    def test_position_ratio_exactly_0_94_not_equal(self):
        """position_ratio = 0.94 is NOT considered equal (boundary)."""
        long_mgr, position, opposite = self._create_test_setup(0.94, 1.0, min_total_margin=10.0)

        long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        # 0.94 is at boundary, should NOT be equal (0.94 < position_ratio < 1.05 is equal)
        # Actually checking: 0.94 < ratio, so ratio=0.94 is NOT > 0.94, so not equal
        is_equal = 0.94 < long_mgr.position_ratio < 1.05
        assert not is_equal  # 0.94 is not > 0.94

    def test_position_ratio_0_941_is_equal(self):
        """position_ratio = 0.941 is considered equal."""
        long_mgr, position, opposite = self._create_test_setup(0.941, 1.0, min_total_margin=10.0)

        long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        is_equal = 0.94 < long_mgr.position_ratio < 1.05
        assert is_equal  # 0.941 is > 0.94 and < 1.05

    def test_position_ratio_1_049_is_equal(self):
        """position_ratio = 1.049 is considered equal."""
        long_mgr, position, opposite = self._create_test_setup(1.049, 1.0, min_total_margin=10.0)

        long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        is_equal = 0.94 < long_mgr.position_ratio < 1.05
        assert is_equal  # 1.049 is > 0.94 and < 1.05

    def test_position_ratio_exactly_1_05_not_equal(self):
        """position_ratio = 1.05 is NOT considered equal (boundary)."""
        long_mgr, position, opposite = self._create_test_setup(1.05, 1.0, min_total_margin=10.0)

        long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        is_equal = 0.94 < long_mgr.position_ratio < 1.05
        assert not is_equal  # 1.05 is not < 1.05


class TestLongPositionRuleBoundaries:
    """Boundary tests for long position rules."""

    def test_high_liq_ratio_at_boundary(self):
        """High liq ratio exactly at 1.05 * min_liq_ratio triggers rule."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        # liq_ratio = 84000 / 100000 = 0.84 = 1.05 * 0.8
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('84000.0'),  # Exactly at boundary
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('2.0')
        )

        result = long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        # 0.84 is NOT > 0.84, so should NOT trigger high liq rule
        # Should fall through to moderate liq check: 0.84 > 0.8, so moderate triggers
        assert result['Sell'] == 1.0  # High liq didn't trigger

    def test_high_liq_ratio_just_above_boundary(self):
        """High liq ratio just above 1.05 * min_liq_ratio triggers rule."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        # liq_ratio = 84100 / 100000 = 0.841 > 0.84
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('84100.0'),  # Just above boundary
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('2.0')
        )

        result = long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        # 0.841 > 0.84, so high liq rule triggers
        assert result['Sell'] == 1.5

    def test_moderate_liq_ratio_at_boundary(self):
        """Moderate liq ratio exactly at min_liq_ratio triggers rule."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, short_mgr = Position.create_linked_pair(risk_config)

        # liq_ratio = 80000 / 100000 = 0.8 = min_liq_ratio
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('80000.0'),  # Exactly at boundary
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('2.0')
        )

        result = long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        # 0.8 is NOT > 0.8, so moderate liq rule should NOT trigger
        assert result == {'Buy': 1.0, 'Sell': 1.0}
        assert short_mgr.amount_multiplier == {'Buy': 1.0, 'Sell': 1.0}

    def test_position_ratio_exactly_0_5_boundary(self):
        """position_ratio exactly 0.5 doesn't trigger small losing rule."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        # ratio = 0.5 / 1.0 = 0.5
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.5'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('50000.0'),  # Safe liq ratio
            margin=Decimal('0.5'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('1.0')
        )

        result = long_mgr.calculate_amount_multiplier(
            position, opposite, last_close=95000.0  # Losing
        )

        # ratio = 0.5 is NOT < 0.5, so small losing rule doesn't trigger
        # But 0.5 > 0.2, so very small rule doesn't trigger either
        assert result == {'Buy': 1.0, 'Sell': 1.0}

    def test_position_ratio_exactly_0_20_boundary(self):
        """position_ratio exactly 0.20 doesn't trigger very small rule."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        # ratio = 0.2 / 1.0 = 0.2
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.2'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('50000.0'),  # Safe liq ratio
            margin=Decimal('0.2'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('1.0')
        )

        result = long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        # ratio = 0.2 is NOT < 0.2, so very small rule doesn't trigger
        assert result == {'Buy': 1.0, 'Sell': 1.0}

    def test_small_position_but_profitable_no_increase(self):
        """Small long position that's profitable doesn't trigger small losing rule."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        # ratio = 0.3 / 1.0 = 0.3 (< 0.5 but profitable)
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.3'),
            entry_price=Decimal('95000.0'),  # Entry below current (profitable)
            liquidation_price=Decimal('50000.0'),  # Safe liq ratio
            margin=Decimal('0.3'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('1.0')
        )

        result = long_mgr.calculate_amount_multiplier(
            position, opposite, last_close=100000.0  # Price above entry (profitable)
        )

        # ratio < 0.5 but unrealized_pnl_pct > 0 (profitable), so small losing rule doesn't trigger
        # ratio > 0.2, so very small rule doesn't trigger
        assert result == {'Buy': 1.0, 'Sell': 1.0}


class TestShortPositionRuleBoundaries:
    """Boundary tests for short position rules."""

    def test_high_liq_ratio_at_boundary(self):
        """High liq ratio exactly at 0.95 * max_liq_ratio triggers rule."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, short_mgr = Position.create_linked_pair(risk_config)

        # liq_ratio = 114000 / 100000 = 1.14 = 0.95 * 1.2
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('114000.0'),  # Exactly at boundary
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            margin=Decimal('2.0')
        )

        result = short_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        # 1.14 is NOT > 1.14, so high liq rule should NOT trigger
        assert result['Buy'] == 1.0

    def test_high_liq_ratio_just_above_boundary(self):
        """High liq ratio just above 0.95 * max_liq_ratio triggers rule."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, short_mgr = Position.create_linked_pair(risk_config)

        # liq_ratio = 114100 / 100000 = 1.141 > 1.14
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('114100.0'),  # Just above boundary
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            margin=Decimal('2.0')
        )

        result = short_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        # 1.141 > 1.14, so high liq rule triggers
        assert result['Buy'] == 1.5

    def test_position_ratio_exactly_2_0_boundary(self):
        """position_ratio exactly 2.0 doesn't trigger large losing rule."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, short_mgr = Position.create_linked_pair(risk_config)

        # ratio = 2.0 / 1.0 = 2.0
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('2.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('110000.0'),  # Safe liq ratio
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            margin=Decimal('1.0')
        )

        result = short_mgr.calculate_amount_multiplier(
            position, opposite, last_close=105000.0  # Price above entry (losing for short)
        )

        # ratio = 2.0 is NOT > 2.0, so large losing rule doesn't trigger
        # But moderate liq might trigger if liq_ratio in range
        # liq_ratio = 110000 / 105000 = 1.048, which is between 0 and 1.2
        # But not high enough for emergency
        # Should trigger moderate liq rule
        assert result == {'Buy': 1.0, 'Sell': 1.0}

    def test_position_ratio_exactly_5_0_boundary(self):
        """position_ratio exactly 5.0 doesn't trigger very large rule."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, short_mgr = Position.create_linked_pair(risk_config)

        # ratio = 5.0 / 1.0 = 5.0
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('5.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('110000.0'),  # Safe liq ratio
            margin=Decimal('5.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            margin=Decimal('1.0')
        )

        result = short_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        # ratio = 5.0 is NOT > 5.0, so very large rule doesn't trigger
        assert result == {'Buy': 1.0, 'Sell': 1.0}

    def test_large_position_but_profitable_no_increase(self):
        """Large short position that's profitable doesn't trigger large losing rule."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, short_mgr = Position.create_linked_pair(risk_config)

        # ratio = 3.0 / 1.0 = 3.0 (> 2.0 but profitable)
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('3.0'),
            entry_price=Decimal('105000.0'),  # Entry above current (profitable for short)
            liquidation_price=Decimal('110000.0'),  # Safe liq ratio
            margin=Decimal('3.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            margin=Decimal('1.0')
        )

        result = short_mgr.calculate_amount_multiplier(
            position, opposite, last_close=100000.0  # Price below entry (profitable for short)
        )

        # ratio > 2.0 but unrealized_pnl_pct > 0 (profitable), so large losing rule doesn't trigger
        # ratio < 5.0, so very large rule doesn't trigger
        assert result == {'Buy': 1.0, 'Sell': 1.0}

    def test_short_low_margin_increase_same_position(self):
        """Short position with low margin and increase_same_position_on_low_margin=True."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.5,  # Higher max to avoid triggering high liq rule
            max_margin=5.0,
            min_total_margin=1.5,
            increase_same_position_on_low_margin=True
        )
        _, short_mgr = Position.create_linked_pair(risk_config)

        # Equal positions with low total margin
        # liq_ratio = 113000 / 100000 = 1.13 (below 0.95 * 1.5 = 1.425)
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('113000.0'),  # Safe liq ratio for short
            margin=Decimal('0.4'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            margin=Decimal('0.4')  # Equal margin, total = 0.8 < 1.5
        )

        result = short_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        # Should double sell multiplier for short
        assert result['Sell'] == 2.0
        assert result['Buy'] == 1.0

    def test_short_low_margin_reduce_opposite(self):
        """Short position with low margin and increase_same_position_on_low_margin=False."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.5,  # Higher max to avoid triggering high liq rule
            max_margin=5.0,
            min_total_margin=1.5,
            increase_same_position_on_low_margin=False
        )
        _, short_mgr = Position.create_linked_pair(risk_config)

        # Equal positions with low total margin
        # liq_ratio = 113000 / 100000 = 1.13 (below 0.95 * 1.5 = 1.425)
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('113000.0'),  # Safe liq ratio for short
            margin=Decimal('0.4'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            margin=Decimal('0.4')  # Equal margin, total = 0.8 < 1.5
        )

        result = short_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        # Should reduce buy multiplier for short
        assert result['Buy'] == 0.5
        assert result['Sell'] == 1.0

    def test_moderate_liq_at_max_boundary(self):
        """Moderate liq exactly at max_liq_ratio triggers high liq rule for shorts."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, short_mgr = Position.create_linked_pair(risk_config)

        # liq_ratio = 120000 / 100000 = 1.2 = max_liq_ratio
        # For shorts, high liq triggers when liq_ratio > 0.95 * max_liq_ratio = 1.14
        # Since 1.2 > 1.14, high liq rule triggers
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('120000.0'),  # Exactly at max (triggers high liq)
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            margin=Decimal('2.0')
        )

        result = short_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)

        # liq_ratio = 1.2 > 0.95 * 1.2 = 1.14, so HIGH liq rule triggers (not moderate)
        assert result == {'Buy': 1.5, 'Sell': 1.0}
        # Long multipliers are reset but not modified by short's high liq rule
        assert long_mgr.amount_multiplier == {'Buy': 1.0, 'Sell': 1.0}


class TestLiquidationRatioEdgeCases:
    """Edge case tests for _get_liquidation_ratio."""

    def test_zero_liq_price(self):
        """Zero liquidation price returns ratio of 0."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        mgr = Position(Position.DIRECTION_LONG, risk_config)

        ratio = mgr._get_liquidation_ratio(Decimal('0'), 100000.0)
        assert ratio == 0.0

    def test_negative_liq_price(self):
        """Negative liquidation price returns negative ratio."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        mgr = Position(Position.DIRECTION_LONG, risk_config)

        ratio = mgr._get_liquidation_ratio(Decimal('-50000'), 100000.0)
        assert ratio == -0.5

    def test_last_close_zero_returns_zero(self):
        """last_close of 0 returns ratio of 0 (avoids division error)."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        mgr = Position(Position.DIRECTION_LONG, risk_config)

        ratio = mgr._get_liquidation_ratio(Decimal('50000'), 0.0)
        assert ratio == 0.0


class TestUnrealizedPnLCalculation:
    """Tests to verify unrealized PnL calculation logic."""

    def test_long_positive_pnl_when_price_up(self):
        """Long position has positive PnL when price goes up."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        # Entry at 100000, current at 110000 (10% up)
        # Small position ratio so we can test small losing rule behavior
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.3'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('50000.0'),
            margin=Decimal('0.3'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('1.0')
        )

        # Price UP = profitable for long
        result = long_mgr.calculate_amount_multiplier(position, opposite, last_close=110000.0)

        # ratio < 0.5 but profitable, so small losing rule doesn't trigger
        # ratio > 0.2, so very small rule doesn't trigger
        assert result == {'Buy': 1.0, 'Sell': 1.0}

    def test_short_positive_pnl_when_price_down(self):
        """Short position has positive PnL when price goes down."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=2.0,  # Higher max to avoid triggering high liq rule
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, short_mgr = Position.create_linked_pair(risk_config)

        # Entry at 100000, current at 90000 (10% down)
        # Large position ratio so we can test large losing rule behavior
        # liq_ratio = 100000 / 90000 = 1.11 (safe, below 0.95 * 2.0 = 1.9)
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('3.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('100000.0'),  # Safe liq ratio
            margin=Decimal('3.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            margin=Decimal('1.0')
        )

        # Price DOWN = profitable for short
        result = short_mgr.calculate_amount_multiplier(position, opposite, last_close=90000.0)

        # ratio > 2.0 but profitable, so large losing rule doesn't trigger
        # ratio < 5.0, so very large rule doesn't trigger
        # liq_ratio = 100000/90000 = 1.11, which is < 1.9 (0.95 * 2.0), so no high liq
        # And it's in moderate range (0 < 1.11 < 2.0), which will trigger moderate rule
        # But that modifies the opposite (long) position, not this one
        assert result == {'Buy': 1.0, 'Sell': 1.0}


class TestPositionRiskManagerAlias:
    """Test that PositionRiskManager is an alias for Position."""

    def test_alias_is_same_class(self):
        """PositionRiskManager is the same class as Position."""
        assert PositionRiskManager is Position


class TestCreateLinkedPairEdgeCases:
    """Edge case tests for create_linked_pair."""

    def test_default_short_config_copies_long(self):
        """When short_config is None, it uses long_config for both."""
        long_config = RiskConfig(
            min_liq_ratio=0.75,
            max_liq_ratio=1.25,
            max_margin=6.0,
            min_total_margin=1.5
        )

        long_mgr, short_mgr = Position.create_linked_pair(long_config)

        # Both should use the same config
        assert long_mgr.risk_config is long_config
        assert short_mgr.risk_config is long_config

class TestEdgeValues:
    """Tests for edge value scenarios."""

    def test_very_large_leverage(self):
        """Very large leverage values work correctly."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('99000.0'),  # Very close to current
            margin=Decimal('100.0'),
            leverage=125  # High leverage
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('100.0')
        )

        # Should not raise error
        result = long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)
        assert 'Buy' in result
        assert 'Sell' in result

    def test_very_small_margin(self):
        """Very small margin values work correctly."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.001'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('50000.0'),
            margin=Decimal('0.0001'),  # Very small margin
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('0.0001')
        )

        # Should not raise error
        result = long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)
        assert 'Buy' in result
        assert 'Sell' in result

    def test_decimal_precision(self):
        """Decimal precision is maintained correctly."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        long_mgr, _ = Position.create_linked_pair(risk_config)

        # Use precise decimal values
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.123456789'),
            entry_price=Decimal('99999.123456789'),
            liquidation_price=Decimal('50000.987654321'),
            margin=Decimal('2.111111111'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            margin=Decimal('2.222222222')
        )

        # Should not raise error and maintain precision in calculations
        result = long_mgr.calculate_amount_multiplier(position, opposite, last_close=100000.0)
        assert 'Buy' in result
        assert 'Sell' in result
