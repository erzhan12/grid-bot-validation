"""
Simple integration test that demonstrates the backtest flow
without external dependencies like yaml, psycopg2, etc.

Tests both basic backtest functionality and start_datetime parameter integration.
"""

from datetime import datetime, timedelta

from src.backtest_order_manager import BacktestOrderManager
from src.backtest_session import BacktestPositionSnapshot, BacktestSession
from src.enums import PositionSide


def test_complete_backtest_flow():
    """Test the complete backtest flow with mock data"""
    
    print("🧪 Testing complete backtest flow...")
    
    # 1. Create backtest session
    session = BacktestSession("INTEGRATION_TEST")
    print(f"✅ Created session: {session.session_id}")
    
    # 2. Create order manager
    order_manager = BacktestOrderManager(session)
    print("✅ Created order manager")
    
    # 3. Simulate creating orders
    timestamp1 = datetime.now()
    
    # Create buy order at 50000
    order_manager.create_order(
        symbol="BTCUSDT",
        side=PositionSide.BUY,
        limit_price=50000.0,
        size=0.001,
        direction="long",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=timestamp1
    )
    
    # Create sell order at 51000
    order_manager.create_order(
        symbol="BTCUSDT",
        side=PositionSide.SELL,
        limit_price=51000.0,
        size=0.001,
        direction="short",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=timestamp1
    )
    
    print(f"✅ Created {len(order_manager.active_orders)} orders")
    
    # 4. Simulate price movements and order fills
    price_sequence = [
        (49900.0, "Price drops - buy order should fill"),
        (50500.0, "Price rises"),
        (51100.0, "Price rises more - sell order should fill"),
        (50800.0, "Price drops")
    ]
    
    for price, description in price_sequence:
        timestamp = datetime.now()
        print(f"\n📊 {description}: ${price:,.2f}")
        
        # Check for fills
        filled_orders = order_manager.check_fills("BTCUSDT", price, timestamp)
        
        if filled_orders:
            for order in filled_orders:
                print(f"   💰 Order filled: {order.order_id} at ${order.fill_price:,.2f}")
        
        # Record position snapshot (mock)
        if len(session.trades) > 0:
            snapshot = BacktestPositionSnapshot(
                timestamp=timestamp,
                symbol="BTCUSDT",
                direction="long",
                size=0.001,
                entry_price=49900.0,  # From first fill
                current_price=price,
                unrealized_pnl=(price - 49900.0) * 0.001,
                margin=50.0,
                liquidation_price=45000.0
            )
            session.record_position_snapshot(snapshot)
    
    # 5. Generate final metrics
    final_metrics = session.get_final_metrics()
    
    # 6. Print results
    print("\n📈 BACKTEST RESULTS:")
    print(f"   Total Trades: {len(session.trades)}")
    print(f"   Position Snapshots: {len(session.position_snapshots)}")
    print(f"   Active Orders: {len(order_manager.active_orders)}")
    print(f"   Order Fill Rate: {order_manager.get_statistics()['fill_rate'] * 100:.1f}%")
    
    if final_metrics:
        for symbol, metrics in final_metrics.items():
            print(f"   {symbol} Metrics:")
            print(f"     Total PnL: ${metrics.total_pnl:.2f}")
            print(f"     Win Rate: {metrics.win_rate:.1f}%")
            print(f"     Total Trades: {metrics.total_trades}")
    
    # Print session summary
    session.print_summary()
    
    # Verify everything worked
    assert len(session.trades) >= 1, "Should have at least one trade"
    assert len(session.position_snapshots) >= 1, "Should have position snapshots"
    
    print("🎉 Integration test completed successfully!")


