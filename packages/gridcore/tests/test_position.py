"""
Unit tests for Position module.
"""

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
        manager = PositionRiskManager('long', risk_config)
        assert manager.direction == 'long'
        assert manager.amount_multiplier['Buy'] == 1.0
        assert manager.amount_multiplier['Sell'] == 1.0

    def test_reset_amount_multiplier(self):
        """Reset multiplier returns to 1.0."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        manager = PositionRiskManager('long', risk_config)

        # Modify multipliers
        manager.amount_multiplier['Buy'] = 2.0
        manager.amount_multiplier['Sell'] = 0.5

        # Reset
        manager.reset_amount_multiplier()
        assert manager.amount_multiplier['Buy'] == 1.0
        assert manager.amount_multiplier['Sell'] == 1.0

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

        multipliers = long_manager.calculate_amount_multiplier(
            position,
            opposite,
            last_close=100000.0,
            wallet_balance=Decimal('10000.0')
        )

        # Should return multipliers dict
        assert 'Buy' in multipliers
        assert 'Sell' in multipliers
        assert isinstance(multipliers['Buy'], float)
        assert isinstance(multipliers['Sell'], float)

    def test_calculate_amount_multiplier_short_position(self):
        """Calculate multipliers for short position."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, short_manager = PositionRiskManager.create_linked_pair(risk_config)

        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('150000.0'),
            margin=Decimal('2.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('50000.0'),
            margin=Decimal('2.0'),
            leverage=10
        )

        multipliers = short_manager.calculate_amount_multiplier(
            position,
            opposite,
            last_close=100000.0,
            wallet_balance=Decimal('10000.0')
        )

        # Should return multipliers dict
        assert 'Buy' in multipliers
        assert 'Sell' in multipliers


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
        manager = PositionRiskManager('long', risk_config)
        # Don't set opposite - should raise error

        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('3.37'),
            entry_price=Decimal('3100.0'),
            liquidation_price=Decimal('0.0'),
            margin=Decimal('0.51'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('4.62'),
            entry_price=Decimal('3102.0'),
            liquidation_price=Decimal('17553.0'),
            margin=Decimal('0.71'),
            leverage=10
        )

        # Should raise ValueError with helpful message
        try:
            manager.calculate_amount_multiplier(
                position, opposite, last_close=100000.0, wallet_balance=Decimal('10000.0')
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
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('3.36'),
            entry_price=Decimal('3100.0'),
            liquidation_price=Decimal('0.0'),
            margin=Decimal('0.51'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('4.62'),
            entry_price=Decimal('3102.0'),
            liquidation_price=Decimal('17553.0'),
            margin=Decimal('0.71'),
            leverage=10
        )

        # Should not raise error
        multipliers = long_mgr.calculate_amount_multiplier(
            position, opposite, last_close=3300.0, wallet_balance=Decimal('10000.0')
        )
        assert 'Buy' in multipliers
        assert 'Sell' in multipliers

    def test_high_liquidation_ratio_long_decreases_position(self):
        """High liquidation ratio for long position decreases long (increases sell multiplier)."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # High liquidation risk: liq_ratio > 1.05 * min_liq_ratio (0.84)
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('2.5'),
            entry_price=Decimal('3300.0'),
            liquidation_price=Decimal('2635.0'),
            margin=Decimal('0.39'),
            leverage=10
        )

        # Short position exists (realistic hedged scenario)
        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('4.62'),
            entry_price=Decimal('3102.0'),
            liquidation_price=Decimal('17553.0'),
            margin=Decimal('0.71'),
            leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(
            position, opposite, last_close=3100.0, wallet_balance=Decimal('10000.0')
        )

        # Should increase sell multiplier to decrease long position
        assert multipliers['Sell'] == 1.5
        assert multipliers['Buy'] == 1.0

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
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('81000.0'),  # liq_ratio = 0.81
            margin=Decimal('2.0'),
            leverage=10
        )

        # Short position exists but is smaller (realistic hedged scenario)
        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('0.5'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('115000.0'),  # Safe liq ratio for short
            margin=Decimal('1.0'),
            leverage=10
        )
        long_multipliers = long_manager.calculate_amount_multiplier(
            position, opposite, last_close=100000.0, wallet_balance=Decimal('10000.0')
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
        _, manager = PositionRiskManager.create_linked_pair(risk_config)

        # High liquidation risk: liq_ratio < 0.95 * max_liq_ratio (1.14)
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('115000.0'),  # liq_ratio = 1.15
            margin=Decimal('2.0'),
            leverage=10
        )

        # Long position exists (realistic hedged scenario)
        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.5'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('90000.0'),
            margin=Decimal('1.0'),
            leverage=10
        )
        multipliers = manager.calculate_amount_multiplier(
            position, opposite, last_close=100000.0, wallet_balance=Decimal('10000.0')
        )

        # Should increase buy multiplier to decrease short position
        assert multipliers['Buy'] == 1.5
        assert multipliers['Sell'] == 1.0

    def test_low_total_margin_with_equal_positions(self):
        """Low total margin with equal positions triggers adjustment."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1000.0,
            increase_same_position_on_low_margin=False
        )
        manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # Equal positions (ratio ~1.0) but low total margin
        # liq_ratio = 70000 / 100000 = 0.7 (below min 0.8, safe)
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('70000.0'),  # Safe liq ratio
            margin=Decimal('0.8'),  # Low margin
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('130000.0'),  # Safe liq ratio
            margin=Decimal('0.8'),  # Equal margin (ratio = 1.0)
            leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(
            position, opposite, last_close=100000.0, wallet_balance=Decimal('10000.0')
        )

        # Should reduce opposite side (sell) to increase long position
        assert multipliers['Sell'] == 0.5
        assert multipliers['Buy'] == 1.0

    def test_low_total_margin_increase_same_position(self):
        """Low total margin with increase_same_position_on_low_margin=True doubles same side."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1000.0,
            increase_same_position_on_low_margin=True
        )
        manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # Equal positions but low total margin
        # liq_ratio = 70000 / 100000 = 0.7 (below min 0.8, safe)
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('70000.0'),  # Safe liq ratio
            margin=Decimal('0.8'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('130000.0'),  # Safe liq ratio
            margin=Decimal('0.8'),
            leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(
            position, opposite, last_close=100000.0, wallet_balance=Decimal('10000.0')
        )

        # Should double buy multiplier to increase long position
        assert multipliers['Buy'] == 2.0
        assert multipliers['Sell'] == 1.0

    def test_small_long_position_losing_increases_long(self):
        """Small long position that's losing increases long multiplier."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # Small position (ratio < 0.5) and losing (price below entry)
        # liq_ratio = 70000 / 95000 = 0.737 (below min 0.8, safe)
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.5'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('70000.0'),  # Safe liq ratio
            margin=Decimal('0.4'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('125000.0'),  # Safe liq ratio
            margin=Decimal('2.0'),  # Much larger (ratio = 0.2)
            leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(
            position, opposite, last_close=95000.0,  # Price below entry (losing)
            wallet_balance=Decimal('10000.0')
        )

        # Should increase buy multiplier
        assert multipliers['Buy'] == 2.0
        assert multipliers['Sell'] == 1.0

    def test_very_small_long_position_increases_long(self):
        """Very small long position (ratio < 0.20) increases long multiplier."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # Very small position (ratio < 0.20)
        # liq_ratio = 70000 / 100000 = 0.7 (below min 0.8, safe)
        position = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.1'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('70000.0'),  # Safe liq ratio
            margin=Decimal('0.2'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('1.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('130000.0'),  # Safe liq ratio
            margin=Decimal('2.0'),  # Much larger (ratio = 0.1)
            leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(
            position, opposite, last_close=100000.0, wallet_balance=Decimal('10000.0')
        )

        # Should increase buy multiplier
        assert multipliers['Buy'] == 2.0
        assert multipliers['Sell'] == 1.0

    def test_large_short_position_losing_increases_short(self):
        """Large short position that's losing increases short multiplier."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, manager = PositionRiskManager.create_linked_pair(risk_config)

        # Large position (ratio > 2.0) and losing (price above entry)
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('2.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('110000.0'),
            margin=Decimal('4.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.5'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('90000.0'),
            margin=Decimal('1.0'),  # Much smaller (ratio = 4.0)
            leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(
            position, opposite, last_close=105000.0,  # Price above entry (losing)
            wallet_balance=Decimal('10000.0')
        )

        # Should increase sell multiplier
        assert multipliers['Sell'] == 2.0
        assert multipliers['Buy'] == 1.0

    def test_very_large_short_position_increases_short(self):
        """Very large short position (ratio > 5.0) increases short multiplier."""
        risk_config = RiskConfig(
            min_liq_ratio=0.8,
            max_liq_ratio=1.2,
            max_margin=5.0,
            min_total_margin=1.0
        )
        _, manager = PositionRiskManager.create_linked_pair(risk_config)

        # Very large position (ratio > 5.0)
        position = PositionState(
            direction=Position.DIRECTION_SHORT,
            size=Decimal('5.0'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('110000.0'),
            margin=Decimal('4.0'),
            leverage=10
        )

        opposite = PositionState(
            direction=Position.DIRECTION_LONG,
            size=Decimal('0.5'),
            entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('90000.0'),
            margin=Decimal('0.4'),  # Much smaller (ratio = 10.0)
            leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(
            position, opposite, last_close=100000.0, wallet_balance=Decimal('10000.0')
        )

        # Should increase sell multiplier
        assert multipliers['Sell'] == 2.0
        assert multipliers['Buy'] == 1.0

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
            position, opposite, last_close=100000.0, wallet_balance=Decimal('10000.0')
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
