"""
Comparison tests to verify gridcore produces identical results to original bbu2-master code.

These tests are CRITICAL - they validate that the extraction maintains exact behavior.
"""

import pytest
import sys
from pathlib import Path
from decimal import Decimal

# Add bbu2-master reference to path for comparison
bbu_reference_path = Path(__file__).parent.parent.parent.parent / 'bbu_reference' / 'bbu2-master'
sys.path.insert(0, str(bbu_reference_path))

from gridcore.grid import Grid


class MockStrat:
    """Mock strat object for original Greed class."""
    def __init__(self):
        self.id = 'test_strat_1'


class MockBybitApiUsdt:
    """Mock BybitApiUsdt for original Greed class."""
    ticksizes = {}

    @classmethod
    def round_price(cls, symbol, price):
        """Mock round_price matching original behavior."""
        tick_size = cls.ticksizes.get(symbol, 0.1)
        rounded = round(price / tick_size) * tick_size
        return float(f'{rounded:.10f}')


class MockDbFiles:
    """Mock DbFiles to avoid database dependencies."""
    _greed_storage = {}

    @classmethod
    def read_greed(cls, strat_id):
        return cls._greed_storage.get(strat_id, [])

    @classmethod
    def write_greed(cls, greed, strat_id):
        cls._greed_storage[strat_id] = greed


@pytest.fixture(autouse=True)
def setup_mocks(monkeypatch):
    """Set up mocks for original code."""
    # Mock BybitApiUsdt in the original greed module
    try:
        import greed as original_greed
        monkeypatch.setattr(original_greed, 'BybitApiUsdt', MockBybitApiUsdt)
        monkeypatch.setattr(original_greed, 'DbFiles', MockDbFiles)
    except ImportError:
        pytest.skip("Original bbu2-master code not available")


