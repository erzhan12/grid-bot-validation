"""
Test Position Management Implementation

Tests the enhanced position tracking with average price calculation,
PnL management, and realistic order fill processing.
"""

from datetime import datetime
from unittest.mock import Mock

from src.backtest_order_manager import BacktestOrderManager
from src.backtest_session import BacktestSession
from src.constants import COMMISSION_RATE
from src.enums import Direction, PositionSide
from src.position_tracker import PositionManager, PositionTracker


def test_position_tracker_basic():
    """Test basic position tracker functionality"""
    print("🧪 Testing basic position tracker...")
    
    tracker = PositionTracker(Direction.LONG, commission_rate=COMMISSION_RATE)
    
    # Test empty position
    assert tracker.is_empty()
    assert tracker.calculate_unrealized_pnl(50000) == 0.0
    
    # Add first position
    realized_pnl = tracker.add_position(0.1, 50000.0, datetime.now(), "ORDER_001")
    
    assert tracker.state.total_size == 0.1
    assert tracker.state.average_entry_price == 50000.0
    assert realized_pnl < 0  # Commission cost
    assert not tracker.is_empty()
    
    print("✅ Basic position tracker test passed")


def test_average_price_calculation():
    """Test average price calculation with multiple entries"""
    print("🧪 Testing average price calculation...")
    
    tracker = PositionTracker(Direction.LONG)
    
    # First entry: 0.1 BTC at $50,000
    tracker.add_position(0.1, 50000.0, datetime.now(), "ORDER_001")
    assert tracker.state.average_entry_price == 50000.0
    
    # Second entry: 0.1 BTC at $52,000
    tracker.add_position(0.1, 52000.0, datetime.now(), "ORDER_002")
    
    # Average should be $51,000
    expected_avg = (0.1 * 50000 + 0.1 * 52000) / 0.2
    assert abs(tracker.state.average_entry_price - expected_avg) < 0.01
    assert tracker.state.total_size == 0.2
    
    # Third entry: 0.05 BTC at $48,000
    tracker.add_position(0.05, 48000.0, datetime.now(), "ORDER_003")
    
    # New average: (0.2 * 51000 + 0.05 * 48000) / 0.25
    expected_avg = (0.2 * 51000 + 0.05 * 48000) / 0.25
    assert abs(tracker.state.average_entry_price - expected_avg) < 0.01
    assert tracker.state.total_size == 0.25
    
    print(f"✅ Average price calculation test passed: ${tracker.state.average_entry_price:.2f}")


def test_pnl_calculations():
    """Test PnL calculations for long and short positions"""
    print("🧪 Testing PnL calculations...")
    
    # Test long position
    long_tracker = PositionTracker(Direction.LONG)
    long_tracker.add_position(0.1, 50000.0, datetime.now(), "LONG_001")
    
    # Price goes up - should be profitable
    unrealized_pnl = long_tracker.calculate_unrealized_pnl(55000.0)
    expected_pnl = (55000 - 50000) * 0.1  # $500
    assert abs(unrealized_pnl - expected_pnl) < 0.01
    
    # Price goes down - should be loss
    unrealized_pnl = long_tracker.calculate_unrealized_pnl(45000.0)
    expected_pnl = (45000 - 50000) * 0.1  # -$500
    assert abs(unrealized_pnl - expected_pnl) < 0.01
    
    # Test short position
    short_tracker = PositionTracker(Direction.SHORT)
    short_tracker.add_position(0.1, 50000.0, datetime.now(), "SHORT_001")
    
    # Price goes down - should be profitable for short
    unrealized_pnl = short_tracker.calculate_unrealized_pnl(45000.0)
    expected_pnl = (50000 - 45000) * 0.1  # $500
    assert abs(unrealized_pnl - expected_pnl) < 0.01
    
    # Price goes up - should be loss for short
    unrealized_pnl = short_tracker.calculate_unrealized_pnl(55000.0)
    expected_pnl = (50000 - 55000) * 0.1  # -$500
    assert abs(unrealized_pnl - expected_pnl) < 0.01
    
    print("✅ PnL calculations test passed")


