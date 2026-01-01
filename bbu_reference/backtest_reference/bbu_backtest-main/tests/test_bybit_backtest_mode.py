"""Tests for BybitApiUsdt backtest mode"""

from datetime import datetime
from unittest.mock import Mock

from src.backtest_session import BacktestSession
from src.bybit_api_usdt import BybitApiUsdt


def create_mock_strat():
    """Create a mock strategy for testing"""
    strat = Mock()
    strat.id = 1
    strat.long_koef = 1.0
    strat.liq_ratio = {'min': 0.8, 'max': 1.2}
    strat.max_margin = 5
    strat.min_total_margin = 0
    return strat


def test_backtest_mode_initialization():
    """Test initializing backtest mode"""
    controller = Mock()
    strat = create_mock_strat()
    
    # Create BybitApiUsdt without API credentials (for backtest)
    bm = BybitApiUsdt(None, None, 0.001, strat, "test_bm", controller)
    
    # assert not bm.backtest_mode
    assert bm.backtest_session is None
    assert bm.backtest_order_manager is None
    
    # Initialize backtest mode
    session = BacktestSession("TEST_001")
    bm.init_backtest_mode(session)
    
    # assert bm.backtest_mode
    assert bm.backtest_session == session
    assert bm.backtest_order_manager is not None


def test_backtest_order_creation():
    """Test creating orders in backtest mode"""
    controller = Mock()
    controller.get_limit_orders.return_value = []  # Mock returns empty list
    strat = create_mock_strat()
    
    bm = BybitApiUsdt(None, None, 0.001, strat, "test_bm", controller)
    session = BacktestSession("TEST_002")
    bm.init_backtest_mode(session)
    bm.current_timestamp = datetime.now()
    
    # Initialize positions
    bm.init_positions(strat)
    
    # Create an order
    price, order_id = bm.new_limit_order("Buy", "BTCUSDT", 50000.0, "test_bm", "long", 0.001)
    
    assert price == 50000.0
    assert order_id is not None
    assert len(bm.backtest_order_manager.active_orders) == 1


def test_order_fill_processing():
    """Test order fill processing in backtest mode"""
    controller = Mock()
    controller.get_limit_orders.return_value = []  # Mock returns empty list
    strat = create_mock_strat()
    
    bm = BybitApiUsdt(None, None, 0.001, strat, "test_bm", controller)
    session = BacktestSession("TEST_003")
    bm.init_backtest_mode(session)
    bm.current_timestamp = datetime.now()
    
    # Initialize positions
    bm.init_positions(strat)
    
    # Create a buy order at 50000
    price, order_id = bm.new_limit_order("Buy", "BTCUSDT", 50000.0, "test_bm", "long", 0.001)
    
    # Check that order was created
    assert len(bm.backtest_order_manager.active_orders) == 1
    
    # Price drops to 49900 - should trigger fill
    bm.check_and_fill_orders("BTCUSDT", 49900.0, datetime.now())
    
    # Order should be filled and removed from active orders
    assert len(bm.backtest_order_manager.active_orders) == 0
    assert len(session.trades) == 1
    
    # Check trade details
    trade = session.trades[0]
    assert trade.symbol == "BTCUSDT"
    assert trade.side == "Buy"
    assert trade.size == 0.001
    assert trade.direction == "long"


def test_multiple_orders_fill():
    """Test multiple orders with different symbols"""
    controller = Mock()
    controller.get_limit_orders.return_value = []  # Mock returns empty list
    strat = create_mock_strat()
    
    bm = BybitApiUsdt(None, None, 0.001, strat, "test_bm", controller)
    session = BacktestSession("TEST_004")
    bm.init_backtest_mode(session)
    bm.current_timestamp = datetime.now()
    
    # Initialize positions
    bm.init_positions(strat)
    
    # Create orders for different symbols
    btc_price, btc_order_id = bm.new_limit_order("Buy", "BTCUSDT", 50000.0, "test_bm", "long", 0.001)
    eth_price, eth_order_id = bm.new_limit_order("Buy", "ETHUSDT", 3000.0, "test_bm", "long", 0.01)
    
    assert len(bm.backtest_order_manager.active_orders) == 2
    
    # Only BTC price triggers
    bm.check_and_fill_orders("BTCUSDT", 49900.0, datetime.now())
    
    # Only BTC order should be filled
    assert len(bm.backtest_order_manager.active_orders) == 1
    assert len(session.trades) == 1
    
    # ETH order should still be active
    eth_orders = [o for o in bm.backtest_order_manager.active_orders.values() if o.symbol == "ETHUSDT"]
    assert len(eth_orders) == 1


def test_position_ratio_calculation():
    """Test position ratio handling in backtest mode"""
    controller = Mock()
    strat = create_mock_strat()
    
    bm = BybitApiUsdt(None, None, 0.001, strat, "test_bm", controller)
    session = BacktestSession("TEST_005")
    bm.init_backtest_mode(session)
    bm.current_timestamp = datetime.now()
    
    # Initialize positions
    bm.init_positions(strat)
    
    # Test position ratio check (simplified)
    bm.position_ratio = 1.5
    bm.update_position_ratio()
    
    assert bm.position[bm.position.keys().__iter__().__next__()].position_ratio == 1.5


def test_live_mode_fallback():
    """Test that live mode still works when backtest mode is not initialized"""
    controller = Mock()
    strat = create_mock_strat()
    
    # Create with API credentials (simulating live mode)
    bm = BybitApiUsdt("test_key", "test_secret", 0.001, strat, "test_bm", controller)
    
    # Initialize positions
    bm.init_positions(strat)
    
    # Should not be in backtest mode
    # assert not bm.backtest_mode
    
    # Calling backtest methods should not error but also not do anything
    bm.check_and_fill_orders("BTCUSDT", 50000.0, datetime.now())  # Should not crash


if __name__ == "__main__":
    print("Testing BybitApiUsdt backtest mode...")
    
    test_backtest_mode_initialization()
    print("✅ Backtest mode initialization test passed")
    
    test_backtest_order_creation()
    print("✅ Backtest order creation test passed")
    
    test_order_fill_processing()
    print("✅ Order fill processing test passed")
    
    test_multiple_orders_fill()
    print("✅ Multiple orders fill test passed")
    
    test_position_ratio_calculation()
    print("✅ Position ratio calculation test passed")
    
    test_live_mode_fallback()
    print("✅ Live mode fallback test passed")
    
    print("\n🎉 All BybitApiUsdt backtest mode tests passed!")
