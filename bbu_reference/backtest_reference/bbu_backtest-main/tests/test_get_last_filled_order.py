"""Test the get_last_filled_order implementation"""
from datetime import datetime

from src.backtest_order_manager import BacktestOrderManager
from src.backtest_session import BacktestSession
from src.enums import PositionSide


def test_get_last_filled_order_basic():
    """Test that last filled order is tracked correctly"""
    session = BacktestSession("TEST_SESSION")
    manager = BacktestOrderManager(session)
    
    # Initially should be empty
    assert manager.get_last_filled_order("BTCUSDT") is None
    
    # Create and fill first order
    order1 = manager.create_order(
        symbol="BTCUSDT",
        side=PositionSide.BUY,
        limit_price=50000.0,
        size=0.1,
        direction="long",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=datetime(2024, 1, 1, 10, 0, 0)
    )
    
    # Fill the order
    manager.check_fills("BTCUSDT", 50000.0, datetime(2024, 1, 1, 10, 0, 1))
    
    # Should now return the filled order
    last_order = manager.get_last_filled_order("BTCUSDT")
    assert last_order is not None
    assert last_order.order_id == order1.order_id
    assert last_order.fill_price == 50000.0


def test_get_last_filled_order_multiple():
    """Test that most recent order is returned when multiple orders filled"""
    session = BacktestSession("TEST_SESSION")
    manager = BacktestOrderManager(session)
    
    # Create and fill first order
    manager.create_order(
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
    
    # Create and fill second order (should replace first)
    order2 = manager.create_order(
        symbol="BTCUSDT",
        side=PositionSide.SELL,
        limit_price=51000.0,
        size=0.1,
        direction="short",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=datetime(2024, 1, 1, 11, 0, 0)
    )
    manager.check_fills("BTCUSDT", 51000.0, datetime(2024, 1, 1, 11, 0, 1))
    
    # Should now return the second order (most recent)
    last_order = manager.get_last_filled_order("BTCUSDT")
    assert last_order is not None
    assert last_order.order_id == order2.order_id
    assert last_order.fill_price == 51000.0


def test_get_last_filled_order_different_symbols():
    """Test that different symbols are tracked separately"""
    session = BacktestSession("TEST_SESSION")
    manager = BacktestOrderManager(session)
    
    # Fill order for BTCUSDT
    order1 = manager.create_order(
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
    
    # Different symbol should still be None
    assert manager.get_last_filled_order("ETHUSDT") is None
    
    # BTCUSDT should have the order
    assert manager.get_last_filled_order("BTCUSDT").order_id == order1.order_id
