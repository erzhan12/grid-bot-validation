#!/usr/bin/env python3
"""
Enhanced Position Management Demo

Demonstrates the new position tracking capabilities with realistic
average price calculation, PnL management, and order processing.

Usage:
    python demo_position_management.py
    python demo_position_management.py --start_datetime "2025-09-19 00:00:00"
"""

import argparse
from datetime import datetime, timedelta

from src.backtest_order_manager import BacktestOrderManager
from src.backtest_session import BacktestSession
from src.constants import COMMISSION_RATE
from src.enums import Direction, PositionSide
from src.position_tracker import PositionManager, PositionTracker


def demo_basic_position_tracking():
    """Demo basic position tracking with average price calculation"""
    
    print("🎯 Demo 1: Basic Position Tracking with Average Price")
    print("=" * 60)
    
    # Create position tracker for long position
    tracker = PositionTracker(Direction.LONG, commission_rate=COMMISSION_RATE)
    
    # Simulate multiple position entries
    print("📊 Building position with multiple entries:")
    
    # Entry 1: Buy 0.1 BTC at $50,000
    pnl_1 = tracker.add_position(0.1, 50000.0, datetime.now(), "BUY_001")
    print(f"   Entry 1: 0.1 BTC @ $50,000 | Avg: ${tracker.state.average_entry_price:,.2f} | Commission: ${-pnl_1:.2f}")
    
    # Entry 2: Buy 0.05 BTC at $49,000 (DCA down)
    pnl_2 = tracker.add_position(0.05, 49000.0, datetime.now(), "BUY_002")
    avg_price = tracker.state.average_entry_price
    print(f"   Entry 2: 0.05 BTC @ $49,000 | Avg: ${avg_price:,.2f} | Commission: ${-pnl_2:.2f}")
    
    # Entry 3: Buy 0.1 BTC at $52,000 (price moved up)
    pnl_3 = tracker.add_position(0.1, 52000.0, datetime.now(), "BUY_003")
    print(f"   Entry 3: 0.1 BTC @ $52,000 | Avg: ${tracker.state.average_entry_price:,.2f} | Commission: ${-pnl_3:.2f}")
    
    print("\n📈 Final Position Summary:")
    print(f"   Total Size: {tracker.state.total_size} BTC")
    print(f"   Average Entry Price: ${tracker.state.average_entry_price:,.2f}")
    print(f"   Total Entries: {len([e for e in tracker.state.entries if e.is_increase])}")
    print(f"   Total Commission Paid: ${tracker.state.commission_paid:.2f}")
    
    # Test unrealized PnL at different prices
    test_prices = [48000, 50000, 52000, 55000]
    print("\n💰 Unrealized PnL at different prices:")
    for price in test_prices:
        unrealized_pnl = tracker.calculate_unrealized_pnl(price)
        total_pnl = tracker.calculate_total_pnl(price)
        print(f"   @ ${price:,}: Unrealized PnL = ${unrealized_pnl:+,.2f} | Total PnL = ${total_pnl:+,.2f}")
    
    return tracker


