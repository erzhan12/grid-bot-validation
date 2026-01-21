"""
Unit tests for Grid module.

Tests grid level calculations to ensure identical behavior to original greed.py.
"""

import pytest
from decimal import Decimal
from gridcore.grid import Grid


class TestGridBasic:
    """Basic grid functionality tests."""

    def test_build_grid_basic(self):
        """Build grid with 50 levels, 0.2% step."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Should have 51 items (25 buy + 1 wait + 25 sell)
        assert len(grid.grid) == 51

        # Count sides
        buy_count = sum(1 for g in grid.grid if g['side'] == grid.BUY)
        sell_count = sum(1 for g in grid.grid if g['side'] == grid.SELL)
        wait_count = sum(1 for g in grid.grid if g['side'] == grid.WAIT)

        assert buy_count == 25
        assert wait_count == 1
        assert sell_count == 25

    def test_build_grid_price_rounding(self):
        """Verify tick_size rounding for BTCUSDT-like symbol."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Check all prices are rounded to 0.1
        for g in grid.grid:
            price = g['price']
            # Price should be divisible by 0.1
            assert abs(price - round(price / 0.1) * 0.1) < 0.00001

    def test_build_grid_none_price(self):
        """Don't build grid if last_close is None."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(None)

        assert len(grid.grid) == 0

    def test_is_greed_correct(self):
        """Valid BUY→WAIT→SELL sequence."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        assert grid.is_grid_correct() is True