def test_position_reduction_and_realized_pnl():
    """Test position reduction with realized PnL calculation"""
    print("🧪 Testing position reduction and realized PnL...")
    
    tracker = PositionTracker(Direction.LONG, commission_rate=COMMISSION_RATE)
    
    # Open position: 0.2 BTC at $50,000
    tracker.add_position(0.2, 50000.0, datetime.now(), "OPEN_001")
    
    # Close half at $55,000 (profitable)
    realized_pnl = tracker.reduce_position(0.1, 55000.0, datetime.now(), "CLOSE_001")
    
    # Expected: (55000 - 50000) * 0.1 - commission
    expected_gross_pnl = (55000 - 50000) * 0.1  # $500
    commission = 0.1 * 55000 * COMMISSION_RATE  # Use actual commission rate
    expected_net_pnl = expected_gross_pnl - commission
    
    assert abs(realized_pnl - expected_net_pnl) < 0.01
    assert tracker.state.total_size == 0.1
    assert tracker.state.average_entry_price == 50000.0  # Should remain same
    
    # Close remaining at $45,000 (loss)
    realized_pnl_2 = tracker.reduce_position(0.1, 45000.0, datetime.now(), "CLOSE_002")
    
    expected_gross_pnl_2 = (45000 - 50000) * 0.1  # -$500
    commission_2 = 0.1 * 45000 * COMMISSION_RATE  # Use actual commission rate
    expected_net_pnl_2 = expected_gross_pnl_2 - commission_2
    
    assert abs(realized_pnl_2 - expected_net_pnl_2) < 0.01
    assert tracker.is_empty()
    
    # Total realized PnL should be sum of both trades PLUS opening commission
    total_realized = tracker.state.realized_pnl

    # Calculate opening commission (now included in realized_pnl)
    opening_commission = 0.2 * 50000 * COMMISSION_RATE

    # Expected total includes opening commission + both closing trades
    expected_total = -opening_commission + expected_net_pnl + expected_net_pnl_2
    assert abs(total_realized - expected_total) < 0.01
    
    print(f"✅ Position reduction test passed: Total realized PnL = ${total_realized:.2f}")


def test_position_manager():
    """Test position manager with both long and short positions"""
    print("🧪 Testing position manager...")
    
    manager = PositionManager(commission_rate=COMMISSION_RATE)
    
    # Open long position
    long_tracker = manager.get_tracker(Direction.LONG)
    long_tracker.add_position(0.1, 50000.0, datetime.now(), "LONG_001")
    
    # Open short position
    short_tracker = manager.get_tracker(Direction.SHORT)
    short_tracker.add_position(0.05, 51000.0, datetime.now(), "SHORT_001")
    
    # Get combined PnL at $52,000
    combined_pnl = manager.get_combined_pnl(52000.0)
    
    # Long should be profitable: (52000 - 50000) * 0.1 = $200
    # Short should be at loss: (51000 - 52000) * 0.05 = -$50
    # Net unrealized: $200 - $50 = $150
    
    assert combined_pnl['long_position']['size'] == 0.1
    assert combined_pnl['short_position']['size'] == 0.05
    assert combined_pnl['net_position_size'] == 0.05  # 0.1 - 0.05
    
    long_pnl = combined_pnl['long_position']['unrealized_pnl']
    short_pnl = combined_pnl['short_position']['unrealized_pnl']
    
    assert abs(long_pnl - 200.0) < 0.01
    assert abs(short_pnl - (-50.0)) < 0.01
    assert abs(combined_pnl['total_unrealized_pnl'] - 150.0) < 0.01
    
    print(f"✅ Position manager test passed: Net unrealized PnL = ${combined_pnl['total_unrealized_pnl']:.2f}")


def test_integrated_backtest_with_position_tracking():
    """Test integrated backtest with realistic position tracking"""
    print("🧪 Testing integrated backtest with position tracking...")
    
    # Create backtest session
    session = BacktestSession("POSITION_TEST")
    order_manager = BacktestOrderManager(session)
    
    # Create mock position objects
    long_position = Mock()
    long_position._Position__direction = Direction.LONG
    
    short_position = Mock()
    short_position._Position__direction = Direction.SHORT
    
    # Simulate order fills and position updates
    timestamp = datetime.now()
    
    # 1. Buy order fills - should increase long position
    order_manager.create_order(
        symbol="BTCUSDT",
        side=PositionSide.BUY,
        limit_price=50000.0,
        size=0.1,
        direction="long",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=timestamp
    )
    
    # Simulate order fill
    filled_orders = order_manager.check_fills("BTCUSDT", 49900.0, timestamp)
    assert len(filled_orders) == 1
    
    # 2. Simulate position tracking (this would normally be done by BybitApiUsdt)
    from src.position_tracker import PositionTracker
    
    # Initialize tracker for the position
    long_position.tracker = PositionTracker(Direction.LONG)
    
    # Process the fill
    filled_order = filled_orders[0]
    realized_pnl = long_position.tracker.add_position(
        size=filled_order.size,
        price=filled_order.fill_price,
        timestamp=timestamp,
        order_id=filled_order.order_id
    )
    
    # Update position attributes
    long_position.size = long_position.tracker.state.total_size
    long_position.entry_price = long_position.tracker.state.average_entry_price
    
    # Verify position state
    assert long_position.size == 0.1
    assert abs(long_position.entry_price - filled_order.fill_price) < 0.01
    assert realized_pnl < 0  # Commission cost
    
    # 3. Price moves and calculate unrealized PnL
    new_price = 51000.0
    unrealized_pnl = long_position.tracker.calculate_unrealized_pnl(new_price)
    
    # Should be profitable: (51000 - fill_price) * 0.1
    # Fill price should be around 50000 (with slippage it might be slightly different)
    print(f"   Fill price: ${filled_order.fill_price:.2f}")
    print(f"   New price: ${new_price:.2f}")
    print(f"   Unrealized PnL: ${unrealized_pnl:.2f}")
    
    # Should be profitable (at least some profit)
    assert unrealized_pnl > 0.0
    
    # 4. Get position information
    position_info = long_position.tracker.get_position_info(new_price)
    
    assert position_info['direction'] == 'long'
    assert position_info['size'] == 0.1
    assert position_info['unrealized_pnl'] > 0.0  # Should be profitable
    assert position_info['total_entries'] == 1
    
    print("✅ Integrated test passed:")
    print(f"   Position: {position_info['size']} BTC @ ${position_info['average_entry_price']:.2f}")
    print(f"   Current Price: ${new_price:,.2f}")
    print(f"   Unrealized PnL: ${position_info['unrealized_pnl']:.2f}")
    print(f"   Commission Paid: ${position_info['commission_paid']:.3f}")