class TestGridCalculationsMatchOriginal:
    """Verify gridcore Grid produces identical results to original Greed."""

    def test_build_greed_produces_identical_prices(self):
        """Grid.build_greed() produces identical price list as original Greed.build_greed()."""
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        # Setup
        symbol = 'BTCUSDT'
        last_close = 100000.0
        tick_size = 0.1
        greed_count = 50
        greed_step = 0.2

        # Original
        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=greed_count, step=greed_step)
        original_greed.build_greed(last_close)

        # New
        new_grid = Grid(tick_size=Decimal(str(tick_size)), greed_count=greed_count, greed_step=greed_step)
        new_grid.build_greed(last_close)

        # Compare
        assert len(original_greed.greed) == len(new_grid.greed), \
            f"Length mismatch: original={len(original_greed.greed)}, new={len(new_grid.greed)}"

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.greed)):
            assert abs(orig['price'] - new['price']) < 0.00001, \
                f"Price mismatch at index {i}: original={orig['price']}, new={new['price']}"
            assert orig['side'] == new['side'], \
                f"Side mismatch at index {i}: original={orig['side']}, new={new['side']}"

    def test_build_greed_various_tick_sizes(self):
        """Test various tick sizes produce identical results."""
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        test_cases = [
            ('BTCUSDT', 100000.0, 0.1),
            ('ETHUSDT', 3000.0, 0.01),
            ('SOLUSDT', 150.0, 0.001),
        ]

        for symbol, last_close, tick_size in test_cases:
            # Original
            MockBybitApiUsdt.ticksizes[symbol] = tick_size
            mock_strat = MockStrat()
            original_greed = OriginalGreed(mock_strat, symbol, n=50, step=0.2)
            original_greed.build_greed(last_close)

            # New
            new_grid = Grid(tick_size=Decimal(str(tick_size)), greed_count=50, greed_step=0.2)
            new_grid.build_greed(last_close)

            # Compare
            for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.greed)):
                assert abs(orig['price'] - new['price']) < 0.0000001, \
                    f"{symbol}: Price mismatch at {i}: {orig['price']} vs {new['price']}"

    def test_update_greed_produces_identical_results(self):
        """Grid.update_greed() produces identical results as original Greed.update_greed()."""
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        # Setup
        symbol = 'BTCUSDT'
        last_close = 100000.0
        last_filled_price = 99800.0
        tick_size = 0.1

        # Original
        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=50, step=0.2)
        original_greed.build_greed(last_close)
        original_greed.update_greed(last_filled_price, last_close)

        # New
        new_grid = Grid(tick_size=Decimal(str(tick_size)), greed_count=50, greed_step=0.2)
        new_grid.build_greed(last_close)
        new_grid.update_greed(last_filled_price, last_close)

        # Compare
        assert len(original_greed.greed) == len(new_grid.greed)

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.greed)):
            assert abs(orig['price'] - new['price']) < 0.00001, \
                f"Price mismatch at {i} after update: {orig['price']} vs {new['price']}"
            assert orig['side'] == new['side'], \
                f"Side mismatch at {i} after update: {orig['side']} vs {new['side']}"

    def test_center_greed_buy_heavy_scenario(self):
        """Test grid centering when buy-heavy matches original."""
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        symbol = 'BTCUSDT'
        last_close = 100000.0
        tick_size = 0.1

        # Build grids
        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=50, step=0.2)
        original_greed.build_greed(last_close)

        new_grid = Grid(tick_size=Decimal(str(tick_size)), greed_count=50, greed_step=0.2)
        new_grid.build_greed(last_close)

        # Simulate buy-heavy scenario by marking sells as WAIT
        for g in original_greed.greed:
            if g['side'] == original_greed.SELL and g['price'] > 100500:
                g['side'] = original_greed.WAIT

        for g in new_grid.greed:
            if g['side'] == new_grid.SELL and g['price'] > 100500:
                g['side'] = new_grid.WAIT

        # Trigger centering via update
        last_filled = 99000.0
        original_greed.update_greed(last_filled, last_close)
        new_grid.update_greed(last_filled, last_close)

        # Compare results
        assert len(original_greed.greed) == len(new_grid.greed)

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.greed)):
            assert abs(orig['price'] - new['price']) < 0.00001, \
                f"Price mismatch at {i} after centering: {orig['price']} vs {new['price']}"

    def test_multiple_updates_sequence(self):
        """Test sequence of updates produces identical results."""
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        symbol = 'BTCUSDT'
        tick_size = 0.1

        # Build initial grids
        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=50, step=0.2)
        original_greed.build_greed(100000.0)

        new_grid = Grid(tick_size=Decimal(str(tick_size)), greed_count=50, greed_step=0.2)
        new_grid.build_greed(100000.0)

        # Simulate sequence of fills
        updates = [
            (99800.0, 99900.0),
            (99600.0, 99700.0),
            (99900.0, 100100.0),
            (100200.0, 100300.0),
        ]

        for last_filled, last_close in updates:
            original_greed.update_greed(last_filled, last_close)
            new_grid.update_greed(last_filled, last_close)

            # Verify they match after each update
            assert len(original_greed.greed) == len(new_grid.greed)

            for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.greed)):
                assert abs(orig['price'] - new['price']) < 0.00001, \
                    f"Price mismatch at {i} after update ({last_filled}, {last_close})"
                assert orig['side'] == new['side'], \
                    f"Side mismatch at {i} after update ({last_filled}, {last_close})"

    def test_update_greed_out_of_bounds_behavior(self):
        """Grid.update_greed() continues side assignment after out-of-bounds rebuild."""
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        # Setup
        symbol = 'BTCUSDT'
        last_close_initial = 100000.0
        tick_size = 0.1

        # Build initial grids
        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=50, step=0.2)
        original_greed.build_greed(last_close_initial)

        new_grid = Grid(tick_size=Decimal(str(tick_size)), greed_count=50, greed_step=0.2)
        new_grid.build_greed(last_close_initial)

        # Move price way outside grid bounds (trigger rebuild)
        last_filled_price = 99800.0
        last_close_out_of_bounds = 120000.0  # Way above grid max

        # Update both grids
        original_greed.update_greed(last_filled_price, last_close_out_of_bounds)
        new_grid.update_greed(last_filled_price, last_close_out_of_bounds)

        # Compare: both should have rebuilt and applied side assignment
        assert len(original_greed.greed) == len(new_grid.greed), \
            f"Length mismatch after out-of-bounds update: original={len(original_greed.greed)}, new={len(new_grid.greed)}"

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.greed)):
            assert abs(orig['price'] - new['price']) < 0.00001, \
                f"Price mismatch at {i} after out-of-bounds update: original={orig['price']}, new={new['price']}"
            assert orig['side'] == new['side'], \
                f"Side mismatch at {i} after out-of-bounds update: original={orig['side']}, new={new['side']}"


class TestGridValidationMethods:
    """Test validation methods work correctly."""

    def test_is_price_sorted_validation(self):
        """Validation methods detect incorrect states."""
        grid = Grid(tick_size=Decimal('0.1'), greed_count=50, greed_step=0.2)
        grid.build_greed(100000.0)

        # Should be valid
        assert grid.is_price_sorted() is True
        assert grid.is_greed_correct() is True

        # Break sorting
        if len(grid.greed) > 1:
            grid.greed[0]['price'], grid.greed[1]['price'] = grid.greed[1]['price'], grid.greed[0]['price']
            assert grid.is_price_sorted() is False

    def test_is_greed_correct_validation(self):
        """is_greed_correct detects invalid sequences."""
        grid = Grid(tick_size=Decimal('0.1'), greed_count=50, greed_step=0.2)
        grid.build_greed(100000.0)

        # Should be valid initially
        assert grid.is_greed_correct() is True

        # Break sequence (put SELL before BUY)
        if len(grid.greed) > 10:
            grid.greed[5]['side'] = 'Sell'  # Should be Buy
            assert grid.is_greed_correct() is False