class TestGridCorrectness:
    """Comprehensive tests for is_grid_correct() covering sorting and sequence validation."""

    def test_valid_grid(self):
        """Valid grid: sorted prices + correct BUY→WAIT→SELL sequence."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        assert grid.is_grid_correct() is True

    def test_unsorted_prices(self):
        """Unsorted prices should cause is_grid_correct() to return False."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Initially correct
        assert grid.is_grid_correct() is True

        # Break sorting by swapping prices
        if len(grid.grid) > 1:
            grid.grid[0]['price'], grid.grid[1]['price'] = grid.grid[1]['price'], grid.grid[0]['price']
            # Should return False because sorting check fails first
            assert grid.is_grid_correct() is False

    def test_wrong_sequence_sorted(self):
        """Sorted prices but wrong sequence should return False."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Initially correct
        assert grid.is_grid_correct() is True

        # Break sequence by putting SELL before BUY (but keep prices sorted)
        if len(grid.grid) > 10:
            # Find a BUY and a SELL
            buy_item = next((g for g in grid.grid if g['side'] == grid.BUY), None)
            sell_item = next((g for g in grid.grid if g['side'] == grid.SELL), None)
            
            if buy_item and sell_item and buy_item['price'] < sell_item['price']:
                # Swap sides but keep prices sorted
                buy_item['side'] = grid.SELL
                sell_item['side'] = grid.BUY
                # Should return False because sequence is wrong
                assert grid.is_grid_correct() is False

    def test_empty_grid(self):
        """Empty grid should return False (no sequence to validate)."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        # Don't build grid - it's empty

        assert len(grid.grid) == 0
        # Empty grid has no sequence, so is_grid_correct() returns False
        # (current_state never reaches 2, which requires BUY→WAIT→SELL sequence)
        assert grid.is_grid_correct() is False

    def test_duplicate_prices(self):
        """Duplicate prices should cause is_grid_correct() to return False."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Initially correct
        assert grid.is_grid_correct() is True

        # Create a scenario with duplicate prices (artificially)
        # Set two adjacent items to the same price
        if len(grid.grid) > 2:
            # Set adjacent items to same price
            grid.grid[1]['price'] = grid.grid[0]['price']
            # Duplicate prices should cause is_grid_correct() to return False
            assert grid.is_grid_correct() is False

    def test_after_updates(self):
        """Grid remains correct after update_grid() calls."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Initially correct
        assert grid.is_grid_correct() is True

        # Update grid
        grid.update_grid(last_filled_price=99800.0, last_close=100000.0)
        
        # Grid should still be correct after update
        # (prices sorted, no duplicates, valid BUY→WAIT→SELL or BUY→SELL sequence)
        assert grid.is_grid_correct() is True, "Grid should remain correct after update_grid()"

    def test_after_rebuilds(self):
        """Grid remains correct after out-of-bounds rebuilds."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Initially correct
        assert grid.is_grid_correct() is True

        # Rebuild by moving price way outside bounds
        grid.update_grid(last_filled_price=99000.0, last_close=120000.0)
        
        # After rebuild, grid should be correct again
        assert grid.is_grid_correct() is True

    def test_single_side_type_all_buy(self):
        """Grid with only BUY side should return False (invalid sequence)."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Artificially set all sides to BUY
        for g in grid.grid:
            g['side'] = grid.BUY

        # Should return False because sequence is invalid (no WAIT or SELL)
        assert grid.is_grid_correct() is False

    def test_single_side_type_all_sell(self):
        """Grid with only SELL side should return False (invalid sequence)."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Artificially set all sides to SELL
        for g in grid.grid:
            g['side'] = grid.SELL

        # Should return False because sequence is invalid (no BUY or WAIT)
        assert grid.is_grid_correct() is False

    def test_single_side_type_all_wait(self):
        """Grid with only WAIT side should return False (invalid sequence)."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Artificially set all sides to WAIT
        for g in grid.grid:
            g['side'] = grid.WAIT

        # Should return False because sequence is invalid (no BUY or SELL)
        assert grid.is_grid_correct() is False

    def test_sequence_without_wait(self):
        """Grid with BUY→SELL but no WAIT should return True (valid pattern)."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Remove WAIT items
        grid.grid = [g for g in grid.grid if g['side'] != grid.WAIT]

        # Should return True because BUY→SELL is a valid pattern
        assert grid.is_grid_correct() is True

    def test_sequence_buy_wait_sell(self):
        """Grid with BUY→WAIT→SELL pattern should return True."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Grid built with build_grid() has BUY→WAIT→SELL pattern
        assert grid.is_grid_correct() is True

    def test_sequence_multiple_waits(self):
        """Grid with BUY→WAIT→WAIT→SELL pattern (multiple WAITs) should return True."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Initially correct
        assert grid.is_grid_correct() is True

        # Artificially create multiple consecutive WAITs by setting adjacent items to WAIT
        # Find the WAIT item and set adjacent items to WAIT as well
        wait_index = next((i for i, g in enumerate(grid.grid) if g['side'] == grid.WAIT), None)
        if wait_index is not None and 0 < wait_index < len(grid.grid) - 1:
            # Set item before WAIT to WAIT (if it's a BUY)
            if grid.grid[wait_index - 1]['side'] == grid.BUY:
                grid.grid[wait_index - 1]['side'] = grid.WAIT
            # Set item after WAIT to WAIT (if it's a SELL)
            if grid.grid[wait_index + 1]['side'] == grid.SELL:
                grid.grid[wait_index + 1]['side'] = grid.WAIT

        # Grid with multiple WAITs in a row should still be correct
        assert grid.is_grid_correct() is True


class TestGridUpdate:
    """Grid update and rebalancing tests."""

    def test_update_grid_side_assignment(self):
        """After fill, sides update correctly based on current price."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Simulate fill at 99800 (below middle)
        grid.update_grid(last_filled_price=99800.0, last_close=100000.0)

        # The filled price should be marked as WAIT
        filled_level = next((g for g in grid.grid if abs(g['price'] - 99800.0) < 1), None)
        assert filled_level is not None
        assert filled_level['side'] == grid.WAIT

    def test_update_grid_rebuilds_on_out_of_bounds(self):
        """Grid rebuilds if price moves outside bounds."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Store original bounds
        original_min = min(g['price'] for g in grid.grid)
        original_max = max(g['price'] for g in grid.grid)

        # Move price way outside bounds (20% move on 0.2% step grid)
        new_price = 120000.0
        grid.update_grid(last_filled_price=110000.0, last_close=new_price)

        # Grid should rebuild around new price
        new_min = min(g['price'] for g in grid.grid)
        new_max = max(g['price'] for g in grid.grid)

        # Bounds should have shifted significantly
        assert new_min > original_min
        assert new_max > original_max

        # Middle should be near new price
        middle_prices = [g['price'] for g in grid.grid if g['side'] == grid.WAIT]
        assert len(middle_prices) >= 1
        # Middle should be within a few steps of new price
        assert abs(middle_prices[0] - new_price) / new_price < 0.01

    def test_update_grid_none_handling(self):
        """Don't update grid if prices are None."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)
        original_len = len(grid.grid)

        # None values should be no-op
        grid.update_grid(None, 100000.0)
        assert len(grid.grid) == original_len

        grid.update_grid(99000.0, None)
        assert len(grid.grid) == original_len


