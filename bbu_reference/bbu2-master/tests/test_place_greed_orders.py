import unittest
from unittest.mock import Mock
import sys
import os

# Add parent directory to path to import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from strat import Strat50


class TestPlaceGreedOrders(unittest.TestCase):
    """Test suite for the place_greed_orders method"""

    def setUp(self):
        """Set up test fixtures before each test"""
        # Mock controller
        self.mock_controller = Mock()
        
        # Create Strat50 instance with minimal required parameters
        self.strat = Strat50(
            controller=self.mock_controller,
            id=1,
            strat='Strat50',
            symbol='BTCUSDT',
            greed_step=0.5,
            greed_count=10,
            direction='long',
            exchange='bybit_usdt',
            max_margin=1000,
            min_liq_ratio=0.1,
            max_liq_ratio=0.9,
            min_total_margin=100,
            long_koef=1.0,
            increase_same_position_on_low_margin=False
        )
        
        # Mock dependencies
        self.strat.place_order = Mock(return_value=50000.0)
        self.strat.cancel_order = Mock()
        
        # Mock greed object
        self.strat.greed = Mock()
        self.strat.greed.WAIT = 'wait'
        self.strat.greed.BUY = 'Buy'
        self.strat.greed.SELL = 'Sell'

    def test_empty_limits_places_all_orders(self):
        """Test that all greed orders are placed when there are no existing limits"""
        # Setup greed with 5 orders
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.0},
            {'side': 'Buy', 'price': 49500.0},
            {'side': 'wait', 'price': 50000.0},
            {'side': 'Sell', 'price': 50500.0},
            {'side': 'Sell', 'price': 51000.0},
        ]
        
        # Call with empty limits
        self.strat.place_greed_orders([], 'long')
        
        # Verify place_order was called for all non-WAIT orders (4 times)
        self.assertEqual(self.strat.place_order.call_count, 4)
        self.strat.cancel_order.assert_not_called()

    def test_matching_orders_not_replaced(self):
        """Test that orders with matching price and side are not touched"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.0},
            {'side': 'wait', 'price': 50000.0},
            {'side': 'Sell', 'price': 51000.0},
        ]
        
        # Limits that match greed orders
        limits = [
            {'price': '49000.0', 'side': 'Buy', 'orderId': 'order1'},
            {'price': '51000.0', 'side': 'Sell', 'orderId': 'order2'},
        ]
        
        self.strat.place_greed_orders(limits, 'long')
        
        # No orders should be placed or cancelled
        self.strat.place_order.assert_not_called()
        self.strat.cancel_order.assert_not_called()

    def test_mismatched_side_cancels_and_replaces(self):
        """Test that orders with matching price but wrong side are cancelled and replaced"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.0},
            {'side': 'wait', 'price': 50000.0},
        ]
        
        # Limit with wrong side
        limits = [
            {'price': '49000.0', 'side': 'Sell', 'orderId': 'order1'},
        ]
        
        self.strat.place_greed_orders(limits, 'long')
        
        # Should cancel old order and place new one
        self.strat.cancel_order.assert_called_once_with('order1')
        self.strat.place_order.assert_called_once()

    def test_orders_outside_greed_range_cancelled(self):
        """Test that limit orders outside the greed price range are cancelled"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.0},
            {'side': 'wait', 'price': 50000.0},
            {'side': 'Sell', 'price': 51000.0},
        ]
        
        # Limits outside greed range
        limits = [
            {'price': '48000.0', 'side': 'Buy', 'orderId': 'order1'},  # Too low
            {'price': '52000.0', 'side': 'Sell', 'orderId': 'order2'},  # Too high
        ]
        
        self.strat.place_greed_orders(limits, 'long')
        
        # Both should be cancelled
        self.assertEqual(self.strat.cancel_order.call_count, 2)
        self.strat.cancel_order.assert_any_call('order1')
        self.strat.cancel_order.assert_any_call('order2')

    def test_floating_point_precision_handled(self):
        """Test that floating-point precision issues don't cause incorrect cancellations"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.123456781},  # Rounds to 49000.12345678
            {'side': 'wait', 'price': 50000.0},
        ]
        
        # Limit with slight floating point difference (rounds to same value at 8 decimals)
        # Both 49000.123456781 and 49000.123456784 round to 49000.12345678
        limits = [
            {'price': '49000.123456784', 'side': 'Buy', 'orderId': 'order1'},
        ]
        
        self.strat.place_greed_orders(limits, 'long')
        
        # Should NOT be cancelled (within 8 decimal tolerance)
        self.strat.cancel_order.assert_not_called()
    
    def test_floating_point_beyond_tolerance_cancelled(self):
        """Test that differences beyond 8 decimal places are detected"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.12345678},
            {'side': 'wait', 'price': 50000.0},
        ]
        
        # Limit with difference that persists after 8 decimal rounding
        limits = [
            {'price': '49000.12345679', 'side': 'Buy', 'orderId': 'order1'},
        ]
        
        self.strat.place_greed_orders(limits, 'long')
        
        # Should be cancelled (outside 8 decimal tolerance)
        self.strat.cancel_order.assert_called_once_with('order1')

    def test_string_to_float_conversion(self):
        """Test that API string prices are correctly converted to floats for comparison"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.0},
            {'side': 'wait', 'price': 50000.0},
        ]
        
        # Limit with string price (as returned by API)
        limits = [
            {'price': '49000.0', 'side': 'Buy', 'orderId': 'order1'},
        ]
        
        self.strat.place_greed_orders(limits, 'long')
        
        # Should match correctly (no new orders placed)
        self.strat.place_order.assert_not_called()
        self.strat.cancel_order.assert_not_called()

    def test_wait_orders_skipped(self):
        """Test that WAIT orders are never placed"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.0},
            {'side': 'wait', 'price': 50000.0},
            {'side': 'wait', 'price': 50100.0},
            {'side': 'Sell', 'price': 51000.0},
        ]
        
        self.strat.place_greed_orders([], 'long')
        
        # Only 2 orders should be placed (not the WAIT ones)
        self.assertEqual(self.strat.place_order.call_count, 2)

    def test_center_based_ordering_with_wait_region(self):
        """Test that orders are placed from center outward when WAIT region exists"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 48000.0},    # index 0
            {'side': 'Buy', 'price': 49000.0},    # index 1
            {'side': 'wait', 'price': 50000.0},   # index 2 (center start)
            {'side': 'wait', 'price': 50100.0},   # index 3 (center end)
            {'side': 'Sell', 'price': 51000.0},   # index 4
            {'side': 'Sell', 'price': 52000.0},   # index 5
        ]
        
        # Track order placement sequence
        placed_prices = []
        self.strat.place_order.side_effect = lambda greed, direction: placed_prices.append(greed['price'])
        
        self.strat.place_greed_orders([], 'long')
        
        # Center is between indices 2 and 3, so center_index = (2+3)//2 = 2
        # Distance from center:
        # index 1 (49000): distance = |1-2| = 1, price = 49000.0
        # index 0 (48000): distance = |0-2| = 2, price = 48000.0
        # index 4 (51000): distance = |4-2| = 2, price = 51000.0
        # index 5 (52000): distance = |5-2| = 3, price = 52000.0
        # Sorted by (distance, price): (1,49000) < (2,48000) < (2,51000) < (3,52000)
        
        # Verify order placement from center outward, with price as secondary sort
        self.assertEqual(placed_prices[0], 49000.0)  # Closest (distance 1)
        self.assertEqual(placed_prices[1], 48000.0)  # Distance 2, lower price
        self.assertEqual(placed_prices[2], 51000.0)  # Distance 2, higher price
        self.assertEqual(placed_prices[3], 52000.0)  # Farthest (distance 3)

    def test_center_based_ordering_without_wait_region(self):
        """Test that orders are placed from center outward when no WAIT region exists"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 48000.0},    # index 0
            {'side': 'Buy', 'price': 49000.0},    # index 1
            {'side': 'Sell', 'price': 51000.0},   # index 2 (center)
            {'side': 'Sell', 'price': 52000.0},   # index 3
        ]
        
        # Track order placement sequence
        placed_prices = []
        self.strat.place_order.side_effect = lambda greed, direction: placed_prices.append(greed['price'])
        
        self.strat.place_greed_orders([], 'long')
        
        # Center is 4//2 = 2
        # Distance from center:
        # index 2 (51000): distance = |2-2| = 0, price = 51000.0
        # index 1 (49000): distance = |1-2| = 1, price = 49000.0
        # index 3 (52000): distance = |3-2| = 1, price = 52000.0
        # index 0 (48000): distance = |0-2| = 2, price = 48000.0
        # Sorted by (distance, price): (0,51000) < (1,49000) < (1,52000) < (2,48000)
        
        # Verify all orders are placed from center outward
        self.assertEqual(len(placed_prices), 4)
        self.assertEqual(placed_prices[0], 51000.0)  # At center (distance 0)
        self.assertEqual(placed_prices[1], 49000.0)  # Distance 1, lower price
        self.assertEqual(placed_prices[2], 52000.0)  # Distance 1, higher price
        self.assertEqual(placed_prices[3], 48000.0)  # Farthest (distance 2)

    def test_mixed_scenario(self):
        """Test a complex scenario with matching, mismatched, missing, and extra orders"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 48000.0},
            {'side': 'Buy', 'price': 49000.0},
            {'side': 'wait', 'price': 50000.0},
            {'side': 'Sell', 'price': 51000.0},
            {'side': 'Sell', 'price': 52000.0},
        ]
        
        limits = [
            {'price': '48000.0', 'side': 'Buy', 'orderId': 'order1'},      # Matching - keep
            {'price': '49000.0', 'side': 'Sell', 'orderId': 'order2'},     # Wrong side - cancel & replace
            # 51000.0 missing - should place
            {'price': '52000.0', 'side': 'Sell', 'orderId': 'order3'},     # Matching - keep
            {'price': '47000.0', 'side': 'Buy', 'orderId': 'order4'},      # Outside range - cancel
        ]
        
        self.strat.place_greed_orders(limits, 'long')
        
        # Should cancel: order2 (wrong side), order4 (outside range)
        self.assertEqual(self.strat.cancel_order.call_count, 2)
        self.strat.cancel_order.assert_any_call('order2')
        self.strat.cancel_order.assert_any_call('order4')
        
        # Should place: replacement for 49000.0, and new order for 51000.0
        self.assertEqual(self.strat.place_order.call_count, 2)

    def test_empty_greed_list(self):
        """Test behavior when greed list is empty"""
        self.strat.greed.greed = []
        
        limits = [
            {'price': '50000.0', 'side': 'Buy', 'orderId': 'order1'},
        ]
        
        # Should not crash, just cancel the limit (outside empty greed range)
        self.strat.place_greed_orders(limits, 'long')
        
        self.strat.cancel_order.assert_called_once_with('order1')

    def test_limits_sorting(self):
        """Test that limits are sorted by price before processing"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.0},
            {'side': 'wait', 'price': 50000.0},
        ]
        
        # Unsorted limits
        limits = [
            {'price': '51000.0', 'side': 'Sell', 'orderId': 'order2'},
            {'price': '49000.0', 'side': 'Buy', 'orderId': 'order1'},
        ]
        
        # Should handle without error
        self.strat.place_greed_orders(limits, 'long')
        
        # order2 should be cancelled (outside range)
        self.strat.cancel_order.assert_called_once_with('order2')

    def test_direction_parameter_passed_correctly(self):
        """Test that the direction parameter is passed to place_order"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.0},
            {'side': 'wait', 'price': 50000.0},
        ]
        
        self.strat.place_greed_orders([], 'short')
        
        # Verify direction is passed
        self.strat.place_order.assert_called_once()
        call_args = self.strat.place_order.call_args
        self.assertEqual(call_args[0][1], 'short')  # Second positional argument


