"""
Test the distance-based sorting in place_greed_orders method.

This test file verifies that the greed orders are processed in the correct order:
1. Sorted by distance from center (WAIT region)
2. When distances are equal, sorted by price (ascending)
"""

from unittest.mock import Mock

import pytest

from src.enums import Direction
from src.strat import Strat50


class TestGreedSorting:
    """Test suite for greed sorting logic"""

    def setup_method(self):
        """Set up test fixtures"""
        self.controller = Mock()
        self.controller.get_same_orders_error.return_value = False
        self.controller.get_limit_orders.return_value = []
        self.controller.check_positions_ratio.return_value = None

        self.strat = Strat50.create_for_testing(controller=self.controller)
        self.strat.last_close = 50000.0

        # Mock the place_order method to track call order
        self.order_calls = []
        self.original_place_order = self.strat.place_order

        def track_place_order(greed, direction):
            self.order_calls.append({
                'price': greed['price'],
                'side': greed['side']
            })
            return None

        self.strat.place_order = Mock(side_effect=track_place_order)
        self.strat.cancel_order = Mock()

    def test_single_wait_center_sorting(self):
        """Test sorting with a single WAIT item in the center"""
        # Build greed grid with single WAIT in center
        # Structure: [BUY, BUY, WAIT, SELL, SELL]
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 48000.0},   # index 0, distance 2
            {'side': 'Buy', 'price': 49000.0},   # index 1, distance 1
            {'side': 'wait', 'price': 50000.0},  # index 2, CENTER
            {'side': 'Sell', 'price': 51000.0},  # index 3, distance 1
            {'side': 'Sell', 'price': 52000.0},  # index 4, distance 2
        ]

        self.strat.place_greed_orders([], Direction.LONG)

        # Expected order: distance 1 items first (49000, 51000), then distance 2 (48000, 52000)
        # When distance is equal, sort by price (lower first)
        expected_prices = [49000.0, 51000.0, 48000.0, 52000.0]
        actual_prices = [call['price'] for call in self.order_calls]

        assert actual_prices == expected_prices, (
            f"Expected order {expected_prices}, got {actual_prices}"
        )

    def test_multiple_wait_items_sorting(self):
        """Test sorting with multiple consecutive WAIT items"""
        # Structure: [BUY, BUY, WAIT, WAIT, WAIT, SELL, SELL]
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 47000.0},   # index 0, distance 3
            {'side': 'Buy', 'price': 48000.0},   # index 1, distance 2
            {'side': 'wait', 'price': 49000.0},  # index 2, WAIT region start
            {'side': 'wait', 'price': 50000.0},  # index 3, CENTER (middle of WAIT)
            {'side': 'wait', 'price': 51000.0},  # index 4, WAIT region end
            {'side': 'Sell', 'price': 52000.0},  # index 5, distance 2
            {'side': 'Sell', 'price': 53000.0},  # index 6, distance 3
        ]

        self.strat.place_greed_orders([], Direction.LONG)

        # Center index = (2 + 4) // 2 = 3
        # Distances: idx0=3, idx1=2, idx5=2, idx6=3
        # Expected: idx1 and idx5 first (distance 2, sorted by price), then idx0 and idx6 (distance 3)
        expected_prices = [48000.0, 52000.0, 47000.0, 53000.0]
        actual_prices = [call['price'] for call in self.order_calls]

        assert actual_prices == expected_prices, (
            f"Expected order {expected_prices}, got {actual_prices}"
        )

    def test_no_wait_items_sorting(self):
        """Test sorting when there are no WAIT items (fallback to list middle)"""
        # Structure: [BUY, BUY, BUY, SELL, SELL, SELL]
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 47000.0},   # index 0
            {'side': 'Buy', 'price': 48000.0},   # index 1
            {'side': 'Buy', 'price': 49000.0},   # index 2
            {'side': 'Sell', 'price': 51000.0},  # index 3
            {'side': 'Sell', 'price': 52000.0},  # index 4
            {'side': 'Sell', 'price': 53000.0},  # index 5
        ]

        self.strat.place_greed_orders([], Direction.LONG)

        # Center index = 6 // 2 = 3 (fallback to middle)
        # Distances: idx0=3, idx1=2, idx2=1, idx3=0, idx4=1, idx5=2
        # Expected order by distance: 51000(0), 49000/52000(1), 48000/53000(2), 47000(3)
        # When equal distance, sort by price
        expected_prices = [51000.0, 49000.0, 52000.0, 48000.0, 53000.0, 47000.0]
        actual_prices = [call['price'] for call in self.order_calls]

        assert actual_prices == expected_prices, (
            f"Expected order {expected_prices}, got {actual_prices}"
        )

    def test_all_wait_items(self):
        """Test edge case where all items are WAIT"""
        self.strat.greed.greed = [
            {'side': 'wait', 'price': 48000.0},
            {'side': 'wait', 'price': 49000.0},
            {'side': 'wait', 'price': 50000.0},
            {'side': 'wait', 'price': 51000.0},
        ]

        self.strat.place_greed_orders([], Direction.LONG)

        # No orders should be placed since all are WAIT
        assert len(self.order_calls) == 0, "No orders should be placed when all items are WAIT"

    def test_empty_greed_list(self):
        """Test edge case with empty greed list"""
        self.strat.greed.greed = []

        self.strat.place_greed_orders([], Direction.LONG)

        # No orders should be placed
        assert len(self.order_calls) == 0, "No orders should be placed with empty greed list"

    def test_asymmetric_greed_distribution(self):
        """Test sorting with asymmetric distribution around center"""
        # Structure: [BUY, WAIT, SELL, SELL, SELL, SELL, SELL]
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 48000.0},   # index 0, distance 1
            {'side': 'wait', 'price': 49500.0},  # index 1, CENTER
            {'side': 'Sell', 'price': 50000.0},  # index 2, distance 1
            {'side': 'Sell', 'price': 51000.0},  # index 3, distance 2
            {'side': 'Sell', 'price': 52000.0},  # index 4, distance 3
            {'side': 'Sell', 'price': 53000.0},  # index 5, distance 4
            {'side': 'Sell', 'price': 54000.0},  # index 6, distance 5
        ]

        self.strat.place_greed_orders([], Direction.LONG)

        # Center at index 1
        # Expected: distance 1 (48000, 50000), distance 2 (51000), distance 3 (52000), etc.
        expected_prices = [48000.0, 50000.0, 51000.0, 52000.0, 53000.0, 54000.0]
        actual_prices = [call['price'] for call in self.order_calls]

        assert actual_prices == expected_prices, (
            f"Expected order {expected_prices}, got {actual_prices}"
        )

    def test_equal_distance_price_tiebreaker(self):
        """Test that when distances are equal, lower price comes first"""
        # Structure designed to have multiple items at same distance
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 45000.0},   # index 0, distance 3
            {'side': 'Buy', 'price': 46000.0},   # index 1, distance 2
            {'side': 'Buy', 'price': 48000.0},   # index 2, distance 1
            {'side': 'wait', 'price': 50000.0},  # index 3, CENTER
            {'side': 'Sell', 'price': 52000.0},  # index 4, distance 1
            {'side': 'Sell', 'price': 54000.0},  # index 5, distance 2
            {'side': 'Sell', 'price': 55000.0},  # index 6, distance 3
        ]

        self.strat.place_greed_orders([], Direction.LONG)

        # Distance 1: 48000 < 52000
        # Distance 2: 46000 < 54000
        # Distance 3: 45000 < 55000
        expected_prices = [48000.0, 52000.0, 46000.0, 54000.0, 45000.0, 55000.0]
        actual_prices = [call['price'] for call in self.order_calls]

        assert actual_prices == expected_prices, (
            f"Expected order {expected_prices}, got {actual_prices}"
        )

    def test_realistic_grid_scenario(self):
        """Test with a realistic grid scenario (50 greeds)"""
        # Build a realistic grid with greed_count=50, greed_step=0.2%
        self.strat.greed.greed_count = 50
        self.strat.greed.greed_step = 0.2
        self.strat.greed.build_greed(50000.0)

        # Verify the grid was built
        assert len(self.strat.greed.greed) == 51, "Should have 51 greeds (25 BUY + 1 WAIT + 25 SELL)"

        # Find the WAIT item
        wait_index = None
        for i, greed in enumerate(self.strat.greed.greed):
            if greed['side'] == 'wait':
                wait_index = i
                break

        assert wait_index is not None, "Should have a WAIT item"
        assert wait_index == 25, "WAIT should be at index 25 (middle of 51 items)"

        self.strat.place_greed_orders([], Direction.LONG)

        # Verify we placed 50 orders (excluding the WAIT)
        assert len(self.order_calls) == 50, f"Should place 50 orders, got {len(self.order_calls)}"

        # Verify the first orders are closest to center
        # Distance 1 should be at indices 24 and 26
        first_two_prices = [self.order_calls[0]['price'], self.order_calls[1]['price']]
        expected_first_two = [
            self.strat.greed.greed[24]['price'],
            self.strat.greed.greed[26]['price']
        ]

        assert sorted(first_two_prices) == sorted(expected_first_two), (
            f"First two orders should be from indices 24 and 26, got {first_two_prices}"
        )

    def test_integration_with_limits(self):
        """Test that sorting works correctly when limits are present"""
        from src.limit_order import LimitOrder

        # Set up a simple greed grid
        self.strat.greed.greed = [
            {'side': 'Buy', 'price': 48000.0},   # index 0, distance 2
            {'side': 'Buy', 'price': 49000.0},   # index 1, distance 1
            {'side': 'wait', 'price': 50000.0},  # index 2, CENTER
            {'side': 'Sell', 'price': 51000.0},  # index 3, distance 1
            {'side': 'Sell', 'price': 52000.0},  # index 4, distance 2
        ]

        # Create a mock limit that matches one of the greeds
        existing_limit = Mock(spec=LimitOrder)
        existing_limit.limit_price = 49000.0
        existing_limit.side = Mock()
        existing_limit.side.value = 'Buy'
        existing_limit.order_id = 'limit_123'

        limits = [existing_limit]

        self.strat.place_greed_orders(limits, Direction.LONG)

        # The greed at 49000 should not result in a new order since it matches the limit
        placed_prices = [call['price'] for call in self.order_calls]

        # Should have orders for all except 49000 (which matches existing limit)
        assert 49000.0 not in placed_prices, "Should not place order at 49000 since limit exists"
        assert len(placed_prices) == 3, f"Should place 3 orders (excluding matched limit), got {len(placed_prices)}"

        # Verify the orders are still in distance-sorted order
        expected_prices = [51000.0, 48000.0, 52000.0]  # distance 1, distance 2, distance 2
        assert placed_prices == expected_prices, (
            f"Expected order {expected_prices}, got {placed_prices}"
        )


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