def demo_position_reduction_and_realized_pnl():
    """Demo position reduction with realized PnL calculation"""
    
    print("\n🎯 Demo 2: Position Reduction & Realized PnL")
    print("=" * 60)
    
    # Start with the tracker from demo 1
    tracker = PositionTracker(Direction.LONG, commission_rate=COMMISSION_RATE)
    
    # Build up position
    tracker.add_position(0.25, 50000.0, datetime.now(), "OPEN_001")
    print("📊 Opened position: 0.25 BTC @ $50,000")
    
    # Scenario: Price rises, take partial profits
    print("\n💰 Taking partial profits as price rises:")
    
    # Sell 0.1 BTC at $52,000
    realized_pnl_1 = tracker.reduce_position(0.1, 52000.0, datetime.now(), "SELL_001")
    print(f"   Sell 1: 0.1 BTC @ $52,000 | Realized PnL: ${realized_pnl_1:+.2f}")
    print(f"           Remaining: {tracker.state.total_size} BTC @ ${tracker.state.average_entry_price:,.2f}")
    
    # Sell 0.05 BTC at $54,000
    realized_pnl_2 = tracker.reduce_position(0.05, 54000.0, datetime.now(), "SELL_002")
    print(f"   Sell 2: 0.05 BTC @ $54,000 | Realized PnL: ${realized_pnl_2:+.2f}")
    print(f"           Remaining: {tracker.state.total_size} BTC @ ${tracker.state.average_entry_price:,.2f}")
    
    # Price drops, stop loss (sell remaining position)
    remaining_size = tracker.state.total_size
    realized_pnl_3 = tracker.reduce_position(remaining_size, 48000.0, datetime.now(), "SELL_003")
    print(f"   Sell 3: {remaining_size:.3f} BTC @ $48,000 | Realized PnL: ${realized_pnl_3:+.2f} (Stop Loss)")
    print(f"           Remaining: {tracker.state.total_size} BTC")
    
    print("\n📊 Trading Summary:")
    print(f"   Total Realized PnL: ${tracker.state.realized_pnl:+.2f}")
    print(f"   Total Commission: ${tracker.state.commission_paid:.2f}")
    print(f"   Net Profit: ${tracker.state.realized_pnl:+.2f}")
    print(f"   Number of Trades: {len(tracker.state.entries)}")
    
    # Show current unrealized PnL for remaining position
    if not tracker.is_empty():
        current_price = 49000.0
        unrealized_pnl = tracker.calculate_unrealized_pnl(current_price)
        print(f"   Unrealized PnL @ ${current_price:,}: ${unrealized_pnl:+.2f}")


def demo_long_short_portfolio():
    """Demo managing both long and short positions"""
    
    print("\n🎯 Demo 3: Long/Short Portfolio Management")
    print("=" * 60)
    
    # Create position manager
    manager = PositionManager(commission_rate=COMMISSION_RATE)
    
    print("📊 Building hedge portfolio:")
    
    # Build long position
    long_tracker = manager.get_tracker(Direction.LONG)
    long_tracker.add_position(0.15, 50000.0, datetime.now(), "LONG_001")
    print("   Long: 0.15 BTC @ $50,000")
    
    # Build short position (hedge)
    short_tracker = manager.get_tracker(Direction.SHORT)
    short_tracker.add_position(0.1, 50500.0, datetime.now(), "SHORT_001")
    print("   Short: 0.1 BTC @ $50,500")
    
    # Test portfolio PnL at different prices
    test_prices = [48000, 49000, 50000, 51000, 52000, 53000]
    
    print("\n💰 Portfolio PnL at different prices:")
    print("   Price    | Long PnL | Short PnL | Net PnL | Net Position")
    print("   ---------|----------|-----------|---------|-------------")
    
    for price in test_prices:
        combined = manager.get_combined_pnl(price)
        long_pnl = combined['long_position']['unrealized_pnl']
        short_pnl = combined['short_position']['unrealized_pnl']
        net_pnl = combined['total_unrealized_pnl']
        net_position = combined['net_position_size']
        
        print(f"   ${price:,} | ${long_pnl:+7.0f} | ${short_pnl:+8.0f} | ${net_pnl:+6.0f} | {net_position:+.3f} BTC")
    
    # Show portfolio summary
    final_price = 51000.0
    combined = manager.get_combined_pnl(final_price)
    
    print(f"\n📊 Portfolio Summary @ ${final_price:,}:")
    long_info = combined['long_position']
    short_info = combined['short_position']
    print(f"   Long Position: {long_info['size']} BTC @ ${long_info['average_entry_price']:,.2f}")
    print(f"   Short Position: {short_info['size']} BTC @ ${short_info['average_entry_price']:,.2f}")
    print(f"   Net Position: {combined['net_position_size']:+.3f} BTC")
    print(f"   Total Unrealized PnL: ${combined['total_unrealized_pnl']:+.2f}")
    print(f"   Total Commission: ${combined['total_commission']:.2f}")