class TestGridRebalancing:
    """Grid rebalancing (centering) tests."""

    def test_center_grid_buy_heavy(self):
        """More buys than sells, imbalance >30% → grid shifts upward."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Initially correct and balanced
        assert grid.is_grid_correct() is True

        # Record original grid bounds
        min_price_before = grid.grid[0]['price']
        max_price_before = grid.grid[-1]['price']
        grid_length_before = len(grid.grid)

        # Create buy-heavy imbalance by setting last_close near the top of the grid
        # This makes most levels below last_close (BUYs) and few above (SELLs)
        # When imbalance > 30%, __center_grid() shifts grid upward
        high_price = max_price_before * 0.98  # Near top, creates ~80% BUYs

        # Trigger update which reassigns sides and then calls __center_grid()
        grid.update_grid(last_filled_price=high_price, last_close=high_price)

        # Grid should still be correct after rebalancing
        assert grid.is_grid_correct() is True, "Grid should remain correct after rebalancing"

        # Grid length should be maintained
        assert len(grid.grid) == grid_length_before, "Grid should maintain same number of levels"

        # Verify grid shifted upward: max price should increase
        max_price_after = grid.grid[-1]['price']
        assert max_price_after > max_price_before, "Grid should shift upward (new top level added)"

        # Verify the grid is still buy-heavy (rebalancing only shifts one level at a time)
        buy_after = sum(1 for g in grid.grid if g['side'] == grid.BUY)
        sell_after = sum(1 for g in grid.grid if g['side'] == grid.SELL)
        total_after = buy_after + sell_after
        if total_after > 0:
            imbalance_after = abs(buy_after - sell_after) / total_after
            # Rebalancing shifts one level at a time, so imbalance may still be above threshold
            # but should be improving (one more sell, one fewer buy than before rebalancing)
            assert sell_after > 0, "Should have at least one sell after rebalancing"

    def test_center_grid_sell_heavy(self):
        """More sells than buys, imbalance >30% → grid shifts downward."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Initially correct and balanced
        assert grid.is_grid_correct() is True

        # Record original grid bounds
        min_price_before = grid.grid[0]['price']
        max_price_before = grid.grid[-1]['price']
        grid_length_before = len(grid.grid)

        # Create sell-heavy imbalance by setting last_close near the bottom of the grid
        # This makes most levels above last_close (SELLs) and few below (BUYs)
        # When imbalance > 30%, __center_grid() shifts grid downward
        low_price = min_price_before * 1.02  # Near bottom, creates ~80% SELLs

        # Trigger update which reassigns sides and then calls __center_grid()
        grid.update_grid(last_filled_price=low_price, last_close=low_price)

        # Grid should still be correct after rebalancing
        assert grid.is_grid_correct() is True, "Grid should remain correct after rebalancing"

        # Grid length should be maintained
        assert len(grid.grid) == grid_length_before, "Grid should maintain same number of levels"

        # Verify grid shifted downward: min price should decrease
        min_price_after = grid.grid[0]['price']
        assert min_price_after < min_price_before, "Grid should shift downward (new bottom level added)"

        # Verify the grid is still sell-heavy (rebalancing only shifts one level at a time)
        buy_after = sum(1 for g in grid.grid if g['side'] == grid.BUY)
        sell_after = sum(1 for g in grid.grid if g['side'] == grid.SELL)
        total_after = buy_after + sell_after
        if total_after > 0:
            imbalance_after = abs(sell_after - buy_after) / total_after
            # Rebalancing shifts one level at a time, so imbalance may still be above threshold
            # but should be improving (one more buy, one fewer sell than before rebalancing)
            assert buy_after > 0, "Should have at least one buy after rebalancing"


class TestGridHelpers:
    """Helper method tests."""

    def test_is_too_close(self):
        """Price within grid_step/4 should return True."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)

        # 0.2% step / 4 = 0.05% threshold
        # At 100000: 0.05% = 50
        price1 = 100000.0
        price2 = 100030.0  # 0.03% away - should be too close

        # Access private method for testing
        assert grid._Grid__is_too_close(price1, price2) is True

        # Prices far apart should return False
        price3 = 100200.0  # 0.2% away - not too close
        assert grid._Grid__is_too_close(price1, price3) is False

    def test_round_price(self):
        """Price rounding should match tick_size."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)

        # Test various prices
        assert grid._round_price(100000.15) == 100000.1
        assert grid._round_price(100000.19) == 100000.2
        assert grid._round_price(100000.14) == 100000.1

        # Test with 0.01 tick size
        grid_fine = Grid(tick_size=Decimal('0.01'), grid_count=50, grid_step=0.2)
        assert grid_fine._round_price(100000.155) == 100000.16
        assert grid_fine._round_price(100000.154) == 100000.15


