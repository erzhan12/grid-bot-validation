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


class MockLoggers:
    """Mock Loggers to avoid logging dependencies."""
    @staticmethod
    def log_exception(msg):
        pass


@pytest.fixture(autouse=True)
def setup_mocks(monkeypatch):
    """Set up mocks for original code."""
    # Mock BybitApiUsdt in the original greed module
    try:
        import greed as original_greed
        monkeypatch.setattr(original_greed, 'BybitApiUsdt', MockBybitApiUsdt)
        monkeypatch.setattr(original_greed, 'DbFiles', MockDbFiles)
    except ImportError:
        pass  # Will skip in individual tests

    # Mock Loggers in the original position module
    try:
        import position as original_position
        monkeypatch.setattr(original_position, 'Loggers', MockLoggers)
    except ImportError:
        pass  # Will skip in individual tests


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
        grid_count = 50
        grid_step = 0.2

        # Original
        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=grid_count, step=grid_step)
        original_greed.build_greed(last_close)

        # New
        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=grid_count, grid_step=grid_step)
        new_grid.build_grid(last_close)

        # Compare
        assert len(original_greed.greed) == len(new_grid.grid), \
            f"Length mismatch: original={len(original_greed.greed)}, new={len(new_grid.grid)}"

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.grid)):
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
            new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=50, grid_step=0.2)
            new_grid.build_grid(last_close)

            # Compare
            for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.grid)):
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
        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=50, grid_step=0.2)
        new_grid.build_grid(last_close)
        new_grid.update_grid(last_filled_price, last_close)

        # Compare
        assert len(original_greed.greed) == len(new_grid.grid)

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.grid)):
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

        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=50, grid_step=0.2)
        new_grid.build_grid(last_close)

        # Simulate buy-heavy scenario by marking sells as WAIT
        for g in original_greed.greed:
            if g['side'] == original_greed.SELL and g['price'] > 100500:
                g['side'] = original_greed.WAIT

        for g in new_grid.grid:
            if g['side'] == new_grid.SELL and g['price'] > 100500:
                g['side'] = new_grid.WAIT

        # Trigger centering via update
        last_filled = 99000.0
        original_greed.update_greed(last_filled, last_close)
        new_grid.update_grid(last_filled, last_close)

        # Compare results
        assert len(original_greed.greed) == len(new_grid.grid)

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.grid)):
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

        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=50, grid_step=0.2)
        new_grid.build_grid(100000.0)

        # Simulate sequence of fills
        updates = [
            (99800.0, 99900.0),
            (99600.0, 99700.0),
            (99900.0, 100100.0),
            (100200.0, 100300.0),
        ]

        for last_filled, last_close in updates:
            original_greed.update_greed(last_filled, last_close)
            new_grid.update_grid(last_filled, last_close)

            # Verify they match after each update
            assert len(original_greed.greed) == len(new_grid.grid)

            for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.grid)):
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

        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=50, grid_step=0.2)
        new_grid.build_grid(last_close_initial)

        # Move price way outside grid bounds (trigger rebuild)
        last_filled_price = 99800.0
        last_close_out_of_bounds = 120000.0  # Way above grid max

        # Update both grids
        original_greed.update_greed(last_filled_price, last_close_out_of_bounds)
        new_grid.update_grid(last_filled_price, last_close_out_of_bounds)

        # Compare: both should have rebuilt and applied side assignment
        assert len(original_greed.greed) == len(new_grid.grid), \
            f"Length mismatch after out-of-bounds update: original={len(original_greed.greed)}, new={len(new_grid.grid)}"

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.grid)):
            assert abs(orig['price'] - new['price']) < 0.00001, \
                f"Price mismatch at {i} after out-of-bounds update: original={orig['price']}, new={new['price']}"
            assert orig['side'] == new['side'], \
                f"Side mismatch at {i} after out-of-bounds update: original={orig['side']}, new={new['side']}"


class TestGridValidationMethods:
    """Test validation methods work correctly."""

    def test_is_price_sorted_validation(self):
        """Validation methods detect incorrect states."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Should be valid
        assert grid.is_price_sorted() is True
        assert grid.is_grid_correct() is True

        # Break sorting
        if len(grid.grid) > 1:
            grid.grid[0]['price'], grid.grid[1]['price'] = grid.grid[1]['price'], grid.grid[0]['price']
            assert grid.is_price_sorted() is False

    def test_is_grid_correct_validation(self):
        """is_grid_correct detects invalid sequences."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Should be valid initially
        assert grid.is_grid_correct() is True

        # Break sequence (put SELL before BUY)
        if len(grid.grid) > 10:
            grid.grid[5]['side'] = 'Sell'  # Should be Buy
            assert grid.is_grid_correct() is False


