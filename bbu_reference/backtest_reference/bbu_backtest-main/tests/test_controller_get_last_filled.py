"""Test Controller.get_last_filled_order integration"""
from datetime import datetime
from unittest.mock import Mock

from src.backtest_order_manager import BacktestOrderManager
from src.backtest_session import BacktestSession
from src.controller import Controller
from src.enums import PositionSide


def test_controller_get_last_filled_order():
    """Test Controller.get_last_filled_order returns correct format"""
    # Create a mock controller with a mock bm
    controller = Mock(spec=Controller)
    
    # Create real backtest session and order manager
    session = BacktestSession("TEST_SESSION")
    manager = BacktestOrderManager(session)
    
    # Create a mock bm (balance manager)
    mock_bm = Mock()
    mock_bm.strat = 1
    mock_bm.backtest_order_manager = manager
    
    # Set up controller's bms
    controller.bms = [mock_bm]
    
    # Use the real implementation
    controller.get_last_filled_order = Controller.get_last_filled_order.__get__(controller, Controller)
    
    # Initially should return None
    result = controller.get_last_filled_order(1, "BTCUSDT")
    assert result is None
    
    # Create and fill an order
    order = manager.create_order(
        symbol="BTCUSDT",
        side=PositionSide.BUY,
        limit_price=50000.0,
        size=0.1,
        direction="long",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=datetime(2024, 1, 1, 10, 0, 0)
    )
    manager.check_fills("BTCUSDT", 50000.0, datetime(2024, 1, 1, 10, 0, 1))
    
    # Now should return the order in correct format
    result = controller.get_last_filled_order(1, "BTCUSDT")
    assert result is not None
    assert 'execPrice' in result  # Required key for backward compatibility
    assert result['execPrice'] == 50000.0
    assert result['fill_price'] == 50000.0
    assert result['symbol'] == "BTCUSDT"
    assert result['side'] == "Buy"
    assert result['direction'] == "long"
    assert result['size'] == 0.1
    assert result['order_id'] == order.order_id
    
    # Wrong strategy should return None
    result = controller.get_last_filled_order(999, "BTCUSDT")
    assert result is None


def test_controller_get_last_filled_order_no_backtest_mode():
    """Test that method returns None when not in backtest mode"""
    controller = Mock(spec=Controller)
    
    # Create a mock bm without backtest_order_manager
    mock_bm = Mock()
    mock_bm.strat = 1
    mock_bm.backtest_order_manager = None  # Not in backtest mode
    
    controller.bms = [mock_bm]
    controller.get_last_filled_order = Controller.get_last_filled_order.__get__(controller, Controller)
    
    result = controller.get_last_filled_order(1, "BTCUSDT")
    assert result is None