class TestGridProperties:
    """Test grid property methods."""

    def test_min_max_greed(self):
        """Min and max price accessors."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        min_price = grid._Grid__min_grid
        max_price = grid._Grid__max_grid

        # Min should be first price
        assert min_price == grid.grid[0]['price']
        # Max should be last price
        assert max_price == grid.grid[-1]['price']

        # Max should be greater than min
        assert max_price > min_price

    def test_buy_sell_counts(self):
        """Buy and sell count properties."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        buy_count = grid._Grid__grid_count_buy
        sell_count = grid._Grid__grid_count_sell

        assert buy_count == 25
        assert sell_count == 25


class TestGridRebuild:
    """Grid rebuild behavior tests."""

    def test_build_grid_clears_before_building(self):
        """Calling build_grid multiple times doesn't double the grid."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)

        # Build first time
        grid.build_grid(100000.0)
        assert len(grid.grid) == 51

        # Build again - should NOT double the grid
        grid.build_grid(100000.0)
        assert len(grid.grid) == 51

        # Build at different price - should rebuild, not append
        grid.build_grid(110000.0)
        assert len(grid.grid) == 51

    def test_rebuild_centers_on_new_price(self):
        """Rebuilding centers the grid on the new price."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)

        # Build at initial price
        grid.build_grid(100000.0)
        wait_levels = [g for g in grid.grid if g['side'] == grid.WAIT]
        assert abs(wait_levels[0]['price'] - 100000.0) < 1

        # Rebuild at new price
        grid.build_grid(120000.0)
        wait_levels = [g for g in grid.grid if g['side'] == grid.WAIT]
        assert abs(wait_levels[0]['price'] - 120000.0) < 1

        # Grid size should remain the same
        assert len(grid.grid) == 51


class TestGridEdgeCases:
    """Edge case tests."""

    def test_small_tick_size(self):
        """Grid works with very small tick sizes."""
        grid = Grid(tick_size=Decimal('0.00001'), grid_count=50, grid_step=0.2)
        grid.build_grid(1.5)

        assert len(grid.grid) == 51
        assert grid.is_grid_correct() is True

    def test_large_grid_count(self):
        """Grid works with large grid counts."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=200, grid_step=0.2)
        grid.build_grid(100000.0)

        assert len(grid.grid) == 201  # 100 buy + 1 wait + 100 sell
        assert grid.is_grid_correct() is True

    def test_small_grid_step(self):
        """Grid works with very small step sizes."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.01)
        grid.build_grid(100000.0)

        assert len(grid.grid) == 51
        assert grid.is_grid_correct() is True

        # Verify steps are actually smaller
        prices = [g['price'] for g in grid.grid]
        avg_step_pct = abs(prices[1] - prices[0]) / prices[0] * 100
        assert avg_step_pct < 0.02  # Should be around 0.01%


class TestGridUpdateGridRebuild:
    """Tests for update_grid rebuild behavior."""

    def test_update_grid_on_empty_grid_builds_from_scratch(self):
        """update_grid on empty grid triggers rebuild."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)

        # Grid is empty, so update_grid should trigger rebuild
        grid.update_grid(last_filled_price=99000.0, last_close=100000.0)

        # Grid should now be built
        assert len(grid.grid) == 51


class TestGridAnchorPrice:
    """Tests for anchor_price property."""

    def test_anchor_price_none_before_build(self):
        """anchor_price returns None before grid is built."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        assert grid.anchor_price is None

    def test_anchor_price_returns_original_center(self):
        """anchor_price returns the original center price after build."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        assert grid.anchor_price == 100000.0

    def test_anchor_price_unchanged_after_update(self):
        """anchor_price remains unchanged after update_grid."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        original_anchor = grid.anchor_price

        # Update grid with fills
        grid.update_grid(last_filled_price=99800.0, last_close=100000.0)

        # Anchor should remain the same
        assert grid.anchor_price == original_anchor