class TestPositionRiskManagerBehavior:
    """
    Verify PositionRiskManager implements the exact logic from original Position class.

    These tests verify behavior against the documented bbu2-master/position.py logic.
    Reference: bbu_reference/bbu2-master/position.py
    """

    def test_long_high_liq_risk_decreases_position(self):
        """
        Long position with high liq risk should decrease long (Sell=1.5).

        Original logic (position.py:60-61):
        if self.get_liquidation_ratio(last_close) > 1.05 * self.__min_liq_ratio:
            self.set_amount_multiplier(Position.SIDE_SELL, 1.5)
        """
        from gridcore.position import PositionRiskManager, PositionState, RiskConfig

        risk_config = RiskConfig(min_liq_ratio=0.8, max_liq_ratio=1.2, max_margin=5000.0, min_total_margin=1000.0)
        manager = PositionRiskManager('long', risk_config)

        # liq_ratio = 88000 / 100000 = 0.88 > 1.05 * 0.8 = 0.84 ✓
        position = PositionState(
            direction='long', size=Decimal('0.02'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('88000.0'), margin=Decimal('2000.0'), leverage=10
        )
        opposite = PositionState(
            direction='short', size=Decimal('0.01'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('110000.0'), margin=Decimal('1000.0'), leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(position, opposite, 100000.0, Decimal('10000.0'))
        assert multipliers['Sell'] == 1.5, "Should decrease long position with Sell=1.5"

    def test_short_moderate_liq_risk_increases_opposite(self):
        """
        Short position with moderate liq risk should increase opposite (long).

        Original logic (position.py:81-86):
        elif 0.0 < self.get_liquidation_ratio(last_close) < self.__max_liq_ratio:
            self.__opposite.set_amount_multiplier(Position.SIDE_SELL, 0.5)

        Gridcore equivalent: sets own SIDE_SELL = 0.5 to decrease short exposure
        """
        from gridcore.position import PositionRiskManager, PositionState, RiskConfig

        risk_config = RiskConfig(min_liq_ratio=0.8, max_liq_ratio=1.2, max_margin=5000.0, min_total_margin=1000.0)
        manager = PositionRiskManager('short', risk_config)

        # liq_ratio = 108000 / 100000 = 1.08 (0.0 < 1.08 < 1.2) ✓
        position = PositionState(
            direction='short', size=Decimal('0.015'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('108000.0'), margin=Decimal('1500.0'), leverage=10
        )
        opposite = PositionState(
            direction='long', size=Decimal('0.015'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('92000.0'), margin=Decimal('1500.0'), leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(position, opposite, 100000.0, Decimal('10000.0'))
        assert multipliers['Sell'] == 0.5, "Should decrease short sells to increase long"

    def test_small_long_position_increases_long(self):
        """
        Small long position (ratio < 0.20) should increase long (Buy=2.0).

        Original logic (position.py:73-74):
        elif self.position_ratio < 0.20:
            self.set_amount_multiplier(Position.SIDE_BUY, 2)
        """
        from gridcore.position import PositionRiskManager, PositionState, RiskConfig

        risk_config = RiskConfig(min_liq_ratio=0.8, max_liq_ratio=1.2, max_margin=5000.0, min_total_margin=1000.0)
        manager = PositionRiskManager('long', risk_config)

        # position_ratio = 100 / 3000 = 0.033 < 0.20 ✓
        position = PositionState(
            direction='long', size=Decimal('0.001'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('95000.0'), margin=Decimal('100.0'), leverage=10
        )
        opposite = PositionState(
            direction='short', size=Decimal('0.03'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('110000.0'), margin=Decimal('3000.0'), leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(position, opposite, 100000.0, Decimal('10000.0'))
        assert multipliers['Buy'] == 2.0, "Should increase long position with Buy=2.0"

    def test_large_short_position_losing_increases_short(self):
        """
        Large short position (ratio > 2.0) that's losing should increase short (Sell=2.0).

        Original logic (position.py:89-90):
        elif self.position_ratio > 2.0 and self.__upnl < 0:
            self.set_amount_multiplier(Position.SIDE_SELL, 2)
        """
        from gridcore.position import PositionRiskManager, PositionState, RiskConfig

        risk_config = RiskConfig(min_liq_ratio=0.8, max_liq_ratio=1.2, max_margin=5000.0, min_total_margin=1000.0)
        manager = PositionRiskManager('short', risk_config)

        # position_ratio = 2000 / 500 = 4.0 > 2.0 ✓
        # Price above entry (105000 > 100000) means short is losing
        position = PositionState(
            direction='short', size=Decimal('2.0'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('110000.0'), margin=Decimal('2000.0'), leverage=10
        )
        opposite = PositionState(
            direction='long', size=Decimal('0.5'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('90000.0'), margin=Decimal('500.0'), leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(position, opposite, 105000.0, Decimal('10000.0'))
        assert multipliers['Sell'] == 2.0, "Should increase short position with Sell=2.0"

    def test_low_total_margin_adjusts_position(self):
        """
        Equal positions with low total margin should adjust multipliers.

        Original logic (position.py:69-70 for long, 87-88 for short):
        elif self.is_position_equal() and self.get_total_margin() < self.__min_total_margin:
            self._adjust_position_for_low_margin()
        """
        from gridcore.position import PositionRiskManager, PositionState, RiskConfig

        risk_config = RiskConfig(
            min_liq_ratio=0.8, max_liq_ratio=1.2,
            max_margin=5000.0, min_total_margin=1000.0,
            increase_same_position_on_low_margin=False
        )
        manager = PositionRiskManager('long', risk_config)

        # position_ratio = 400 / 400 = 1.0 (is_equal ✓)
        # total_margin = 800 < 1000 ✓
        position = PositionState(
            direction='long', size=Decimal('1.0'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('90000.0'), margin=Decimal('400.0'), leverage=10
        )
        opposite = PositionState(
            direction='short', size=Decimal('1.0'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('110000.0'), margin=Decimal('400.0'), leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(position, opposite, 100000.0, Decimal('10000.0'))
        # Should reduce opposite side (Sell=0.5) to increase this position
        assert multipliers['Sell'] == 0.5, "Should adjust for low margin"


class TestEngineStrat50Behavior:
    """
    Verify GridEngine implements the exact logic from original Strat50 class.

    These tests verify behavior against the documented bbu2-master/strat.py logic.
    Reference: bbu_reference/bbu2-master/strat.py (Strat50 class)
    """

    def test_builds_grid_when_empty(self):
        """
        Engine should build grid when grid is empty (len <= 1).

        Original logic (strat.py:85-87):
        while len(self.greed.greed) <= 1:
            self.greed.build_greed(self.get_last_close())
        """
        from gridcore.engine import GridEngine
        from gridcore.config import GridConfig
        from gridcore.events import TickerEvent, EventType
        from datetime import datetime

        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config)

        # Grid should be empty initially
        assert len(engine.grid.grid) == 0

        # Send ticker event
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(),
            local_ts=datetime.now(),
            last_price=Decimal('100000.0')
        )

        intents = engine.on_event(event, {'long': [], 'short': []})

        # Grid should now be built
        assert len(engine.grid.grid) > 1, "Grid should be built after ticker event"
        assert len(engine.grid.grid) == 51, "Grid should have grid_count+1 items"

    def test_order_placement_eligibility_buy_below_market(self):
        """
        Buy orders must be below market price.

        Original logic (strat.py:169-172):
        diff_p = (last_close - greed['price']) / last_close * 100
        if (greed['side'] == self.greed.BUY and diff_p <= 0):
            return
        """
        from gridcore.engine import GridEngine
        from gridcore.config import GridConfig
        from gridcore.events import TickerEvent, EventType
        from datetime import datetime

        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config)

        # Build grid at 100000
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(),
            local_ts=datetime.now(),
            last_price=Decimal('100000.0')
        )
        intents = engine.on_event(event, {'long': [], 'short': []})

        # All Buy intents should have price < last_close
        buy_intents = [i for i in intents if hasattr(i, 'side') and i.side == 'Buy']
        for intent in buy_intents:
            assert float(intent.price) < 100000.0, f"Buy order at {intent.price} should be below market 100000"

    def test_order_placement_eligibility_sell_above_market(self):
        """
        Sell orders must be above market price.

        Original logic (strat.py:169-172):
        diff_p = (last_close - greed['price']) / last_close * 100
        if (greed['side'] == self.greed.SELL and diff_p >= 0):
            return
        """
        from gridcore.engine import GridEngine
        from gridcore.config import GridConfig
        from gridcore.events import TickerEvent, EventType
        from datetime import datetime

        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config)

        # Build grid at 100000
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(),
            local_ts=datetime.now(),
            last_price=Decimal('100000.0')
        )
        intents = engine.on_event(event, {'long': [], 'short': []})

        # All Sell intents should have price > last_close
        sell_intents = [i for i in intents if hasattr(i, 'side') and i.side == 'Sell']
        for intent in sell_intents:
            assert float(intent.price) > 100000.0, f"Sell order at {intent.price} should be above market 100000"

    def test_min_distance_from_current_price(self):
        """
        Orders must be at least grid_step/2 away from current price.

        Original logic (strat.py:175-176):
        if abs(diff_p) <= self.greed.greed_step / 2:
            return
        """
        from gridcore.engine import GridEngine
        from gridcore.config import GridConfig
        from gridcore.events import TickerEvent, EventType
        from datetime import datetime

        config = GridConfig(grid_count=50, grid_step=0.2)  # 0.2% step
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config)

        # Build grid at 100000
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(),
            local_ts=datetime.now(),
            last_price=Decimal('100000.0')
        )
        intents = engine.on_event(event, {'long': [], 'short': []})

        # All intents should be at least 0.1% (grid_step/2) away from current price
        min_distance_pct = 0.2 / 2  # grid_step / 2
        for intent in intents:
            if hasattr(intent, 'price'):
                diff_pct = abs((100000.0 - float(intent.price)) / 100000.0 * 100)
                assert diff_pct > min_distance_pct, \
                    f"Order at {intent.price} is too close to market (diff={diff_pct:.3f}%, min={min_distance_pct}%)"

    def test_too_many_orders_triggers_rebuild(self):
        """
        If limit orders > grid_count + 10, should rebuild grid.

        Original logic (strat.py:103-104):
        if len(limits) > len(self.greed.greed) + 10:
            self._rebuild_greed(self._symbol)
        """
        from gridcore.engine import GridEngine
        from gridcore.config import GridConfig
        from gridcore.events import TickerEvent, EventType
        from gridcore.intents import CancelIntent
        from datetime import datetime

        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config)

        # Build grid
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(),
            local_ts=datetime.now(),
            last_price=Decimal('100000.0')
        )
        engine.on_event(event, {'long': [], 'short': []})

        # Simulate too many orders (grid_count=50, so >61 should trigger rebuild)
        fake_orders = [
            {'orderId': f'order_{i}', 'price': str(100000 + i * 10), 'side': 'Buy'}
            for i in range(65)
        ]

        intents = engine.on_event(event, {'long': fake_orders, 'short': []})

        # Should generate cancel intents for rebuild
        cancel_intents = [i for i in intents if isinstance(i, CancelIntent) and i.reason == 'rebuild']
        assert len(cancel_intents) > 0, "Should generate cancel intents for rebuild when too many orders"

    def test_updates_grid_after_partial_fills(self):
        """
        If 0 < len(limits) < grid_count, should update grid.

        Original logic (strat.py:105-106):
        if len(limits) > 0 and len(limits) < self.greed.greed_count:
            self.greed.update_greed(self.get_last_filled_price(), self.get_last_close())
        """
        from gridcore.engine import GridEngine
        from gridcore.config import GridConfig
        from gridcore.events import TickerEvent, ExecutionEvent, EventType
        from datetime import datetime

        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config)

        # Build grid
        ticker_event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(),
            local_ts=datetime.now(),
            last_price=Decimal('100000.0')
        )
        engine.on_event(ticker_event, {'long': [], 'short': []})

        # Record grid state before execution
        grid_before = [{'side': g['side'], 'price': g['price']} for g in engine.grid.grid]

        # Simulate execution at 99800
        execution_event = ExecutionEvent(
            event_type=EventType.EXECUTION,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(),
            local_ts=datetime.now(),
            price=Decimal('99800.0'),
            qty=Decimal('0.01'),
            side='Buy'
        )
        engine.on_event(execution_event)

        # Simulate some limit orders (less than grid_count)
        partial_limits = [
            {'orderId': f'order_{i}', 'price': str(100000 + i * 100), 'side': 'Sell'}
            for i in range(10)  # Less than grid_count=50
        ]

        # Send another ticker to trigger update logic
        engine.on_event(ticker_event, {'long': [], 'short': partial_limits})

        # Grid should have been updated (some items marked as WAIT near 99800)
        grid_after = engine.grid.grid
        wait_items = [g for g in grid_after if g['side'] == 'wait']
        assert len(wait_items) > 0, "Grid should have WAIT items after execution"

    def test_cancel_intent_for_side_mismatch(self):
        """
        If existing limit has wrong side, should cancel and replace.

        Original logic (strat.py:147-149):
        if limit['side'] != greed['side']:
            self.cancel_order(limit['orderId'])
            self.__place_order(greed, direction)
        """
        from gridcore.engine import GridEngine
        from gridcore.config import GridConfig
        from gridcore.events import TickerEvent, EventType
        from gridcore.intents import CancelIntent, PlaceLimitIntent
        from datetime import datetime

        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config)

        # Build grid at 100000
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(),
            local_ts=datetime.now(),
            last_price=Decimal('100000.0')
        )
        engine.on_event(event, {'long': [], 'short': []})

        # Create a mismatched order (Sell at a price that should be Buy)
        mismatched_order = {
            'orderId': 'wrong_order_1',
            'price': '99800.0',  # Below market, should be Buy
            'side': 'Sell'  # But it's Sell - WRONG!
        }

        intents = engine.on_event(event, {'long': [mismatched_order], 'short': []})

        # Should generate cancel intent for side mismatch
        cancel_intents = [i for i in intents if isinstance(i, CancelIntent) and i.reason == 'side_mismatch']
        assert len(cancel_intents) > 0, "Should cancel orders with side mismatch"

        # Should also generate place intent to replace it
        place_intents = [i for i in intents if isinstance(i, PlaceLimitIntent)]
        assert len(place_intents) > 0, "Should place new orders after canceling mismatched"

    def test_cancel_orders_outside_grid_range(self):
        """
        Orders with prices outside grid range should be canceled.

        Original logic (strat.py:154-160):
        greed_price_set = {round(greed['price'], 8) for greed in self.greed.greed}
        for limit in limits:
            if limit_price not in greed_price_set:
                self.cancel_order(limit['orderId'])
        """
        from gridcore.engine import GridEngine
        from gridcore.config import GridConfig
        from gridcore.events import TickerEvent, EventType
        from gridcore.intents import CancelIntent
        from datetime import datetime

        config = GridConfig(grid_count=50, grid_step=0.2)
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config)

        # Build grid at 100000
        event = TickerEvent(
            event_type=EventType.TICKER,
            symbol='BTCUSDT',
            exchange_ts=datetime.now(),
            local_ts=datetime.now(),
            last_price=Decimal('100000.0')
        )
        engine.on_event(event, {'long': [], 'short': []})

        # Create orders way outside grid range
        outside_orders = [
            {'orderId': 'outside_1', 'price': '80000.0', 'side': 'Buy'},  # Way below
            {'orderId': 'outside_2', 'price': '120000.0', 'side': 'Sell'},  # Way above
        ]

        intents = engine.on_event(event, {'long': outside_orders, 'short': []})

        # Should generate cancel intents for outside orders
        cancel_intents = [i for i in intents if isinstance(i, CancelIntent) and i.reason == 'outside_grid']
        assert len(cancel_intents) >= 2, f"Should cancel orders outside grid, got {len(cancel_intents)} cancels"

    def test_deterministic_client_order_id(self):
        """
        Same grid level should produce same client_order_id.

        This ensures execution layer can detect duplicates.
        Reference: PlaceLimitIntent.create() using SHA256 hash
        """
        from gridcore.intents import PlaceLimitIntent

        # Create two intents with same parameters
        intent1 = PlaceLimitIntent.create(
            symbol='BTCUSDT',
            side='Buy',
            price=Decimal('99800.0'),
            qty=Decimal('0.01'),
            grid_level=5,
            direction='long'
        )

        intent2 = PlaceLimitIntent.create(
            symbol='BTCUSDT',
            side='Buy',
            price=Decimal('99800.0'),
            qty=Decimal('0.01'),
            grid_level=5,
            direction='long'
        )

        # Should have identical client_order_id
        assert intent1.client_order_id == intent2.client_order_id, \
            "Same parameters should produce same client_order_id"

        # Different grid level should produce different ID
        intent3 = PlaceLimitIntent.create(
            symbol='BTCUSDT',
            side='Buy',
            price=Decimal('99800.0'),
            qty=Decimal('0.01'),
            grid_level=6,  # Different level
            direction='long'
        )

        assert intent1.client_order_id != intent3.client_order_id, \
            "Different grid level should produce different client_order_id"