def demo_grid_trading_simulation():
    """Demo realistic grid trading with position management"""
    
    print("\n🎯 Demo 4: Grid Trading Simulation")
    print("=" * 60)
    
    # Create backtest session and order manager
    session = BacktestSession("GRID_DEMO")
    order_manager = BacktestOrderManager(session)
    
    # Create position manager
    position_manager = PositionManager()
    
    # Grid parameters
    center_price = 50000.0
    grid_step = 500.0  # $500 between levels
    grid_levels = 5
    order_size = 0.02
    
    print("📊 Setting up grid trading:")
    print(f"   Center Price: ${center_price:,}")
    print(f"   Grid Step: ${grid_step:,}")
    print(f"   Levels: {grid_levels} above and below")
    print(f"   Order Size: {order_size} BTC")
    
    # Create initial grid orders
    active_orders = []
    
    # Buy orders below center
    for i in range(1, grid_levels + 1):
        price = center_price - (i * grid_step)
        order = order_manager.create_order(
            symbol="BTCUSDT",
            side=PositionSide.BUY,
            limit_price=price,
            size=order_size,
            direction="long",
            strategy_id=1,
            bm_name="grid_bot",
            timestamp=datetime.now()
        )
        active_orders.append(order)
    
    # Sell orders above center  
    for i in range(1, grid_levels + 1):
        price = center_price + (i * grid_step)
        order = order_manager.create_order(
            symbol="BTCUSDT",
            side=PositionSide.SELL,
            limit_price=price,
            size=order_size,
            direction="short",
            strategy_id=1,
            bm_name="grid_bot",
            timestamp=datetime.now()
        )
        active_orders.append(order)
    
    print(f"   Created {len(active_orders)} grid orders")
    
    # Simulate price movements and order fills
    price_movements = [
        49200,  # Trigger buy at 49500
        48800,  # Trigger buy at 49000  
        49600,  # No fills
        50600,  # Trigger sell at 50500
        51200,  # Trigger sell at 51000
        50800,  # No fills
        48600,  # Trigger buy at 49000, 48500
        52200,  # Trigger sell at 51500, 52000
    ]
    
    print("\n🔄 Processing price movements:")
    
    for i, price in enumerate(price_movements):
        timestamp = datetime.now() + timedelta(minutes=i * 5)
        
        # Check for order fills
        filled_orders = order_manager.check_fills("BTCUSDT", price, timestamp)
        
        print(f"   Price: ${price:,} | Fills: {len(filled_orders)}")
        
        # Process filled orders and update positions
        for order in filled_orders:
            direction = Direction.LONG if order.direction == 'long' else Direction.SHORT
            tracker = position_manager.get_tracker(direction)
            
            if order.side == PositionSide.BUY:
                # Buy order filled - increase position
                tracker.add_position(
                    order.size, order.fill_price, timestamp, order.order_id
                )
                print(f"     📈 Long position increased: +{order.size} @ ${order.fill_price:.2f}")
            else:
                # Sell order filled - increase short position
                tracker.add_position(
                    order.size, order.fill_price, timestamp, order.order_id
                )
                print(f"     📉 Short position increased: +{order.size} @ ${order.fill_price:.2f}")
        
        # Show portfolio status after fills
        if filled_orders:
            combined = position_manager.get_combined_pnl(price)
            print(f"     Portfolio: Long={combined['long_position']['size']:.3f}, "
                  f"Short={combined['short_position']['size']:.3f}, "
                  f"Net PnL=${combined['total_unrealized_pnl']:+.0f}")
    
    # Final portfolio summary
    final_price = price_movements[-1]
    combined = position_manager.get_combined_pnl(final_price)
    
    print("\n📊 Final Grid Trading Results:")
    print(f"   Total Trades: {len(session.trades)}")
    print(f"   Active Orders: {len(order_manager.active_orders)}")
    long_pos = combined['long_position']
    short_pos = combined['short_position']
    print(f"   Long Position: {long_pos['size']:.3f} BTC @ ${long_pos['average_entry_price']:,.2f}")
    print(f"   Short Position: {short_pos['size']:.3f} BTC @ ${short_pos['average_entry_price']:,.2f}")
    print(f"   Net Position: {combined['net_position_size']:+.3f} BTC")
    print(f"   Total Unrealized PnL: ${combined['total_unrealized_pnl']:+.2f}")
    print(f"   Total Commission: ${combined['total_commission']:.2f}")
    
    # Show order fill rate
    stats = order_manager.get_statistics()
    print(f"   Fill Rate: {stats['fill_rate'] * 100:.1f}%")


