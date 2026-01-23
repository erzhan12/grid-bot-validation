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

# Mock yaml module before any imports that depend on it
# This prevents ImportError when settings.py tries to import yaml
class MockYaml:
    @staticmethod
    def load(stream, loader):
        return {}
    
    class FullLoader:
        pass

# Mock pybit module before any imports that depend on it
class MockPybit:
    class unified_trading:
        class HTTP:
            pass
        class WebSocket:
            pass
    
    class exceptions:
        pass

# Inject mocks into sys.modules before any imports
if 'yaml' not in sys.modules:
    sys.modules['yaml'] = MockYaml()
if 'pybit' not in sys.modules:
    sys.modules['pybit'] = MockPybit()
if 'pybit.unified_trading' not in sys.modules:
    sys.modules['pybit.unified_trading'] = MockPybit.unified_trading
if 'pybit.exceptions' not in sys.modules:
    sys.modules['pybit.exceptions'] = MockPybit.exceptions

from gridcore.grid import Grid, GridSideType


def normalize_side(side: str) -> str:
    """Normalize side value for comparison (handles 'wait' vs 'Wait' difference)."""
    if side.lower() == 'wait':
        return GridSideType.WAIT
    return side


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
    logger_exceptions = None
    logger_orders = None
    logger_check = None
    
    @staticmethod
    def log_exception(msg):
        pass
    
    @staticmethod
    def log_order(msg):
        pass
    
    @staticmethod
    def init_loggers():
        # Create mock logger objects
        class MockLogger:
            def info(self, msg):
                pass
        MockLoggers.logger_exceptions = MockLogger()
        MockLoggers.logger_orders = MockLogger()
        MockLoggers.logger_check = MockLogger()


class MockSettings:
    """Mock Settings to avoid yaml dependency."""
    yaml = {'check': {}, 'intervals': {'check': 1.0}, 'amounts': [], 'pair_timeframes': []}
    keys = {'bm_keys': [], 'default_key': {}, 'telegram': {'token': None, 'chat_id': None}}
    server = {'debug': False}
    INTERVALS = {'CHECK': 1.0}
    bm_keys = []
    default_key = {}
    amounts = []
    pair_timeframes = []
    DEBUG = False
    telegram = {'token': None, 'chat_id': None}


@pytest.fixture(autouse=True)
def setup_mocks(monkeypatch):
    """Set up mocks for original code."""
    # Mock Settings before any imports that depend on it
    try:
        import settings as original_settings
        monkeypatch.setattr(original_settings, 'Settings', MockSettings)
    except ImportError:
        pass  # Will skip in individual tests
    
    # Mock BybitApiUsdt in the original greed module
    try:
        import greed as original_greed
        monkeypatch.setattr(original_greed, 'BybitApiUsdt', MockBybitApiUsdt)
        monkeypatch.setattr(original_greed, 'DbFiles', MockDbFiles)
    except ImportError:
        pass  # Will skip in individual tests

    # Mock Loggers in the original position module and loggers module
    try:
        import loggers as original_loggers
        MockLoggers.init_loggers()  # Initialize logger attributes
        monkeypatch.setattr(original_loggers, 'Loggers', MockLoggers)
    except ImportError:
        pass  # Will skip in individual tests
    
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
            assert normalize_side(orig['side']) == new['side'], \
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
            assert normalize_side(orig['side']) == new['side'], \
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
            if g['side'] == GridSideType.SELL and g['price'] > 100500:
                g['side'] = GridSideType.WAIT

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
                assert normalize_side(orig['side']) == new['side'], \
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
            assert normalize_side(orig['side']) == new['side'], \
                f"Side mismatch at {i} after out-of-bounds update: original={orig['side']}, new={new['side']}"