class TestGetWaitIndices(unittest.TestCase):
    """Test suite for the _get_wait_indices helper method"""

    def setUp(self):
        """Set up test fixtures before each test"""
        self.mock_controller = Mock()
        self.strat = Strat50(
            controller=self.mock_controller,
            id=1,
            strat='Strat50',
            symbol='BTCUSDT',
            greed_step=0.5,
            greed_count=10,
            direction='long',
            exchange='bybit_usdt',
            max_margin=1000,
            min_liq_ratio=0.1,
            max_liq_ratio=0.9,
            min_total_margin=100,
            long_koef=1.0,
            increase_same_position_on_low_margin=False
        )
        self.strat.greed = Mock()
        self.strat.greed.WAIT = 'wait'

    def test_single_wait_index(self):
        """Test with a single WAIT entry"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.0},
            {'side': 'wait', 'price': 50000.0},
            {'side': 'Sell', 'price': 51000.0},
        ]
        
        center = self.strat._get_wait_indices()
        self.assertEqual(center, 1)

    def test_multiple_wait_indices(self):
        """Test with multiple consecutive WAIT entries"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.0},
            {'side': 'wait', 'price': 49500.0},
            {'side': 'wait', 'price': 50000.0},
            {'side': 'wait', 'price': 50500.0},
            {'side': 'Sell', 'price': 51000.0},
        ]
        
        center = self.strat._get_wait_indices()
        # Middle of indices 1,2,3 = (1+3)//2 = 2
        self.assertEqual(center, 2)

    def test_no_wait_indices(self):
        """Test fallback when no WAIT entries exist"""
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 49000.0},
            {'side': 'Buy', 'price': 49500.0},
            {'side': 'Sell', 'price': 50500.0},
            {'side': 'Sell', 'price': 51000.0},
        ]
        
        center = self.strat._get_wait_indices()
        # Middle of list: 4//2 = 2
        self.assertEqual(center, 2)

    def test_empty_greed_list(self):
        """Test with empty greed list"""
        self.strat.greed.greed = []
        
        center = self.strat._get_wait_indices()
        self.assertEqual(center, 0)


if __name__ == '__main__':
    # Run tests with verbose output
    unittest.main(verbosity=2)

