"""Tests for BacktestOrderManager"""

from datetime import datetime

from src.backtest_order_manager import BacktestOrderManager
from src.backtest_session import BacktestSession
from src.enums import PositionSide


def test_order_creation():
    """Test creating orders with backtest metadata"""
    session = BacktestSession("TEST_ORDER_001")
    manager = BacktestOrderManager(session)
    
    timestamp = datetime.now()
    
    order = manager.create_order(
        symbol="BTCUSDT",
        side=PositionSide.BUY,
        limit_price=50000.0,
        size=0.001,
        direction="long",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=timestamp
    )
    
    assert order.symbol == "BTCUSDT"
    assert order.side == PositionSide.BUY
    assert order.limit_price == 50000.0
    assert order.size == 0.001
    assert order.direction == "long"
    assert order.strategy_id == 1
    assert order.bm_name == "test_bm"
    assert order.created_at == timestamp
    assert len(manager.active_orders) == 1


def test_order_fill_buy():
    """Test filling a buy order"""
    session = BacktestSession("TEST_ORDER_002")
    manager = BacktestOrderManager(session)
    
    # Create a buy order at 50000
    order = manager.create_order(
        symbol="BTCUSDT",
        side=PositionSide.BUY,
        limit_price=50000.0,
        size=0.001,
        direction="long",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=datetime.now()
    )
    
    # Market price drops to 49900 - should fill
    filled_orders = manager.check_fills("BTCUSDT", 49900.0, datetime.now())
    
    assert len(filled_orders) == 1
    assert filled_orders[0].order_id == order.order_id
    assert filled_orders[0].is_filled()
    assert len(manager.active_orders) == 0  # Should be removed from active
    assert len(session.trades) == 1  # Should record trade


def test_order_fill_sell():
    """Test filling a sell order"""
    session = BacktestSession("TEST_ORDER_003")
    manager = BacktestOrderManager(session)
    
    # Create a sell order at 50000
    order = manager.create_order(
        symbol="BTCUSDT",
        side=PositionSide.SELL,
        limit_price=50000.0,
        size=0.001,
        direction="short",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=datetime.now()
    )
    
    # Market price rises to 50100 - should fill
    filled_orders = manager.check_fills("BTCUSDT", 50100.0, datetime.now())
    
    assert len(filled_orders) == 1
    assert filled_orders[0].order_id == order.order_id
    assert filled_orders[0].is_filled()
    assert len(session.trades) == 1


def test_order_no_fill():
    """Test order that shouldn't fill"""
    session = BacktestSession("TEST_ORDER_004")
    manager = BacktestOrderManager(session)
    
    # Create a buy order at 50000
    manager.create_order(
        symbol="BTCUSDT",
        side=PositionSide.BUY,
        limit_price=50000.0,
        size=0.001,
        direction="long",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=datetime.now()
    )
    
    # Market price is 50100 - buy order shouldn't fill
    filled_orders = manager.check_fills("BTCUSDT", 50100.0, datetime.now())
    
    assert len(filled_orders) == 0
    assert len(manager.active_orders) == 1  # Should remain active
    assert len(session.trades) == 0  # No trades


def test_slippage_calculation():
    """Test slippage calculation"""
    session = BacktestSession("TEST_ORDER_005")
    manager = BacktestOrderManager(session)
    manager.set_slippage(10)  # 0.1% slippage
    
    # Create a buy order at 50000
    manager.create_order(
        symbol="BTCUSDT",
        side=PositionSide.BUY,
        limit_price=50000.0,
        size=0.001,
        direction="long",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=datetime.now()
    )
    
    # Market price is 49800 - should fill with slippage
    filled_orders = manager.check_fills("BTCUSDT", 49800.0, datetime.now())
    
    assert len(filled_orders) == 1
    # Fill price should be higher than market price due to slippage
    # but not higher than limit price
    fill_price = filled_orders[0].fill_price
    assert fill_price > 49800.0
    assert fill_price <= 50000.0


def test_order_cancellation():
    """Test order cancellation"""
    session = BacktestSession("TEST_ORDER_006")
    manager = BacktestOrderManager(session)
    
    order = manager.create_order(
        symbol="BTCUSDT",
        side=PositionSide.BUY,
        limit_price=50000.0,
        size=0.001,
        direction="long",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=datetime.now()
    )
    
    assert len(manager.active_orders) == 1
    
    # Cancel the order
    success = manager.cancel_order(order.order_id, datetime.now())
    
    assert success
    assert len(manager.active_orders) == 0
    assert len(manager.order_history) == 1
    assert manager.order_history[0].status.value == "cancelled"


def test_multiple_orders_mixed_symbols():
    """Test handling multiple orders with different symbols"""
    session = BacktestSession("TEST_ORDER_007")
    manager = BacktestOrderManager(session)
    
    # Create orders for different symbols
    btc_order = manager.create_order("BTCUSDT", PositionSide.BUY, 50000.0, 0.001, "long", 1, "bm1", datetime.now())
    manager.create_order("ETHUSDT", PositionSide.BUY, 3000.0, 0.01, "long", 1, "bm1", datetime.now())
    
    assert len(manager.active_orders) == 2
    
    # Only BTC price triggers
    filled_orders = manager.check_fills("BTCUSDT", 49900.0, datetime.now())
    
    assert len(filled_orders) == 1
    assert filled_orders[0].order_id == btc_order.order_id
    assert len(manager.active_orders) == 1  # ETH order still active
    
    # ETH order should still be there
    eth_orders = [o for o in manager.active_orders.values() if o.symbol == "ETHUSDT"]
    assert len(eth_orders) == 1


def test_statistics():
    """Test order statistics"""
    session = BacktestSession("TEST_ORDER_008")
    manager = BacktestOrderManager(session)
    
    # Create some orders
    manager.create_order("BTCUSDT", PositionSide.BUY, 50000.0, 0.001, "long", 1, "bm1", datetime.now())
    order2 = manager.create_order("BTCUSDT", PositionSide.SELL, 51000.0, 0.001, "short", 1, "bm1", datetime.now())
    
    # Fill one order
    manager.check_fills("BTCUSDT", 49900.0, datetime.now())
    
    # Cancel another
    manager.cancel_order(order2.order_id, datetime.now())
    
    stats = manager.get_statistics()
    
    assert stats['total_orders_created'] == 2
    assert stats['active_orders'] == 0
    assert stats['filled_orders'] == 1
    assert stats['cancelled_orders'] == 1
    assert stats['fill_rate'] == 0.5


if __name__ == "__main__":
    # Run tests manually
    print("Testing BacktestOrderManager...")
    
    test_order_creation()
    print("✅ Order creation test passed")
    
    test_order_fill_buy()
    print("✅ Buy order fill test passed")
    
    test_order_fill_sell()
    print("✅ Sell order fill test passed")
    
    test_order_no_fill()
    print("✅ No fill test passed")
    
    test_slippage_calculation()
    print("✅ Slippage calculation test passed")
    
    test_order_cancellation()
    print("✅ Order cancellation test passed")
    
    test_multiple_orders_mixed_symbols()
    print("✅ Multiple orders test passed")
    
    test_statistics()
    print("✅ Statistics test passed")
    
    print("\n🎉 All BacktestOrderManager tests passed!")