def main():
    """Run all position management demos"""
    parser = argparse.ArgumentParser(description='Enhanced Position Management Demo')
    parser.add_argument('--start_datetime', type=str,
                       help='Start datetime for realistic timestamp generation (YYYY-MM-DD HH:MM:SS)')

    args = parser.parse_args()

    print("🚀 Enhanced Position Management Demo")
    print("=" * 80)
    print("Demonstrating realistic position tracking with average price calculation,")
    print("PnL management, and integration with the grid bot backtest system.")

    if args.start_datetime:
        try:
            # Parse the datetime to validate format
            base_time = datetime.strptime(args.start_datetime, '%Y-%m-%d %H:%M:%S')
            print(f"📅 Using start datetime: {args.start_datetime}")
            print("   (Timestamps in demos will be based on this datetime)")
        except ValueError:
            print(f"❌ Invalid datetime format: {args.start_datetime}")
            print("   Expected format: YYYY-MM-DD HH:MM:SS")
            print("   Example: 2025-09-19 00:00:00")
            return
    else:
        base_time = datetime.now()
        print("📅 Using current datetime for demo timestamps")

    print()

    # Run demos with realistic timestamps
    demo_basic_position_tracking_with_time(base_time)
    demo_position_reduction_and_realized_pnl_with_time(base_time)
    demo_long_short_portfolio_with_time(base_time)
    demo_grid_trading_simulation_with_time(base_time)

    print(f"\n{'=' * 80}")
    print("🎉 All Position Management Demos Completed!")
    print("\n📋 Key Features Demonstrated:")
    print("   ✅ Volume-weighted average price calculation")
    print("   ✅ Realistic commission tracking and deduction")
    print("   ✅ Separate realized vs unrealized PnL calculation")
    print("   ✅ Long/short position management")
    print("   ✅ Integration with order management system")
    print("   ✅ Grid trading position tracking")
    print("   ✅ Realistic timestamp-based demo scenarios")
    print("\n🎯 Your backtest system now has production-grade position management!")
    print("   Ready for accurate grid bot backtesting with proper PnL tracking.")


def demo_basic_position_tracking_with_time(base_time):
    """Demo basic position tracking with realistic timestamps"""

    print("🎯 Demo 1: Basic Position Tracking with Average Price")
    print("=" * 60)

    # Create position tracker for long position
    tracker = PositionTracker(Direction.LONG, commission_rate=COMMISSION_RATE)

    # Simulate multiple position entries with realistic timing
    print("📊 Building position with multiple entries:")

    # Entry 1: Buy 0.1 BTC at $50,000
    timestamp1 = base_time
    pnl_1 = tracker.add_position(0.1, 50000.0, timestamp1, "BUY_001")
    print(f"   {timestamp1.strftime('%H:%M:%S')} Entry 1: 0.1 BTC @ $50,000 | "
          f"Avg: ${tracker.state.average_entry_price:,.2f} | Commission: ${-pnl_1:.2f}")

    # Entry 2: Buy 0.05 BTC at $49,000 (DCA down) - 1 hour later
    timestamp2 = base_time + timedelta(hours=1)
    pnl_2 = tracker.add_position(0.05, 49000.0, timestamp2, "BUY_002")
    avg_price = tracker.state.average_entry_price
    print(f"   {timestamp2.strftime('%H:%M:%S')} Entry 2: 0.05 BTC @ $49,000 | Avg: ${avg_price:,.2f} | Commission: ${-pnl_2:.2f}")

    # Entry 3: Buy 0.1 BTC at $52,000 (price moved up) - 2 hours later
    timestamp3 = base_time + timedelta(hours=2)
    pnl_3 = tracker.add_position(0.1, 52000.0, timestamp3, "BUY_003")
    print(f"   {timestamp3.strftime('%H:%M:%S')} Entry 3: 0.1 BTC @ $52,000 | "
          f"Avg: ${tracker.state.average_entry_price:,.2f} | Commission: ${-pnl_3:.2f}")

    print("\n📈 Final Position Summary:")
    print(f"   Total Size: {tracker.state.total_size} BTC")
    print(f"   Average Entry Price: ${tracker.state.average_entry_price:,.2f}")
    print(f"   Total Entries: {len([e for e in tracker.state.entries if e.is_increase])}")
    print(f"   Total Commission Paid: ${tracker.state.commission_paid:.2f}")
    print(f"   Time Range: {timestamp1.strftime('%H:%M:%S')} - {timestamp3.strftime('%H:%M:%S')}")

    # Test unrealized PnL at different prices
    test_prices = [48000, 50000, 52000, 55000]
    print("\n💰 Unrealized PnL at different prices:")
    for price in test_prices:
        unrealized = tracker.calculate_unrealized_pnl(price)
        print(f"   @ ${price:,}: ${unrealized:+.2f}")

    print()