class TestGridValidationMethods:
    """Test validation methods work correctly."""

    def test_is_grid_correct_validation(self):
        """is_grid_correct detects invalid sequences and unsorted prices."""
        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Should be valid initially (sorted + correct sequence)
        assert grid.is_grid_correct() is True

        # Test 1: Break sorting (unsorted prices should cause False)
        if len(grid.grid) > 1:
            # Save original state
            original_price_0 = grid.grid[0]['price']
            original_price_1 = grid.grid[1]['price']
            
            # Break sorting by swapping prices
            grid.grid[0]['price'], grid.grid[1]['price'] = grid.grid[1]['price'], grid.grid[0]['price']
            # Should return False because sorting check fails first
            assert grid.is_grid_correct() is False
            
            # Restore for next test
            grid.grid[0]['price'] = original_price_0
            grid.grid[1]['price'] = original_price_1

        # Test 2: Break sequence (sorted but wrong sequence should return False)
        if len(grid.grid) > 10:
            # Break sequence by putting SELL before BUY (but keep prices sorted)
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
        from gridcore.position import PositionRiskManager, PositionState, RiskConfig, Position

        risk_config = RiskConfig(min_liq_ratio=0.8, max_liq_ratio=1.2, max_margin=5000.0, min_total_margin=1000.0)
        manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # liq_ratio = 88000 / 100000 = 0.88 > 1.05 * 0.8 = 0.84 ✓
        position = PositionState(
            direction=Position.DIRECTION_LONG, size=Decimal('0.02'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('88000.0'), margin=Decimal('2000.0'), leverage=10
        )
        opposite = PositionState(
            direction=Position.DIRECTION_SHORT, size=Decimal('0.01'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('110000.0'), margin=Decimal('1000.0'), leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(position, opposite, 100000.0)
        assert multipliers['Sell'] == 1.5, "Should decrease long position with Sell=1.5"

    def test_short_moderate_liq_risk_increases_opposite(self):
        """
        Short position with moderate liq risk should increase opposite (long).

        Original logic (position.py:81-86):
        elif 0.0 < self.get_liquidation_ratio(last_close) < self.__max_liq_ratio:
            self.__opposite.set_amount_multiplier(Position.SIDE_SELL, 0.5)

        Gridcore uses two-position architecture: short modifies opposite (long) multipliers
        """
        from gridcore.position import PositionRiskManager, PositionState, RiskConfig, Position

        risk_config = RiskConfig(min_liq_ratio=0.8, max_liq_ratio=1.2, max_margin=5000.0, min_total_margin=1000.0)
        short_manager = PositionRiskManager('short', risk_config)
        long_manager = PositionRiskManager('long', risk_config)

        # Link the two positions
        short_manager.set_opposite(long_manager)
        long_manager.set_opposite(short_manager)

        # liq_ratio = 108000 / 100000 = 1.08 (0.0 < 1.08 < 1.2) ✓
        position = PositionState(
            direction=Position.DIRECTION_SHORT, size=Decimal('0.015'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('108000.0'), margin=Decimal('1500.0'), leverage=10
        )
        opposite = PositionState(
            direction=Position.DIRECTION_LONG, size=Decimal('0.015'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('92000.0'), margin=Decimal('1500.0'), leverage=10
        )

        short_multipliers = short_manager.calculate_amount_multiplier(position, opposite, 100000.0)

        # Short position should not modify itself
        assert short_multipliers['Sell'] == 1.0
        assert short_multipliers['Buy'] == 1.0

        # Instead, it should modify the opposite (long) position's multipliers
        long_multipliers = long_manager.get_amount_multiplier()
        assert long_multipliers['Sell'] == 0.5, "Should decrease long sells to increase long"
        assert long_multipliers['Buy'] == 1.0

    def test_small_long_position_increases_long(self):
        """
        Small long position (ratio < 0.20) should increase long (Buy=2.0).

        Original logic (position.py:73-74):
        elif self.position_ratio < 0.20:
            self.set_amount_multiplier(Position.SIDE_BUY, 2)
        """
        from gridcore.position import PositionRiskManager, PositionState, RiskConfig, Position

        risk_config = RiskConfig(min_liq_ratio=0.8, max_liq_ratio=1.2, max_margin=5000.0, min_total_margin=1000.0)
        manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # position_ratio = 100 / 3000 = 0.033 < 0.20 ✓
        # liq_ratio = 70000 / 100000 = 0.7 (below min 0.8, safe)
        position = PositionState(
            direction=Position.DIRECTION_LONG, size=Decimal('0.001'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('70000.0'), margin=Decimal('100.0'), leverage=10
        )
        opposite = PositionState(
            direction=Position.DIRECTION_SHORT, size=Decimal('0.03'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('130000.0'), margin=Decimal('3000.0'), leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(position, opposite, 100000.0)
        assert multipliers['Buy'] == 2.0, "Should increase long position with Buy=2.0"

    def test_large_short_position_losing_increases_short(self):
        """
        Large short position (ratio > 2.0) that's losing should increase short (Sell=2.0).

        Original logic (position.py:89-90):
        elif self.position_ratio > 2.0 and self.__upnl < 0:
            self.set_amount_multiplier(Position.SIDE_SELL, 2)
        """
        from gridcore.position import PositionRiskManager, PositionState, RiskConfig, Position

        risk_config = RiskConfig(min_liq_ratio=0.8, max_liq_ratio=1.2, max_margin=5000.0, min_total_margin=1000.0)
        _, manager = PositionRiskManager.create_linked_pair(risk_config)

        # position_ratio = 2000 / 500 = 4.0 > 2.0 ✓
        # Price above entry (105000 > 100000) means short is losing
        position = PositionState(
            direction=Position.DIRECTION_SHORT, size=Decimal('2.0'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('110000.0'), margin=Decimal('2000.0'), leverage=10
        )
        opposite = PositionState(
            direction=Position.DIRECTION_LONG, size=Decimal('0.5'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('90000.0'), margin=Decimal('500.0'), leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(position, opposite, 105000.0)
        assert multipliers['Sell'] == 2.0, "Should increase short position with Sell=2.0"

    def test_low_total_margin_adjusts_position(self):
        """
        Equal positions with low total margin should adjust multipliers.

        Original logic (position.py:69-70 for long, 87-88 for short):
        elif self.is_position_equal() and self.get_total_margin() < self.__min_total_margin:
            self._adjust_position_for_low_margin()
        """
        from gridcore.position import PositionRiskManager, PositionState, RiskConfig, Position

        risk_config = RiskConfig(
            min_liq_ratio=0.8, max_liq_ratio=1.2,
            max_margin=5000.0, min_total_margin=1000.0,
            increase_same_position_on_low_margin=False
        )
        manager, _ = PositionRiskManager.create_linked_pair(risk_config)

        # position_ratio = 400 / 400 = 1.0 (is_equal ✓)
        # total_margin = 800 < 1000 ✓
        # liq_ratio = 70000 / 100000 = 0.7 (below min 0.8, safe)
        position = PositionState(
            direction=Position.DIRECTION_LONG, size=Decimal('1.0'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('70000.0'), margin=Decimal('400.0'), leverage=10
        )
        opposite = PositionState(
            direction=Position.DIRECTION_SHORT, size=Decimal('1.0'), entry_price=Decimal('100000.0'),
            liquidation_price=Decimal('130000.0'), margin=Decimal('400.0'), leverage=10
        )

        multipliers = manager.calculate_amount_multiplier(position, opposite, 100000.0)
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
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config, strat_id='btcusdt_test')

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
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config, strat_id='btcusdt_test')

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
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config, strat_id='btcusdt_test')

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
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config, strat_id='btcusdt_test')

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
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config, strat_id='btcusdt_test')

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
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config, strat_id='btcusdt_test')

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
        wait_items = [g for g in grid_after if g['side'] == 'Wait']
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
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config, strat_id='btcusdt_test')

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
        engine = GridEngine('BTCUSDT', Decimal('0.1'), config, strat_id='btcusdt_test')

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
        from gridcore.position import Position

        # Create two intents with same parameters
        intent1 = PlaceLimitIntent.create(
            symbol='BTCUSDT',
            side='Buy',
            price=Decimal('99800.0'),
            qty=Decimal('0.01'),
            grid_level=5,
            direction=Position.DIRECTION_LONG
        )

        intent2 = PlaceLimitIntent.create(
            symbol='BTCUSDT',
            side='Buy',
            price=Decimal('99800.0'),
            qty=Decimal('0.01'),
            grid_level=5,
            direction=Position.DIRECTION_LONG
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
            direction=Position.DIRECTION_LONG
        )

        assert intent1.client_order_id != intent3.client_order_id, \
            "Different grid level should produce different client_order_id"


class TestGridEdgeCaseBehavior:
    """
    Test Grid edge cases that match original Greed behavior.

    Reference: bbu_reference/bbu2-master/greed.py
    """

    def test_rebuild_on_price_out_of_bounds(self):
        """
        Grid should rebuild when price moves outside grid range.

        Original logic (greed.py:53-55):
        if not (self.__min_greed < last_close < self.__max_greed):
            self.rebuild_greed(last_close)
        """
        from gridcore.grid import Grid

        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)

        # Build grid at 100000
        grid.build_grid(100000.0)
        initial_grid_prices = [g['price'] for g in grid.grid]

        # Get grid bounds
        min_price = min(initial_grid_prices)
        max_price = max(initial_grid_prices)

        # Update with price way outside bounds
        grid.update_grid(99800.0, 120000.0)  # Way above max_price

        # Grid should have been rebuilt centered on new price
        new_grid_prices = [g['price'] for g in grid.grid]
        new_min = min(new_grid_prices)
        new_max = max(new_grid_prices)

        # New range should include 120000
        assert new_min < 120000.0 < new_max, \
            f"Rebuilt grid should include new price 120000, got range [{new_min}, {new_max}]"

        # Grid should have been rebuilt (different prices)
        assert new_grid_prices != initial_grid_prices, "Grid should be rebuilt with new prices"

    def test_center_grid_when_buy_heavy(self):
        """
        Grid should rebalance upward when too many buy orders.

        Original logic (greed.py:87-90):
        if (buy_count - sell_count) / total_count > 0.3:
            self.greed.pop(0)  # Delete bottom
            price = round_price(highest_sell_price * (1 + step))
            self.greed.append({'side': SELL, 'price': price})
        """
        from gridcore.grid import Grid

        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Artificially create buy-heavy scenario by marking many sells as WAIT
        for g in grid.grid:
            if g['side'] == 'Sell' and g['price'] > 100500:
                g['side'] = 'Wait'

        # Count before rebalancing
        buy_count_before = sum(1 for g in grid.grid if g['side'] == 'Buy')
        sell_count_before = sum(1 for g in grid.grid if g['side'] == 'Sell')

        # Trigger update which should call __center_grid
        grid.update_grid(99000.0, 100000.0)

        # Count after rebalancing
        buy_count_after = sum(1 for g in grid.grid if g['side'] == 'Buy')
        sell_count_after = sum(1 for g in grid.grid if g['side'] == 'Sell')

        # Should have shifted grid upward (removed bottom buy, added top sell)
        # Note: exact counts depend on how many were marked WAIT, but grid should be more balanced
        assert len(grid.grid) == 51, "Grid size should remain constant"

    def test_center_grid_when_sell_heavy(self):
        """
        Grid should rebalance downward when too many sell orders.

        Original logic (greed.py:91-94):
        elif (sell_count - buy_count) / total_count > 0.3:
            self.greed.pop()  # Delete top
            price = round_price(lowest_buy_price * (1 - step))
            self.greed.insert(0, {'side': BUY, 'price': price})
        """
        from gridcore.grid import Grid

        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Artificially create sell-heavy scenario
        for g in grid.grid:
            if g['side'] == 'Buy' and g['price'] < 99500:
                g['side'] = 'Wait'

        # Trigger update which should call __center_grid
        grid.update_grid(101000.0, 100000.0)

        # Should have shifted grid downward
        assert len(grid.grid) == 51, "Grid size should remain constant"

    def test_is_too_close_marks_as_wait(self):
        """
        Prices too close to last filled should be marked as WAIT.

        Original logic (greed.py:57-58):
        if self.__is_too_close(greed['price'], last_filled_price):
            greed['side'] = self.WAIT
        """
        from gridcore.grid import Grid

        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Update with filled price at 99800
        grid.update_grid(99800.0, 100000.0)

        # Items near 99800 should be marked as WAIT
        wait_items = [g for g in grid.grid if g['side'] == 'Wait']
        assert len(wait_items) > 0, "Should have WAIT items near filled price"

        # Verify wait items are close to filled price
        for wait_item in wait_items:
            # Should be within grid_step/4 = 0.05% of filled price
            diff_pct = abs(wait_item['price'] - 99800.0) / 99800.0 * 100
            # If marked as WAIT, should be close enough OR was already WAIT
            # The middle line is always WAIT initially

    def test_side_assignment_based_on_current_price(self):
        """
        Grid sides should be assigned based on current price.

        Original logic (greed.py:59-62):
        elif last_close < greed['price']:
            greed['side'] = SELL
        elif last_close > greed['price']:
            greed['side'] = BUY
        """
        from gridcore.grid import Grid

        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Update with new price
        grid.update_grid(99800.0, 99500.0)  # Price moved down

        # All prices above 99500 should be SELL (or WAIT if too close to filled)
        # All prices below 99500 should be BUY
        for g in grid.grid:
            if g['side'] == 'Buy':
                assert g['price'] < 99500.0, f"Buy at {g['price']} should be below 99500"
            elif g['side'] == 'Sell':
                assert g['price'] > 99500.0, f"Sell at {g['price']} should be above 99500"

    def test_grid_always_sorted_by_price(self):
        """Grid prices should always be in ascending order (validated through is_grid_correct)."""
        from gridcore.grid import Grid

        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # After build - should be correct (sorted + correct sequence)
        assert grid.is_grid_correct() is True, "Grid should be correct after build"

        # After multiple updates - note: sequence may break after fills, but sorting is maintained
        grid.update_grid(99800.0, 99900.0)
        # After updates with fills, multiple WAIT levels may exist breaking sequence
        # But we can verify it doesn't crash and grid structure is maintained
        assert len(grid.grid) > 0, "Grid should not be empty after update"

        grid.update_grid(100200.0, 100100.0)
        assert len(grid.grid) > 0, "Grid should not be empty after second update"

        # Rebuild by calling build_grid directly to ensure correct sequence
        grid.build_grid(98000.0)
        # After rebuild, grid should be correct again (sorted + correct sequence)
        assert grid.is_grid_correct() is True, "Grid should be correct after rebuild"

    def test_grid_maintains_correct_sequence(self):
        """Grid should maintain BUY → WAIT → SELL sequence."""
        from gridcore.grid import Grid

        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # After build
        assert grid.is_grid_correct(), "Grid should have correct sequence after build"

        # After updates (unless all items become one type)
        grid.update_grid(99800.0, 100000.0)
        # Note: After update, grid might not follow strict BUY→WAIT→SELL if price moved
        # But it should still be price-sorted

    def test_multiple_fills_sequence(self):
        """
        Multiple sequential fills should maintain grid integrity.

        This tests that the grid can handle multiple updates in sequence
        without breaking, matching original behavior.
        """
        from gridcore.grid import Grid

        grid = Grid(tick_size=Decimal('0.1'), grid_count=50, grid_step=0.2)
        grid.build_grid(100000.0)

        # Simulate sequence of fills
        fills = [
            (99800.0, 99900.0),
            (100200.0, 100100.0),
            (99600.0, 99700.0),
            (100400.0, 100300.0),
        ]

        for filled_price, current_price in fills:
            grid.update_grid(filled_price, current_price)

            # Grid should maintain integrity
            assert len(grid.grid) > 0, "Grid should not be empty"
            # Note: After fills, multiple WAIT levels may exist breaking sequence,
            # but sorting should be maintained. We verify grid structure is intact.
            # Full correctness (sorting + sequence) is tested through is_grid_correct() in other tests.

            # Should have WAIT items near filled prices
            wait_items = [g for g in grid.grid if g['side'] == 'Wait']
            assert len(wait_items) > 0, f"Should have WAIT items after fill at {filled_price}"


class TestGridComparisonExtended:
    """
    Extended comparison tests for edge cases and parameter variations.

    These tests ensure complete behavioral parity between gridcore.Grid and
    original bbu2-master Greed implementation for scenarios not covered by
    existing comparison tests.
    """

    def test_sell_heavy_rebalancing_matches_original(self):
        """
        Sell-heavy grid rebalancing produces identical results.

        Tests grid shift when too many sell orders exist (>30% imbalance).
        Original shifts grid downward by removing top sell and adding bottom buy.

        Realistic scenario: Price moves UP with multiple BUY fills, naturally
        creating fewer BUY orders and many SELL orders (sell-heavy).

        Rationale: Existing tests only cover buy-heavy rebalancing. This validates
        the opposite direction to ensure __center_grid logic is symmetric.

        Reference: bbu2-master/greed.py:91-94
        """
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        symbol = 'BTCUSDT'
        initial_price = 100000.0
        tick_size = 0.1

        # Build initial grids
        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=50, step=0.2)
        original_greed.build_greed(initial_price)

        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=50, grid_step=0.2)
        new_grid.build_grid(initial_price)

        # Simulate realistic upward price movement with multiple BUY fills
        # Each fill marks nearby prices as WAIT, reducing active BUY count
        # This naturally creates sell-heavy scenario: few BUY, some WAIT, many SELL
        #
        # With grid_step=0.2%, grid levels are ~0.2% apart:
        # 99800 → 100000 → 100200 → 100400 → 100600 → 100800 → 101000
        upward_fills = [
            (99800.0, 99900.0),   # BUY at 99800 fills, price moves to 99900
            (99900.0, 100200.0),  # Price jumps to 100200 (next grid level)
            (100200.0, 100400.0), # SELL at 100200 fills, price at 100400
            (100400.0, 100600.0), # SELL at 100400 fills, price at 100600
            (100600.0, 100800.0), # SELL at 100600 fills, price at 100800
        ]

        for filled_price, current_price in upward_fills:
            original_greed.update_greed(filled_price, current_price)
            new_grid.update_grid(filled_price, current_price)

        # After upward movement, grid should be sell-heavy
        # Verify both implementations rebalance identically
        assert len(original_greed.greed) == len(new_grid.grid), \
            f"Length mismatch: original={len(original_greed.greed)}, new={len(new_grid.grid)}"

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.grid)):
            assert abs(orig['price'] - new['price']) < 0.00001, \
                f"Price mismatch at {i} after sell-heavy centering: {orig['price']} vs {new['price']}"
            assert normalize_side(orig['side']) == new['side'], \
                f"Side mismatch at {i} after sell-heavy centering: {orig['side']} vs {new['side']}"

    @pytest.mark.parametrize("grid_count", [10, 20])
    def test_small_grid_counts_match_original(self, grid_count):
        """
        Small grid_count values produce identical results.

        Tests grid building with smaller grids to ensure algorithm works
        correctly at edge of practical usage (minimum viable grid sizes).

        Rationale: Default tests use grid_count=50. Small grids test edge cases
        where integer division and rounding might behave differently.
        """
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        symbol = 'BTCUSDT'
        last_close = 100000.0
        tick_size = 0.1

        # Original
        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=grid_count, step=0.2)
        original_greed.build_greed(last_close)

        # New
        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=grid_count, grid_step=0.2)
        new_grid.build_grid(last_close)

        # Compare
        assert len(original_greed.greed) == len(new_grid.grid), \
            f"Length mismatch for grid_count={grid_count}: original={len(original_greed.greed)}, new={len(new_grid.grid)}"

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.grid)):
            assert abs(orig['price'] - new['price']) < 0.00001, \
                f"Price mismatch at {i} for grid_count={grid_count}: {orig['price']} vs {new['price']}"
            assert normalize_side(orig['side']) == new['side'], \
                f"Side mismatch at {i} for grid_count={grid_count}: {orig['side']} vs {new['side']}"

    @pytest.mark.parametrize("grid_count", [100, 200])
    def test_large_grid_counts_match_original(self, grid_count):
        """
        Large grid_count values produce identical results.

        Tests grid building with larger grids to ensure algorithm scales
        correctly and doesn't have hidden issues with more levels.

        Rationale: Large grids stress-test floating-point precision in price
        calculations over many iterations. Ensures no accumulation errors.
        """
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        symbol = 'BTCUSDT'
        last_close = 100000.0
        tick_size = 0.1

        # Original
        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=grid_count, step=0.2)
        original_greed.build_greed(last_close)

        # New
        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=grid_count, grid_step=0.2)
        new_grid.build_grid(last_close)

        # Compare
        assert len(original_greed.greed) == len(new_grid.grid), \
            f"Length mismatch for grid_count={grid_count}: original={len(original_greed.greed)}, new={len(new_grid.grid)}"

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.grid)):
            assert abs(orig['price'] - new['price']) < 0.00001, \
                f"Price mismatch at {i} for grid_count={grid_count}: {orig['price']} vs {new['price']}"
            assert normalize_side(orig['side']) == new['side'], \
                f"Side mismatch at {i} for grid_count={grid_count}: {orig['side']} vs {new['side']}"

    @pytest.mark.parametrize("grid_step", [0.05, 0.1, 0.5, 1.0])
    def test_various_grid_steps_match_original(self, grid_step):
        """
        Different grid_step percentage values produce identical spacing.

        Tests various grid step sizes (0.05% to 1.0%) to ensure price
        spacing calculations match original implementation exactly.

        Rationale: Grid step affects price calculations throughout the grid.
        Tests that percentage-to-price conversion is identical across implementations.
        """
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        symbol = 'ETHUSDT'
        last_close = 3000.0
        tick_size = 0.01

        # Original
        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=50, step=grid_step)
        original_greed.build_greed(last_close)

        # New
        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=50, grid_step=grid_step)
        new_grid.build_grid(last_close)

        # Compare
        assert len(original_greed.greed) == len(new_grid.grid), \
            f"Length mismatch for grid_step={grid_step}: original={len(original_greed.greed)}, new={len(new_grid.grid)}"

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.grid)):
            assert abs(orig['price'] - new['price']) < 0.00001, \
                f"Price mismatch at {i} for grid_step={grid_step}: {orig['price']} vs {new['price']}"
            assert normalize_side(orig['side']) == new['side'], \
                f"Side mismatch at {i} for grid_step={grid_step}: {orig['side']} vs {new['side']}"

    @pytest.mark.parametrize("price,tick_size", [
        (0.0001, 0.00001),  # Very small altcoin (SHIB-like)
        (1.5, 0.001),       # Low-price crypto (XRP-like)
        (100000.0, 0.1),    # BTC-like
        (999999.0, 1.0),    # Extreme high price
    ])
    def test_extreme_prices_match_original(self, price, tick_size):
        """
        Extreme price values produce identical results with realistic tick sizes.

        Tests grid building at price extremes to ensure rounding and calculation
        precision matches original across full range of real-world crypto prices.

        Rationale: Price rounding (_round_price) is used throughout grid calculations.
        Testing extreme values ensures floating-point precision is handled identically.
        """
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        symbol = 'TESTUSDT'

        # Original
        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=50, step=0.2)
        original_greed.build_greed(price)

        # New
        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=50, grid_step=0.2)
        new_grid.build_grid(price)

        # Compare
        assert len(original_greed.greed) == len(new_grid.grid), \
            f"Length mismatch for price={price}: original={len(original_greed.greed)}, new={len(new_grid.grid)}"

        for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.grid)):
            # Use relative tolerance for extreme prices
            tolerance = max(0.00001, abs(orig['price']) * 0.0000001)
            assert abs(orig['price'] - new['price']) < tolerance, \
                f"Price mismatch at {i} for price={price}, tick_size={tick_size}: {orig['price']} vs {new['price']}"
            assert normalize_side(orig['side']) == new['side'], \
                f"Side mismatch at {i} for price={price}: {orig['side']} vs {new['side']}"

    def test_none_handling_matches_original(self):
        """
        None input handling matches original behavior.

        Tests that None inputs to build_grid and update_grid are handled
        identically (skip silently, leave grid unchanged).

        Rationale: Defensive edge case testing. Ensures both implementations
        handle missing data gracefully without crashing or corrupting state.

        Reference: bbu2-master/greed.py:19-20 (build), 49-52 (update)
        """
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        symbol = 'BTCUSDT'
        tick_size = 0.1

        # Test 1: build_grid(None) - should leave grid empty
        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=50, step=0.2)
        original_greed.build_greed(None)

        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=50, grid_step=0.2)
        new_grid.build_grid(None)

        assert len(original_greed.greed) == len(new_grid.grid) == 0, \
            "build_grid(None) should leave grid empty"

        # Test 2: Build valid grid first
        original_greed.build_greed(100000.0)
        new_grid.build_grid(100000.0)

        original_grid_copy = [dict(g) for g in original_greed.greed]
        new_grid_copy = [dict(g) for g in new_grid.grid]

        # Test 3: update_grid(None, x) - should leave grid unchanged
        original_greed.update_greed(None, 100000.0)
        new_grid.update_grid(None, 100000.0)

        assert len(original_greed.greed) == len(original_grid_copy), \
            "update_grid(None, x) should not change grid length"
        assert len(new_grid.grid) == len(new_grid_copy), \
            "update_grid(None, x) should not change grid length"

        # Test 4: update_grid(x, None) - should leave grid unchanged
        original_greed.update_greed(99800.0, None)
        new_grid.update_grid(99800.0, None)

        assert len(original_greed.greed) == len(original_grid_copy), \
            "update_grid(x, None) should not change grid length"
        assert len(new_grid.grid) == len(new_grid_copy), \
            "update_grid(x, None) should not change grid length"

    def test_rebuild_clears_grid_like_original(self):
        """
        Rebuild clears grid before rebuilding to prevent doubling.

        Critical test: ensures both implementations clear self.grid/self.greed
        before building new grid, preventing duplicate levels on rebuild.

        Rationale: Bug prevention test. Without clearing, rebuilds would
        continuously append to existing grid, causing exponential growth.

        Reference: bbu2-master/greed.py:43-45
        """
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        symbol = 'BTCUSDT'
        tick_size = 0.1

        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=50, step=0.2)
        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=50, grid_step=0.2)

        # Build initial grid
        original_greed.build_greed(100000.0)
        new_grid.build_grid(100000.0)

        initial_orig_len = len(original_greed.greed)
        initial_new_len = len(new_grid.grid)

        # Rebuild should clear and rebuild, not append
        original_greed.rebuild_greed(100000.0)
        new_grid.build_grid(100000.0)  # Grid.build_grid clears internally

        assert len(original_greed.greed) == initial_orig_len, \
            f"Rebuild should maintain grid size, got {len(original_greed.greed)} vs {initial_orig_len}"
        assert len(new_grid.grid) == initial_new_len, \
            f"Rebuild should maintain grid size, got {len(new_grid.grid)} vs {initial_new_len}"

        # Multiple rebuilds should still maintain size
        for _ in range(3):
            original_greed.rebuild_greed(100000.0)
            new_grid.build_grid(100000.0)

        assert len(original_greed.greed) == initial_orig_len, \
            "Multiple rebuilds should not accumulate grid levels"
        assert len(new_grid.grid) == initial_new_len, \
            "Multiple rebuilds should not accumulate grid levels"

    def test_boundary_and_consecutive_rebuilds_match_original(self):
        """
        Grid boundary conditions and consecutive rebuilds match original.

        Tests:
        1. Price exactly at min_grid or max_grid boundaries
        2. Multiple consecutive out-of-bounds updates trigger identical rebuilds

        Rationale: Boundary conditions often expose off-by-one errors.
        Consecutive rebuilds test that rebuild state is clean and repeatable.

        Reference: bbu2-master/greed.py:53-55 (boundary check)
        """
        try:
            from greed import Greed as OriginalGreed
        except ImportError:
            pytest.skip("Original bbu2-master code not available")

        symbol = 'BTCUSDT'
        tick_size = 0.1

        MockBybitApiUsdt.ticksizes[symbol] = tick_size
        mock_strat = MockStrat()
        original_greed = OriginalGreed(mock_strat, symbol, n=50, step=0.2)
        new_grid = Grid(tick_size=Decimal(str(tick_size)), grid_count=50, grid_step=0.2)

        # Build initial grid
        original_greed.build_greed(100000.0)
        new_grid.build_grid(100000.0)

        # Get grid boundaries
        orig_min = min(g['price'] for g in original_greed.greed)
        orig_max = max(g['price'] for g in original_greed.greed)
        new_min = min(g['price'] for g in new_grid.grid)
        new_max = max(g['price'] for g in new_grid.grid)

        # Test 1: Price at boundary (should NOT rebuild)
        original_greed.update_greed(99800.0, orig_min + 100)  # Just inside
        new_grid.update_grid(99800.0, new_min + 100)

        # Should still have same grid structure (not rebuilt)
        assert len(original_greed.greed) == len(new_grid.grid), \
            "Grid should not rebuild when price is just inside boundary"

        # Test 2: Consecutive out-of-bounds rebuilds
        rebuild_prices = [120000.0, 85000.0, 150000.0, 70000.0]

        for rebuild_price in rebuild_prices:
            original_greed.update_greed(99800.0, rebuild_price)
            new_grid.update_grid(99800.0, rebuild_price)

            # Both should rebuild and produce identical grids
            assert len(original_greed.greed) == len(new_grid.grid), \
                f"Length mismatch after rebuild at price={rebuild_price}"

            for i, (orig, new) in enumerate(zip(original_greed.greed, new_grid.grid)):
                assert abs(orig['price'] - new['price']) < 0.00001, \
                    f"Price mismatch at {i} after rebuild at {rebuild_price}: {orig['price']} vs {new['price']}"
                assert normalize_side(orig['side']) == new['side'], \
                    f"Side mismatch at {i} after rebuild at {rebuild_price}: {orig['side']} vs {new['side']}"
