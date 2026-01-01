#!/usr/bin/env python3
"""
Unit tests for Position._adjust_position_for_low_margin() method.

Tests verify that the low margin adjustment logic works correctly for all combinations:
- Long/Short positions
- increase_same_position_on_low_margin True/False
"""

import unittest
from unittest.mock import Mock
from position import Position


class TestPositionLowMarginAdjustment(unittest.TestCase):
    """Test cases for the _adjust_position_for_low_margin method."""

    def setUp(self):
        """Set up mock strategy for testing."""
        self.mock_strat = Mock()
        self.mock_strat.liq_ratio = {'min': 0.8, 'max': 1.2}
        self.mock_strat.max_margin = 8
        self.mock_strat.min_total_margin = 2
        self.mock_strat.id = 1

    def test_long_position_increase_same_position_true(self):
        """Test long position with increase_same_position_on_low_margin=True."""
        # Setup
        self.mock_strat.increase_same_position_on_low_margin = True
        position = Position('long', self.mock_strat)

        # Execute
        position._adjust_position_for_low_margin()

        # Verify: Should increase long position by setting BUY multiplier to 2.0
        multipliers = position.get_amount_multiplier()
        self.assertEqual(multipliers[Position.SIDE_BUY], 2.0)
        self.assertEqual(multipliers[Position.SIDE_SELL], 1.0)  # Should remain default

    def test_long_position_increase_same_position_false(self):
        """Test long position with increase_same_position_on_low_margin=False."""
        # Setup
        self.mock_strat.increase_same_position_on_low_margin = False
        position = Position('long', self.mock_strat)

        # Execute
        position._adjust_position_for_low_margin()

        # Verify: Should increase long position by setting SELL multiplier to 0.5
        multipliers = position.get_amount_multiplier()
        self.assertEqual(multipliers[Position.SIDE_BUY], 1.0)   # Should remain default
        self.assertEqual(multipliers[Position.SIDE_SELL], 0.5)

    def test_short_position_increase_same_position_true(self):
        """Test short position with increase_same_position_on_low_margin=True."""
        # Setup
        self.mock_strat.increase_same_position_on_low_margin = True
        position = Position('short', self.mock_strat)

        # Execute
        position._adjust_position_for_low_margin()

        # Verify: Should increase short position by setting SELL multiplier to 2.0
        multipliers = position.get_amount_multiplier()
        self.assertEqual(multipliers[Position.SIDE_BUY], 1.0)   # Should remain default
        self.assertEqual(multipliers[Position.SIDE_SELL], 2.0)

    def test_short_position_increase_same_position_false(self):
        """Test short position with increase_same_position_on_low_margin=False."""
        # Setup
        self.mock_strat.increase_same_position_on_low_margin = False
        position = Position('short', self.mock_strat)

        # Execute
        position._adjust_position_for_low_margin()

        # Verify: Should increase short position by setting BUY multiplier to 0.5
        multipliers = position.get_amount_multiplier()
        self.assertEqual(multipliers[Position.SIDE_BUY], 0.5)
        self.assertEqual(multipliers[Position.SIDE_SELL], 1.0)  # Should remain default

    def test_multiplier_reset_between_calls(self):
        """Test that multipliers can be reset and adjusted multiple times."""
        # Setup
        self.mock_strat.increase_same_position_on_low_margin = True
        position = Position('long', self.mock_strat)

        # First adjustment
        position._adjust_position_for_low_margin()
        multipliers = position.get_amount_multiplier()
        self.assertEqual(multipliers[Position.SIDE_BUY], 2.0)

        # Reset multipliers
        position.reset_amount_multiplier()
        multipliers = position.get_amount_multiplier()
        self.assertEqual(multipliers[Position.SIDE_BUY], 1.0)
        self.assertEqual(multipliers[Position.SIDE_SELL], 1.0)

        # Second adjustment with same setting (since setting is stored at initialization)
        position._adjust_position_for_low_margin()
        multipliers = position.get_amount_multiplier()
        self.assertEqual(multipliers[Position.SIDE_BUY], 2.0)  # Should be 2.0 again
        self.assertEqual(multipliers[Position.SIDE_SELL], 1.0)

    def test_different_settings_require_new_instances(self):
        """Test that different settings require creating new Position instances."""
        # Test with increase_same_position_on_low_margin = True
        self.mock_strat.increase_same_position_on_low_margin = True
        position_true = Position('long', self.mock_strat)
        position_true._adjust_position_for_low_margin()
        multipliers_true = position_true.get_amount_multiplier()

        # Test with increase_same_position_on_low_margin = False
        self.mock_strat.increase_same_position_on_low_margin = False
        position_false = Position('long', self.mock_strat)
        position_false._adjust_position_for_low_margin()
        multipliers_false = position_false.get_amount_multiplier()

        # Verify different behaviors
        self.assertEqual(multipliers_true[Position.SIDE_BUY], 2.0)
        self.assertEqual(multipliers_true[Position.SIDE_SELL], 1.0)

        self.assertEqual(multipliers_false[Position.SIDE_BUY], 1.0)
        self.assertEqual(multipliers_false[Position.SIDE_SELL], 0.5)

    def test_all_combinations_matrix(self):
        """Test all combinations in a matrix to ensure comprehensive coverage."""
        test_cases = [
            # (direction, increase_same_position, expected_buy_mult, expected_sell_mult)
            ('long', True, 2.0, 1.0),   # Long + True: increase BUY
            ('long', False, 1.0, 0.5),  # Long + False: decrease SELL
            ('short', True, 1.0, 2.0),  # Short + True: increase SELL
            ('short', False, 0.5, 1.0), # Short + False: decrease BUY
        ]

        for direction, increase_same, expected_buy, expected_sell in test_cases:
            with self.subTest(direction=direction, increase_same=increase_same):
                # Setup
                self.mock_strat.increase_same_position_on_low_margin = increase_same
                position = Position(direction, self.mock_strat)

                # Execute
                position._adjust_position_for_low_margin()

                # Verify
                multipliers = position.get_amount_multiplier()
                self.assertEqual(
                    multipliers[Position.SIDE_BUY],
                    expected_buy,
                    f"BUY multiplier mismatch for {direction} position with increase_same={increase_same}"
                )
                self.assertEqual(
                    multipliers[Position.SIDE_SELL],
                    expected_sell,
                    f"SELL multiplier mismatch for {direction} position with increase_same={increase_same}"
                )


if __name__ == '__main__':
    # Run tests with verbose output
    unittest.main(verbosity=2)