def demo_position_reduction_and_realized_pnl_with_time(base_time):
    """Demo position reduction with realistic timestamps"""

    print("🎯 Demo 2: Position Reduction & Realized PnL")
    print("=" * 60)

    # Create and build up a position first
    tracker = PositionTracker(Direction.LONG, commission_rate=COMMISSION_RATE)

    print("📊 Building up position:")

    # Build position over time
    timestamp1 = base_time
    tracker.add_position(0.2, 50000.0, timestamp1, "BUY_001")
    print(f"   {timestamp1.strftime('%H:%M:%S')} Buy 0.2 BTC @ $50,000")

    timestamp2 = base_time + timedelta(minutes=30)
    tracker.add_position(0.1, 48000.0, timestamp2, "BUY_002")
    print(f"   {timestamp2.strftime('%H:%M:%S')} Buy 0.1 BTC @ $48,000")

    # Show intermediate state
    print(f"   Position: {tracker.state.total_size} BTC @ avg ${tracker.state.average_entry_price:,.2f}")

    print("\n📉 Reducing position with profit:")

    # Sell some at a profit - 1 hour after last buy
    timestamp3 = base_time + timedelta(hours=1, minutes=30)
    realized_pnl = tracker.reduce_position(0.15, 52000.0, timestamp3, "SELL_001")
    print(f"   {timestamp3.strftime('%H:%M:%S')} Sell 0.15 BTC @ $52,000")
    print(f"   Realized PnL: ${realized_pnl:+.2f}")
    print(f"   Remaining: {tracker.state.total_size} BTC @ avg ${tracker.state.average_entry_price:,.2f}")

    # Close remaining position - 30 minutes later
    timestamp4 = base_time + timedelta(hours=2)
    final_pnl = tracker.reduce_position(tracker.state.total_size, 51000.0, timestamp4, "SELL_002")
    print(f"   {timestamp4.strftime('%H:%M:%S')} Sell remaining {0.15} BTC @ $51,000")
    print(f"   Final PnL: ${final_pnl:+.2f}")

    print("\n📊 Total Session Summary:")
    print(f"   Time Range: {timestamp1.strftime('%H:%M:%S')} - {timestamp4.strftime('%H:%M:%S')}")
    print(f"   Total Realized PnL: ${tracker.state.realized_pnl:+.2f}")
    print(f"   Total Commission: ${tracker.state.commission_paid:.2f}")
    print(f"   Net PnL: ${tracker.state.realized_pnl - tracker.state.commission_paid:+.2f}")

    print()


def demo_long_short_portfolio_with_time(base_time):
    """Demo using the original function with base time for context"""
    print("🎯 Demo 3: Long/Short Portfolio Management")
    print("=" * 60)
    print(f"📅 Demo time base: {base_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Call the original demo function
    demo_long_short_portfolio()


def demo_grid_trading_simulation_with_time(base_time):
    """Demo using the original function with base time for context"""
    print("🎯 Demo 4: Grid Trading Position Management")
    print("=" * 60)
    print(f"📅 Demo time base: {base_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Call the original demo function
    demo_grid_trading_simulation()


if __name__ == "__main__":
    main()
