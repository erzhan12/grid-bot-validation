"""Test enhanced Strat50 with backtest functionality"""

from datetime import datetime
from unittest.mock import Mock, patch

from src.backtest_session import BacktestSession
from src.bybit_api_usdt import BybitApiUsdt
from src.position import Direction
from src.strat import Strat50


def create_test_strat50():
    """Create a Strat50 instance for testing"""
    
    # Mock controller
    controller = Mock()
    controller.get_same_orders_error.return_value = False
    controller.get_limit_orders.return_value = []
    controller.check_positions_ratio.return_value = None
    controller.new_order.return_value = (50000.0, "ORDER_001")
    controller.cancel_order.return_value = True
    controller.get_last_filled_order.return_value = None
    
    # Mock data provider to avoid database dependency
    with patch('src.strat.DataProvider'):
        strat = Strat50.create_for_testing("BTCUSDT", controller)
        
        # Add a mock BM
        mock_bm = Mock(spec=BybitApiUsdt)
        mock_bm.strat = strat.id
        mock_bm.name = "test_bm"
        mock_bm.symbol = "BTCUSDT"
        mock_bm.current_timestamp = None
        mock_bm.backtest_session = None
        
        # Mock position objects
        mock_long_pos = Mock()
        mock_long_pos.is_empty.return_value = True
        mock_long_pos.size = 0
        mock_long_pos.entry_price = 0
        mock_long_pos.get_margin.return_value = 0
        mock_long_pos.liq_price = 0
        
        mock_short_pos = Mock()
        mock_short_pos.is_empty.return_value = True
        mock_short_pos.size = 0
        mock_short_pos.entry_price = 0
        mock_short_pos.get_margin.return_value = 0
        mock_short_pos.liq_price = 0
        
        mock_bm.position = {
            Direction.LONG: mock_long_pos,
            Direction.SHORT: mock_short_pos
        }
        
        # Mock order fill checking
        mock_bm.check_and_fill_orders = Mock()
        
        strat.bms = [mock_bm]
        
        return strat, mock_bm


def test_strat50_basic_processing():
    """Test basic price processing without backtest session"""
    strat, mock_bm = create_test_strat50()
    
    timestamp = datetime.now()
    last_close = 50000.0
    
    # Process a price tick
    strat._process_single_last_close(last_close, timestamp)
    
    # Should set timestamp on BM
    assert mock_bm.current_timestamp == timestamp
    
    # Should call order fill checking
    mock_bm.check_and_fill_orders.assert_called_once_with("BTCUSDT", last_close, timestamp)
    
    # Should store the last close
    assert strat.last_close == last_close


def test_strat50_with_backtest_session():
    """Test Strat50 with backtest session integration"""
    strat, mock_bm = create_test_strat50()
    
    # Add backtest session
    session = BacktestSession("TEST_001")
    mock_bm.backtest_session = session
    
    timestamp = datetime.now()
    last_close = 50100.0
    
    # Process a price tick
    strat._process_single_last_close(last_close, timestamp)
    
    # Should process normally
    assert strat.last_close == last_close
    assert mock_bm.current_timestamp == timestamp
    mock_bm.check_and_fill_orders.assert_called_once_with("BTCUSDT", last_close, timestamp)


def test_position_snapshot_recording():
    """Test position snapshot recording"""
    strat, mock_bm = create_test_strat50()
    
    # Add backtest session
    session = BacktestSession("TEST_002")
    mock_bm.backtest_session = session
    
    # Set up a non-empty position
    mock_long_pos = mock_bm.position[Direction.LONG]
    mock_long_pos.is_empty.return_value = False
    mock_long_pos.size = 0.001
    mock_long_pos.entry_price = 49500.0
    mock_long_pos.get_margin.return_value = 50.0
    mock_long_pos.liq_price = 45000.0
    
    timestamp = datetime.now()
    last_close = 50000.0
    
    # Process price tick
    strat._process_single_last_close(last_close, timestamp)
    
    # Should record position snapshot
    assert len(session.position_snapshots) == 1
    snapshot = session.position_snapshots[0]
    assert snapshot.symbol == "BTCUSDT"
    assert snapshot.direction == "long"
    assert snapshot.size == 0.001
    assert snapshot.entry_price == 49500.0


def test_unrealized_pnl_calculation():
    """Test unrealized PnL calculation"""
    strat, mock_bm = create_test_strat50()
    
    # Mock position with direction attribute
    mock_pos = Mock()
    mock_pos.is_empty.return_value = False
    mock_pos.size = 0.001
    mock_pos.entry_price = 49000.0
    mock_pos._Position__direction = Direction.LONG  # Private attribute
    
    current_price = 50000.0
    
    # Calculate PnL
    pnl = strat._calculate_unrealized_pnl(mock_pos, current_price)
    
    # Long position: (current - entry) * size = (50000 - 49000) * 0.001 = 1.0
    assert pnl == 1.0


def test_multiple_price_ticks():
    """Test processing multiple price ticks"""
    strat, mock_bm = create_test_strat50()
    
    session = BacktestSession("TEST_003")
    mock_bm.backtest_session = session
    
    prices = [49900.0, 50000.0, 50100.0, 49800.0]
    
    for i, price in enumerate(prices):
        timestamp = datetime.now()
        strat._process_single_last_close(price, timestamp)
        
        # Each call should update the current price
        assert strat.last_close == price
        
        # Order fill checking should be called each time
        assert mock_bm.check_and_fill_orders.call_count == i + 1


def test_greed_building():
    """Test that greed is built correctly"""
    strat, mock_bm = create_test_strat50()
    
    # Initially greed should be empty or minimal
    assert len(strat.greed.greed) <= 1
    
    # Process a price tick
    strat._process_single_last_close(50000.0, datetime.now())
    
    # Greed should be built
    assert len(strat.greed.greed) > 1
    assert abs(len(strat.greed.greed) - strat.greed.greed_count) <= 1


if __name__ == "__main__":
    print("Testing enhanced Strat50...")
    
    test_strat50_basic_processing()
    print("✅ Basic processing test passed")
    
    test_strat50_with_backtest_session()
    print("✅ Backtest session integration test passed")
    
    test_position_snapshot_recording()
    print("✅ Position snapshot recording test passed")
    
    test_unrealized_pnl_calculation()
    print("✅ Unrealized PnL calculation test passed")
    
    test_multiple_price_ticks()
    print("✅ Multiple price ticks test passed")
    
    test_greed_building()
    print("✅ Greed building test passed")
    
    print("\n🎉 All enhanced Strat50 tests passed!")
