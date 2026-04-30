"""
Unit tests for Grid module.

Tests grid level calculations to ensure identical behavior to original greed.py.
"""

import pytest
from decimal import Decimal
from gridcore.grid import Grid, GridSideType


class TestGridBasic:
    """Basic grid functionality tests."""

    def test_build_grid_basic(self):
        """Build grid with 50 levels, 0.2% step."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Should have 51 items (25 buy + 1 wait + 25 sell)
        assert len(grid.grid) == 51

        # Count sides
        buy_count = sum(1 for g in grid.grid if g['side'] == GridSideType.BUY)
        sell_count = sum(1 for g in grid.grid if g['side'] == GridSideType.SELL)
        wait_count = sum(1 for g in grid.grid if g['side'] == GridSideType.WAIT)

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

    def test_build_grid_no_duplicate_prices(self):
        """Grid build validates no duplicate prices (critical for order identity)."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Extract all prices
        prices = [g['price'] for g in grid.grid]

        # All prices must be unique
        assert len(prices) == len(set(prices)), "Grid contains duplicate prices"

        # Also verify prices are sorted
        assert prices == sorted(prices), "Grid prices are not in ascending order"

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
            buy_item = next((g for g in grid.grid if g['side'] == GridSideType.BUY), None)
            sell_item = next((g for g in grid.grid if g['side'] == GridSideType.SELL), None)
            
            if buy_item and sell_item and buy_item['price'] < sell_item['price']:
                # Swap sides but keep prices sorted
                buy_item['side'] = GridSideType.SELL
                sell_item['side'] = GridSideType.BUY
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
            g['side'] = GridSideType.BUY

        # Should return False because sequence is invalid (no WAIT or SELL)
        assert grid.is_grid_correct() is False

    def test_single_side_type_all_sell(self):
        """Grid with only SELL side should return False (invalid sequence)."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Artificially set all sides to SELL
        for g in grid.grid:
            g['side'] = GridSideType.SELL

        # Should return False because sequence is invalid (no BUY or WAIT)
        assert grid.is_grid_correct() is False

    def test_single_side_type_all_wait(self):
        """Grid with only WAIT side should return False (invalid sequence)."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Artificially set all sides to WAIT
        for g in grid.grid:
            g['side'] = GridSideType.WAIT

        # Should return False because sequence is invalid (no BUY or SELL)
        assert grid.is_grid_correct() is False

    def test_sequence_without_wait(self):
        """Grid with BUY→SELL but no WAIT should return True (valid pattern)."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Remove WAIT items
        grid.grid = [g for g in grid.grid if g['side'] != GridSideType.WAIT]

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
        wait_index = next((i for i, g in enumerate(grid.grid) if g['side'] == GridSideType.WAIT), None)
        if wait_index is not None and 0 < wait_index < len(grid.grid) - 1:
            # Set item before WAIT to WAIT (if it's a BUY)
            if grid.grid[wait_index - 1]['side'] == GridSideType.BUY:
                grid.grid[wait_index - 1]['side'] = GridSideType.WAIT
            # Set item after WAIT to WAIT (if it's a SELL)
            if grid.grid[wait_index + 1]['side'] == GridSideType.SELL:
                grid.grid[wait_index + 1]['side'] = GridSideType.WAIT

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
        assert filled_level['side'] == GridSideType.WAIT

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
        middle_prices = [g['price'] for g in grid.grid if g['side'] == GridSideType.WAIT]
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
        buy_after = sum(1 for g in grid.grid if g['side'] == GridSideType.BUY)
        sell_after = sum(1 for g in grid.grid if g['side'] == GridSideType.SELL)
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
        buy_after = sum(1 for g in grid.grid if g['side'] == GridSideType.BUY)
        sell_after = sum(1 for g in grid.grid if g['side'] == GridSideType.SELL)
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
        wait_levels = [g for g in grid.grid if g['side'] == GridSideType.WAIT]
        assert abs(wait_levels[0]['price'] - 100000.0) < 1

        # Rebuild at new price
        grid.build_grid(120000.0)
        wait_levels = [g for g in grid.grid if g['side'] == GridSideType.WAIT]
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


class TestGridOnChangeCallback:
    """Tests for the on_change callback wiring."""

    def test_callback_fires_after_build_grid(self):
        calls = []
        grid = Grid(tick_size=Decimal('0.1'), grid_count=10, grid_step=0.2,
                    on_change=lambda g: calls.append(list(g)))
        grid.build_grid(100.0)
        assert len(calls) == 1
        assert len(calls[0]) == 11  # 5 buy + 1 wait + 5 sell

    def test_callback_fires_after_update_grid(self):
        calls = []
        grid = Grid(tick_size=Decimal('0.1'), grid_count=10, grid_step=0.2,
                    on_change=lambda g: calls.append(list(g)))
        grid.build_grid(100.0)
        calls.clear()
        grid.update_grid(last_filled_price=100.2, last_close=100.0)
        assert len(calls) == 1

    def test_callback_does_not_fire_for_restore_grid(self):
        """Restoration is not a mutation worth persisting (we just loaded
        what was already on disk)."""
        calls = []
        grid = Grid(tick_size=Decimal('0.1'), grid_count=10, grid_step=0.2,
                    on_change=lambda g: calls.append(list(g)))
        serialized = [
            {'side': 'Buy', 'price': 99.0},
            {'side': 'Buy', 'price': 99.5},
            {'side': 'Wait', 'price': 100.0},
            {'side': 'Sell', 'price': 100.5},
            {'side': 'Sell', 'price': 101.0},
        ]
        grid.restore_grid(serialized)
        assert calls == []

    def test_callback_errors_do_not_break_grid(self):
        """Callback failures are logged but never propagate — persistence
        failures must not crash strategy logic."""
        def raise_callback(g):
            raise RuntimeError("boom")

        grid = Grid(tick_size=Decimal('0.1'), grid_count=10, grid_step=0.2,
                    on_change=raise_callback)
        grid.build_grid(100.0)  # Should not raise.
        assert len(grid.grid) == 11


class TestGridRestoreGrid:
    """Tests for restore_grid()."""

    def test_restore_valid_grid(self):
        grid = Grid(tick_size=Decimal('0.1'), grid_count=10, grid_step=0.2)
        serialized = [
            {'side': 'Buy', 'price': 99.0},
            {'side': 'Buy', 'price': 99.5},
            {'side': 'Wait', 'price': 100.0},
            {'side': 'Sell', 'price': 100.5},
            {'side': 'Sell', 'price': 101.0},
        ]
        assert grid.restore_grid(serialized) is True
        assert len(grid.grid) == 5
        assert grid.grid[2]['side'] == GridSideType.WAIT

    def test_restore_invalid_pattern_returns_false(self):
        """Grid that violates BUY→WAIT→SELL pattern is rejected."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=10, grid_step=0.2)
        bad = [
            {'side': 'Sell', 'price': 99.0},  # SELL before BUY — invalid
            {'side': 'Buy', 'price': 99.5},
            {'side': 'Wait', 'price': 100.0},
        ]
        assert grid.restore_grid(bad) is False
        assert grid.grid == []

    def test_restore_unknown_side_returns_false(self):
        grid = Grid(tick_size=Decimal('0.1'), grid_count=10, grid_step=0.2)
        bad = [{'side': 'Garbage', 'price': 100.0}]
        assert grid.restore_grid(bad) is False
        assert grid.grid == []

    def test_restore_missing_keys_returns_false(self):
        grid = Grid(tick_size=Decimal('0.1'), grid_count=10, grid_step=0.2)
        bad = [{'side': 'Wait'}]  # missing 'price'
        assert grid.restore_grid(bad) is False
        assert grid.grid == []

    def test_restore_derives_anchor_from_wait(self):
        """anchor_price is derived from the WAIT center after restore."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=10, grid_step=0.2)
        serialized = [
            {'side': 'Buy', 'price': 99.0},
            {'side': 'Wait', 'price': 100.0},
            {'side': 'Sell', 'price': 101.0},
        ]
        grid.restore_grid(serialized)
        assert grid.anchor_price == 100.0

    def test_restore_derives_anchor_with_multi_wait(self):
        """When restored grid has multiple consecutive WAITs (allowed by
        is_grid_correct), anchor_price uses the middle WAIT."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=10, grid_step=0.2)
        serialized = [
            {'side': 'Buy', 'price': 99.0},
            {'side': 'Wait', 'price': 99.5},
            {'side': 'Wait', 'price': 100.0},
            {'side': 'Wait', 'price': 100.5},
            {'side': 'Sell', 'price': 101.0},
        ]
        grid.restore_grid(serialized)
        # WAIT indices are [1, 2, 3], middle = 2 → price 100.0
        assert grid.anchor_price == 100.0

    def test_round_trip_identity(self):
        """build_grid → serialize → restore_grid produces identical grid."""
        grid_a = Grid(tick_size=Decimal('0.1'), grid_count=20, grid_step=0.3)
        grid_a.build_grid(50.0)

        serialized = [{'side': str(g['side']), 'price': g['price']} for g in grid_a.grid]

        grid_b = Grid(tick_size=Decimal('0.1'), grid_count=20, grid_step=0.3)
        assert grid_b.restore_grid(serialized) is True
        assert grid_b.grid == grid_a.grid