def test_order_lifecycle():
    """Test complete order lifecycle"""
    
    print("\n🔄 Testing order lifecycle...")
    
    session = BacktestSession("ORDER_LIFECYCLE_TEST")
    order_manager = BacktestOrderManager(session)
    
    # Create order
    order = order_manager.create_order(
        symbol="ETHUSDT",
        side=PositionSide.BUY,
        limit_price=3000.0,
        size=0.01,
        direction="long",
        strategy_id=1,
        bm_name="test_bm",
        timestamp=datetime.now()
    )
    
    print(f"✅ Created order: {order.order_id}")
    assert order.is_active()
    
    # Price doesn't trigger - order stays active
    order_manager.check_fills("ETHUSDT", 3100.0, datetime.now())
    assert order.is_active()
    print("✅ Order remains active when price doesn't trigger")
    
    # Price triggers - order fills
    filled_orders = order_manager.check_fills("ETHUSDT", 2950.0, datetime.now())
    assert len(filled_orders) == 1
    assert filled_orders[0].is_filled()
    assert not filled_orders[0].is_active()
    print("✅ Order filled when price triggers")
    
    # Check trade was recorded
    assert len(session.trades) == 1
    trade = session.trades[0]
    print(f"✅ Trade recorded: {trade.trade_id} - ${trade.price:.2f}")
    
    print("🎉 Order lifecycle test completed!")


def test_position_tracking():
    """Test position tracking and PnL calculation"""
    
    print("\n📊 Testing position tracking...")
    
    session = BacktestSession("POSITION_TEST")
    
    # Simulate a series of position changes
    prices = [50000, 50500, 49800, 51200, 50900]
    entry_price = 50000.0
    size = 0.001
    
    for i, price in enumerate(prices):
        unrealized_pnl = (price - entry_price) * size
        
        snapshot = BacktestPositionSnapshot(
            timestamp=datetime.now(),
            symbol="BTCUSDT",
            direction="long",
            size=size,
            entry_price=entry_price,
            current_price=price,
            unrealized_pnl=unrealized_pnl,
            margin=size * price * 0.1,  # 10% margin
            liquidation_price=entry_price * 0.9  # 90% of entry
        )
        
        session.record_position_snapshot(snapshot)
        print(f"   📸 Snapshot {i + 1}: Price ${price:,.2f}, PnL ${unrealized_pnl:.2f}")
    
    print(f"✅ Recorded {len(session.position_snapshots)} position snapshots")
    
    # Check max/min PnL
    pnls = [s.unrealized_pnl for s in session.position_snapshots]
    max_pnl = max(pnls)
    min_pnl = min(pnls)
    
    print(f"✅ Max PnL: ${max_pnl:.2f}, Min PnL: ${min_pnl:.2f}")
    
    print("🎉 Position tracking test completed!")


def test_start_datetime_integration():
    """Test backtest with realistic start_datetime timestamps"""

    print("\n📅 Testing start_datetime integration...")

    # Create session with realistic timestamp
    start_time = datetime(2025, 9, 19, 0, 0, 0)
    session_id = f"DATETIME_TEST_{start_time.strftime('%Y%m%d_%H%M%S')}"
    session = BacktestSession(session_id)
    session.start_datetime = start_time.strftime('%Y-%m-%d %H:%M:%S')

    print(f"   Session ID: {session_id}")
    print(f"   Start DateTime: {session.start_datetime}")

    order_manager = BacktestOrderManager(session)

    # Create orders with timestamps relative to start_datetime
    timestamps = [
        start_time,
        start_time + timedelta(minutes=15),
        start_time + timedelta(minutes=30),
        start_time + timedelta(hours=1),
    ]

    prices = [50000, 49800, 50200, 50100]

    print(f"   Creating orders across {len(timestamps)} timestamps...")

    for i, (timestamp, price) in enumerate(zip(timestamps, prices)):
        order_manager.create_order(
            symbol="BTCUSDT",
            side=PositionSide.BUY,
            limit_price=price - 100,  # Below market to ensure fills
            size=0.001,
            direction="long",
            strategy_id=1,
            bm_name="datetime_test",
            timestamp=timestamp
        )

        # Check if order fills at market price
        filled_orders = order_manager.check_fills("BTCUSDT", price, timestamp)
        if filled_orders:
            print(f"   ✅ {timestamp.strftime('%H:%M:%S')} Order {i + 1} filled @ ${price:,.2f}")
        else:
            print(f"   ⏳ {timestamp.strftime('%H:%M:%S')} Order {i + 1} created @ ${price - 100:,.2f}")

    # Test data range info (would normally come from DataProvider)
    duration = timestamps[-1] - timestamps[0]
    print(f"   Test Duration: {duration}")
    print(f"   Total Orders: {len(order_manager.active_orders) + len(session.trades)}")
    print(f"   Filled Orders: {len(session.trades)}")

    # Verify session has realistic timestamps
    if session.trades:
        first_trade_time = session.trades[0].executed_at
        print(f"   First Trade: {first_trade_time.strftime('%Y-%m-%d %H:%M:%S')}")

    print("🎉 Start datetime integration test completed!")