def test_complex_trading_scenario():
    """Test complex trading scenario with multiple entries and exits"""
    print("🧪 Testing complex trading scenario...")
    
    tracker = PositionTracker(Direction.LONG, commission_rate=COMMISSION_RATE)
    
    # Trading scenario:
    # 1. Buy 0.1 BTC at $50,000
    # 2. Buy 0.05 BTC at $49,000 (DCA down)
    # 3. Sell 0.075 BTC at $52,000 (partial profit)
    # 4. Buy 0.025 BTC at $48,000 (DCA down again)
    # 5. Sell all remaining at $53,000
    
    timestamp = datetime.now()
    
    # Step 1: Initial buy
    tracker.add_position(0.1, 50000.0, timestamp, "BUY_1")
    assert tracker.state.total_size == 0.1
    assert tracker.state.average_entry_price == 50000.0
    
    # Step 2: DCA down
    tracker.add_position(0.05, 49000.0, timestamp, "BUY_2")
    expected_avg = (0.1 * 50000 + 0.05 * 49000) / 0.15
    assert abs(tracker.state.average_entry_price - expected_avg) < 0.01
    assert abs(tracker.state.total_size - 0.15) < 0.000001  # Check with precision tolerance
    
    # Step 3: Partial sell at profit
    tracker.reduce_position(0.075, 52000.0, timestamp, "SELL_1")
    # Average entry price should remain the same
    assert abs(tracker.state.average_entry_price - expected_avg) < 0.01
    assert abs(tracker.state.total_size - 0.075) < 0.000001
    
    # Step 4: DCA down again
    tracker.add_position(0.025, 48000.0, timestamp, "BUY_3")
    new_expected_avg = (0.075 * expected_avg + 0.025 * 48000) / 0.1
    assert abs(tracker.state.average_entry_price - new_expected_avg) < 0.01
    assert abs(tracker.state.total_size - 0.1) < 0.000001
    
    # Step 5: Sell all remaining
    print(f"   Before final sell: Size={tracker.state.total_size}")
    tracker.reduce_position(0.1, 53000.0, timestamp, "SELL_2")
    print(f"   After final sell: Size={tracker.state.total_size}")
    assert abs(tracker.state.total_size) < 0.000001  # Check if effectively empty
    
    # Calculate expected total PnL manually
    # Entry 1: 0.1 * 50000 = 5000
    # Entry 2: 0.05 * 49000 = 2450
    # Exit 1: 0.075 * 52000 = 3900 (partial, from avg price ~49666.67)
    # Entry 3: 0.025 * 48000 = 1200
    # Exit 2: 0.1 * 53000 = 5300 (from new avg price)
    
    total_realized = tracker.state.realized_pnl
    print(f"   Total realized PnL: ${total_realized:.2f}")
    print(f"   Total commission: ${tracker.state.commission_paid:.3f}")
    print(f"   Entry count: {len([e for e in tracker.state.entries if e.is_increase])}")
    print(f"   Exit count: {len([e for e in tracker.state.entries if not e.is_increase])}")
    
    # Should be profitable (rough check)
    assert total_realized > 0, f"Expected profit but got ${total_realized:.2f}"
    
    print("✅ Complex trading scenario test passed")


if __name__ == "__main__":
    print("🚀 Running Position Management Tests")
    print("=" * 50)
    
    test_position_tracker_basic()
    test_average_price_calculation()
    test_pnl_calculations()
    test_position_reduction_and_realized_pnl()
    test_position_manager()
    test_integrated_backtest_with_position_tracking()
    test_complex_trading_scenario()
    
    print(f"\n{'=' * 50}")
    print("🎉 ALL POSITION MANAGEMENT TESTS PASSED!")
    print("\n📋 Summary of Capabilities:")
    print("   ✅ Average price calculation with multiple entries")
    print("   ✅ Realistic PnL calculation (long/short)")
    print("   ✅ Position reduction with realized PnL")
    print("   ✅ Commission tracking and deduction")
    print("   ✅ Combined position management (long + short)")
    print("   ✅ Integration with backtest order system")
    print("   ✅ Complex trading scenarios")
    print("\n🎯 Ready for production backtesting with accurate position tracking!")