class TestGridMinMaxAccessors:
    def test_min_max_grid_after_build(self):
        grid = Grid(tick_size=Decimal('0.1'), grid_count=10, grid_step=0.2)
        grid.build_grid(100.0)
        assert grid.min_grid == grid.grid[0]['price']
        assert grid.max_grid == grid.grid[-1]['price']

    def test_min_max_grid_raises_when_empty(self):
        grid = Grid(tick_size=Decimal('0.1'), grid_count=10, grid_step=0.2)
        with pytest.raises(ValueError):
            _ = grid.min_grid
        with pytest.raises(ValueError):
            _ = grid.max_grid


class TestWaitCenter:
    """Tests for Grid.wait_center()."""

    def test_single_wait_level_returns_its_price(self):
        grid = Grid(tick_size=Decimal('0.01'), grid_count=10, grid_step=1.0)
        grid.build_grid(100.0)
        # build_grid produces exactly one WAIT level
        assert grid.wait_center() == 100.0

    def test_multi_wait_band_returns_midpoint(self):
        grid = Grid(tick_size=Decimal('0.01'), grid_count=10, grid_step=1.0)
        grid.build_grid(100.0)
        # Manually expand the WAIT band: relabel a few levels around center as WAIT
        center_idx = next(i for i, g in enumerate(grid.grid) if g['side'] == GridSideType.WAIT)
        grid.grid[center_idx - 1]['side'] = GridSideType.WAIT
        grid.grid[center_idx + 1]['side'] = GridSideType.WAIT
        wait_prices = [g['price'] for g in grid.grid if g['side'] == GridSideType.WAIT]
        expected = (min(wait_prices) + max(wait_prices)) / 2
        assert grid.wait_center() == expected

    def test_zero_wait_even_length_uses_mean_of_two_middles(self):
        grid = Grid(tick_size=Decimal('0.01'), grid_count=10, grid_step=1.0)
        grid.build_grid(100.0)
        # Force even length
        grid.grid.pop()
        # Strip every WAIT mark
        for level in grid.grid:
            if level['side'] == GridSideType.WAIT:
                level['side'] = GridSideType.BUY
        n = len(grid.grid)
        assert n % 2 == 0
        expected = (grid.grid[n // 2 - 1]['price'] + grid.grid[n // 2]['price']) / 2
        assert grid.wait_center() == expected

    def test_zero_wait_odd_length_uses_single_middle(self):
        grid = Grid(tick_size=Decimal('0.01'), grid_count=10, grid_step=1.0)
        grid.build_grid(100.0)
        n = len(grid.grid)
        assert n % 2 == 1  # build_grid yields 11 levels for grid_count=10
        for level in grid.grid:
            if level['side'] == GridSideType.WAIT:
                level['side'] = GridSideType.BUY
        assert grid.wait_center() == grid.grid[n // 2]['price']

    def test_wait_center_raises_on_empty_grid(self):
        """Defensive contract: wait_center() must raise ValueError on empty grid
        rather than IndexError or returning a meaningless value."""
        grid = Grid(tick_size=Decimal('0.01'), grid_count=10, grid_step=1.0)
        assert grid.grid == []
        with pytest.raises(ValueError, match="empty grid"):
            grid.wait_center()


class TestZeroDivisionGuards:
    """Defensive guards against division-by-zero in degenerate states.
    The grid invariants (positive traded prices, GridConfig.grid_step > 0)
    make these unreachable in production, but the guards are exercised here
    to lock in the contract."""

    def test_recenter_no_op_when_wait_center_is_zero(self, caplog):
        """If the WAIT band is symmetric around zero (e.g. [-1, 1]), wait_center
        returns 0. recenter_if_drifted must early-return rather than divide."""
        grid = Grid(tick_size=Decimal('0.01'), grid_count=10, grid_step=1.0)
        # Bypass build_grid validation by setting grid directly.
        grid.grid = [
            {'side': GridSideType.WAIT, 'price': -1.0},
            {'side': GridSideType.WAIT, 'price': 1.0},
        ]
        assert grid.wait_center() == 0.0

        with caplog.at_level('WARNING'):
            result = grid.recenter_if_drifted(5.0)

        assert bool(result) is False
        assert result.n_steps == 0
        assert any('wait_center is zero' in r.message for r in caplog.records)

    def test_recenter_no_op_when_grid_step_is_zero(self, caplog):
        """grid_step == 0 would crash int(deviation_pct / 0). Guard returns
        a no-op RecenterResult."""
        grid = Grid(tick_size=Decimal('0.01'), grid_count=10, grid_step=1.0)
        grid.build_grid(100.0)
        grid.grid_step = 0  # break GridConfig invariant for the test

        with caplog.at_level('WARNING'):
            result = grid.recenter_if_drifted(105.0)

        assert bool(result) is False
        assert result.n_steps == 0
        assert any('grid_step is zero' in r.message for r in caplog.records)

    def test_is_too_close_returns_false_on_zero_price(self):
        """__is_too_close divides by price1 — guard short-circuits on zero."""
        grid = Grid(tick_size=Decimal('0.01'), grid_count=10, grid_step=1.0)
        # Name-mangled access for the private method
        assert grid._Grid__is_too_close(0.0, 1.0) is False

    def test_is_too_close_returns_false_on_zero_grid_step(self):
        """grid_step == 0 makes the threshold 0; guard returns False."""
        grid = Grid(tick_size=Decimal('0.01'), grid_count=10, grid_step=1.0)
        grid.grid_step = 0
        assert grid._Grid__is_too_close(100.0, 100.0) is False


class TestRecenterIfDrifted:
    """Tests for Grid.recenter_if_drifted (feature 0022)."""

    def _make_grid(self, anchor=100.0, grid_count=20, grid_step=1.0):
        grid = Grid(tick_size=Decimal('0.01'), grid_count=grid_count, grid_step=grid_step)
        grid.build_grid(anchor)
        return grid

    def test_no_action_when_grid_empty(self):
        grid = Grid(tick_size=Decimal('0.01'), grid_count=10, grid_step=1.0)
        walked, dev, n = grid.recenter_if_drifted(100.0)
        assert walked is False
        assert n == 0

    def test_no_action_when_deviation_below_grid_step(self):
        grid = self._make_grid(anchor=100.0, grid_step=1.0)
        # 0.5% deviation < 1.0% grid_step
        walked, dev, n = grid.recenter_if_drifted(100.5)
        assert walked is False
        assert n == 0
        assert dev < 1.0

    def test_no_action_at_exact_grid_step(self):
        grid = self._make_grid(anchor=100.0, grid_step=1.0)
        # exactly at threshold; trigger uses '>', not '>='
        walked, dev, n = grid.recenter_if_drifted(101.0)
        assert walked is False
        assert n == 0

    def test_walks_n_steps_up_when_price_above_band(self):
        grid = self._make_grid(anchor=100.0, grid_count=20, grid_step=1.0)
        original_min = grid.grid[0]['price']
        original_max = grid.grid[-1]['price']
        original_len = len(grid.grid)
        # 3.5% deviation → n_steps = 3
        walked, dev, n = grid.recenter_if_drifted(103.5)
        assert walked is True
        assert n == 3
        assert len(grid.grid) == original_len  # length preserved
        assert grid.grid[0]['price'] > original_min  # bottom raised
        assert grid.grid[-1]['price'] > original_max  # top raised

    def test_walks_n_steps_down_when_price_below_band(self):
        grid = self._make_grid(anchor=100.0, grid_count=20, grid_step=1.0)
        original_min = grid.grid[0]['price']
        original_max = grid.grid[-1]['price']
        original_len = len(grid.grid)
        # -2.5% deviation → n_steps = 2 (deviation_pct ≈ 2.439, floor = 2)
        walked, dev, n = grid.recenter_if_drifted(97.5)
        assert walked is True
        assert n == 2
        assert len(grid.grid) == original_len
        assert grid.grid[0]['price'] < original_min
        assert grid.grid[-1]['price'] < original_max

    def test_round_trip_outside_walked_region(self):
        """Walk-up by N: levels at indices [0..len-N-1] post-walk == levels at [N..len-1] pre-walk."""
        grid = self._make_grid(anchor=100.0, grid_count=20, grid_step=1.0)
        pre_prices = [g['price'] for g in grid.grid]
        walked, _, n = grid.recenter_if_drifted(103.5)
        assert walked is True
        post_prices = [g['price'] for g in grid.grid]
        assert post_prices[: len(pre_prices) - n] == pre_prices[n:]

    def test_full_rebuild_fallback_when_n_steps_exceeds_half(self):
        grid = self._make_grid(anchor=100.0, grid_count=20, grid_step=1.0)
        original_prices = {g['price'] for g in grid.grid}
        # 15% deviation → n_steps = 15 > grid_count // 2 = 10 → fallback
        walked, dev, n = grid.recenter_if_drifted(115.0)
        assert walked is True
        assert n >= grid.grid_count // 2
        # Grid is rebuilt around 115.0; very few (if any) shared prices with original
        new_prices = {g['price'] for g in grid.grid}
        # Rebuilt grid is centered far from original — bulk of prices differ.
        overlap = original_prices & new_prices
        assert len(overlap) <= 1

    def test_anchor_updated_to_wait_center_after_walk(self):
        grid = self._make_grid(anchor=100.0, grid_count=20, grid_step=1.0)
        walked, _, _ = grid.recenter_if_drifted(103.5)
        assert walked is True
        # _original_anchor_price should now equal new wait_center (close to 103.5)
        assert grid._original_anchor_price == grid.wait_center()
        assert abs(grid._original_anchor_price - 103.5) < 1.0

    def test_assign_sides_post_walk(self):
        grid = self._make_grid(anchor=100.0, grid_count=20, grid_step=1.0)
        walked, _, _ = grid.recenter_if_drifted(103.5)
        assert walked is True
        last_close = 103.5
        for level in grid.grid:
            diff_pct = abs(level['price'] - last_close) / level['price'] * 100
            if diff_pct < grid.grid_step / 4:
                assert level['side'] == GridSideType.WAIT
            elif level['price'] > last_close:
                assert level['side'] == GridSideType.SELL
            elif level['price'] < last_close:
                assert level['side'] == GridSideType.BUY

    def test_notify_change_called_on_walk(self):
        calls = []
        grid = Grid(
            tick_size=Decimal('0.01'),
            grid_count=20,
            grid_step=1.0,
            on_change=lambda g: calls.append(len(g)),
        )
        grid.build_grid(100.0)
        calls_after_build = len(calls)
        walked, _, _ = grid.recenter_if_drifted(103.5)
        assert walked is True
        assert len(calls) == calls_after_build + 1

    def test_notify_change_not_called_when_no_action(self):
        calls = []
        grid = Grid(
            tick_size=Decimal('0.01'),
            grid_count=20,
            grid_step=1.0,
            on_change=lambda g: calls.append(len(g)),
        )
        grid.build_grid(100.0)
        calls_after_build = len(calls)
        walked, _, _ = grid.recenter_if_drifted(100.2)
        assert walked is False
        assert len(calls) == calls_after_build  # no extra notify

    def test_assign_sides_with_fill_price_preserves_legacy(self):
        """_assign_sides(last_close, fill_price=fp) preserves update_grid semantics:
        WAIT marking is driven by proximity to fp, not last_close."""
        grid = self._make_grid(anchor=100.0, grid_count=20, grid_step=1.0)
        target_idx = 5
        fp = grid.grid[target_idx]['price']
        last_close = grid.grid[12]['price']  # well separated from fp (different level)
        grid._assign_sides(last_close, fill_price=fp)
        # Level at fp must be WAIT (driven by fill_price proximity)
        assert grid.grid[target_idx]['side'] == GridSideType.WAIT
        # The level at last_close must NOT be WAIT, because the WAIT rule uses
        # fill_price as reference, not last_close. (If it were last_close, this
        # level would also be WAIT.)
        assert grid.grid[12]['side'] != GridSideType.WAIT