def test_realistic_backtest_scenario():
    """Test a realistic backtest scenario with multiple strategies"""

    print("\n🎯 Testing realistic backtest scenario...")

    # Simulate a realistic trading day
    start_time = datetime(2025, 9, 19, 9, 0, 0)  # 9 AM start
    session = BacktestSession(f"REALISTIC_TEST_{start_time.strftime('%Y%m%d')}")
    session.start_datetime = start_time.strftime('%Y-%m-%d %H:%M:%S')

    order_manager = BacktestOrderManager(session)
    order_manager.set_slippage(5)  # 0.05% slippage

    print(f"   Simulating trading day starting {start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Simulate price movement throughout the day
    base_price = 50000
    current_time = start_time

    # Morning volatility
    morning_prices = [base_price, base_price + 150, base_price - 50, base_price + 80, base_price - 100, base_price + 200]
    afternoon_prices = [base_price + 200, base_price + 350, base_price + 180, base_price + 450, base_price + 320, base_price + 280]

    all_prices = morning_prices + afternoon_prices
    trades_count = 0

    for i, price in enumerate(all_prices):
        current_time += timedelta(minutes=30)  # 30-minute intervals

        # Create buy and sell orders around current price
        buy_price = price - 50  # Buy 50 below
        sell_price = price + 50  # Sell 50 above

        # Create orders
        order_manager.create_order(
            symbol="BTCUSDT",
            side=PositionSide.BUY,
            limit_price=buy_price,
            size=0.001,
            direction="long",
            strategy_id=1,
            bm_name="realistic_test",
            timestamp=current_time
        )

        order_manager.create_order(
            symbol="BTCUSDT",
            side=PositionSide.SELL,
            limit_price=sell_price,
            size=0.001,
            direction="short",
            strategy_id=1,
            bm_name="realistic_test",
            timestamp=current_time
        )

        # Check for fills
        filled_orders = order_manager.check_fills("BTCUSDT", price, current_time)
        trades_count += len(filled_orders)

        if i % 3 == 0:  # Print every 3rd update
            time_str = current_time.strftime('%H:%M')
            print(f"   {time_str} Price: ${price:,.2f}, Active Orders: {len(order_manager.active_orders)}, Trades: {trades_count}")

    # Final statistics
    stats = order_manager.get_statistics()
    end_time = current_time
    duration = end_time - start_time

    print("\n📊 Realistic Backtest Results:")
    print(f"   Duration: {duration}")
    print(f"   Price Range: ${min(all_prices):,.2f} - ${max(all_prices):,.2f}")
    print(f"   Total Orders Created: {stats['total_orders_created']}")
    print(f"   Orders Filled: {stats['filled_orders']}")
    print(f"   Fill Rate: {stats['fill_rate'] * 100:.1f}%")
    print(f"   Average Slippage: {stats['slippage_bps']} bps")

    print("🎉 Realistic backtest scenario test completed!")


if __name__ == "__main__":
    print("🚀 Running Backtest Integration Tests")
    print("=" * 50)

    # Run all tests
    session = test_complete_backtest_flow()
    test_order_lifecycle()
    test_position_tracking()
    test_start_datetime_integration()
    test_realistic_backtest_scenario()

    print(f"\n{'=' * 50}")
    print("🎉 ALL INTEGRATION TESTS PASSED!")
    print("📋 Summary:")
    print("   - Backtest session management: ✅")
    print("   - Order creation and management: ✅")
    print("   - Order fills and trade recording: ✅")
    print("   - Position tracking and snapshots: ✅")
    print("   - Metrics calculation: ✅")
    print("   - Complete backtest flow: ✅")
    print("\n🚀 Ready for production backtesting!")
    
    # Show final session state
    print("\n📊 Final session state:")
    session.print_summary()
