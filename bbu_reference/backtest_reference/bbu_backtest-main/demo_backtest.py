#!/usr/bin/env python3
"""
Demo Backtest Runner

A simplified demo that shows how the backtest system works without requiring
database connections. This simulates what would happen with real data.

Usage:
    python demo_backtest.py
"""

import random
import time
from datetime import datetime, timedelta

from src.backtest_order_manager import BacktestOrderManager
from src.backtest_session import BacktestSession
from src.enums import PositionSide as OrderSide


def generate_realistic_price_data(symbol="BTCUSDT", start_price=50000, num_ticks=1000):
    """Generate realistic price movement data for demo"""
    
    prices = []
    current_price = start_price
    timestamp = datetime.now() - timedelta(hours=num_ticks // 60)  # Start from past
    
    for i in range(num_ticks):
        # Simulate price volatility (±0.1% per tick)
        change_pct = random.uniform(-0.001, 0.001)  # ±0.1%
        current_price *= (1 + change_pct)
        
        # Add occasional larger moves (±0.5%)
        if random.random() < 0.05:  # 5% chance
            large_move = random.uniform(-0.005, 0.005)  # ±0.5%
            current_price *= (1 + large_move)
        
        # Keep price reasonable
        current_price = max(current_price, start_price * 0.8)  # Don't drop below 80%
        current_price = min(current_price, start_price * 1.2)  # Don't rise above 120%
        
        prices.append({
            'timestamp': timestamp + timedelta(minutes=i),
            'price': round(current_price, 2),
            'id': i + 1
        })
    
    return prices


def simulate_grid_strategy(price_data, grid_step_pct=0.5, grid_levels=20):
    """Simulate a grid trading strategy"""
    
    print("🎯 Simulating Grid Strategy")
    print(f"   Grid Step: {grid_step_pct}%")
    print(f"   Grid Levels: {grid_levels}")
    print(f"   Price Data: {len(price_data)} ticks")
    
    # Create backtest session
    session = BacktestSession("GRID_DEMO")
    session.initial_balance = 10000
    session.current_balance = 10000
    
    # Create order manager
    order_manager = BacktestOrderManager(session)
    order_manager.set_slippage(5)  # 0.05% slippage
    
    # Get starting price
    start_price = price_data[0]['price']
    symbol = "BTCUSDT"
    
    # Create initial grid orders
    grid_orders = []
    order_size = 0.001  # 0.001 BTC per order
    
    print(f"\n📊 Setting up grid around ${start_price:,.2f}")
    
    # Create buy orders below current price
    for i in range(1, grid_levels // 2 + 1):
        price = start_price * (1 - i * grid_step_pct / 100)
        
        order = order_manager.create_order(
            symbol=symbol,
            side=OrderSide.BUY,
            limit_price=price,
            size=order_size,
            direction="long",
            strategy_id=1,
            bm_name="grid_demo",
            timestamp=price_data[0]['timestamp']
        )
        grid_orders.append(order)
    
    # Create sell orders above current price
    for i in range(1, grid_levels // 2 + 1):
        price = start_price * (1 + i * grid_step_pct / 100)
        
        order = order_manager.create_order(
            symbol=symbol,
            side=OrderSide.SELL,
            limit_price=price,
            size=order_size,
            direction="short",
            strategy_id=1,
            bm_name="grid_demo",
            timestamp=price_data[0]['timestamp']
        )
        grid_orders.append(order)
    
    print(f"✅ Created {len(grid_orders)} grid orders")
    
    # Process price ticks
    print("\n🔄 Processing price movements...")
    
    fill_count = 0
    last_print_time = time.time()
    
    for i, tick in enumerate(price_data):
        current_price = tick['price']
        timestamp = tick['timestamp']
        
        # Check for order fills
        filled_orders = order_manager.check_fills(symbol, current_price, timestamp)
        
        if filled_orders:
            fill_count += len(filled_orders)
            
            # Create new grid orders to replace filled ones
            for filled_order in filled_orders:
                # Determine if we need to create opposite order
                if filled_order.side == OrderSide.BUY:
                    # Buy order filled, create sell order above
                    new_price = filled_order.fill_price * (1 + grid_step_pct / 100)
                    order_manager.create_order(
                        symbol=symbol,
                        side=OrderSide.SELL,
                        limit_price=new_price,
                        size=order_size,
                        direction="short",
                        strategy_id=1,
                        bm_name="grid_demo",
                        timestamp=timestamp
                    )
                else:
                    # Sell order filled, create buy order below
                    new_price = filled_order.fill_price * (1 - grid_step_pct / 100)
                    order_manager.create_order(
                        symbol=symbol,
                        side=OrderSide.BUY,
                        limit_price=new_price,
                        size=order_size,
                        direction="long",
                        strategy_id=1,
                        bm_name="grid_demo",
                        timestamp=timestamp
                    )
        
        # Print progress every 10% or when orders fill
        progress_pct = (i + 1) / len(price_data) * 100
        if (time.time() - last_print_time > 1) or filled_orders or (i + 1) % (len(price_data) // 10) == 0:
            print(f"   Progress: {progress_pct:.1f}% | Price: ${current_price:,.2f} | "
                  f"Fills: {fill_count} | Active Orders: {len(order_manager.active_orders)}")
            last_print_time = time.time()
    
    return session, order_manager


def main():
    """Run the demo backtest"""
    
    print("🚀 Grid Bot Backtest Demo")
    print("=" * 50)
    print("This demo simulates running a grid bot on historical price data")
    print()
    
    # Generate demo price data
    print("📈 Generating realistic price data...")
    price_data = generate_realistic_price_data(
        symbol="BTCUSDT",
        start_price=50000,
        num_ticks=500  # Smaller dataset for demo
    )
    
    start_price = price_data[0]['price']
    end_price = price_data[-1]['price']
    price_change = (end_price - start_price) / start_price * 100
    
    print(f"✅ Generated {len(price_data)} price ticks")
    print(f"   Start Price: ${start_price:,.2f}")
    print(f"   End Price: ${end_price:,.2f}")
    print(f"   Price Change: {price_change:+.2f}%")
    
    # Run grid simulation
    print("\n" + "=" * 50)
    session, order_manager = simulate_grid_strategy(price_data)
    
    # Show results
    print("\n" + "=" * 50)
    print("📊 BACKTEST RESULTS")
    print("=" * 50)
    
    # Get final metrics
    final_metrics = session.get_final_metrics()
    summary = session.get_summary()
    order_stats = order_manager.get_statistics()
    
    # Financial performance
    print("💰 Financial Performance:")
    print(f"   Initial Balance: ${session.initial_balance:,.2f}")
    print(f"   Final Balance:   ${summary['current_balance']:,.2f}")
    print(f"   Total PnL:       ${summary['total_pnl']:+,.2f}")
    print(f"   Return:          {summary['return_pct']:+.2f}%")
    
    # Trading statistics
    print("\n📈 Trading Statistics:")
    print(f"   Total Trades:    {summary['total_trades']}")
    print(f"   Winning Trades:  {summary['winning_trades']}")
    print(f"   Win Rate:        {summary['win_rate']:.1f}%")
    
    # Order statistics
    print("\n🎯 Order Management:")
    print(f"   Orders Created:  {order_stats['total_orders_created']}")
    print(f"   Orders Filled:   {order_stats['filled_orders']}")
    print(f"   Orders Active:   {order_stats['active_orders']}")
    print(f"   Fill Rate:       {order_stats['fill_rate'] * 100:.1f}%")
    print(f"   Slippage:        {order_stats['slippage_bps']} bps")
    
    # Detailed metrics
    if 'BTCUSDT' in final_metrics:
        metrics = final_metrics['BTCUSDT']
        print("\n📊 Detailed Analysis:")
        print(f"   Max Drawdown:    ${metrics.max_drawdown:.2f}")
        print(f"   Max Profit:      ${metrics.max_profit:.2f}")
        if metrics.profit_factor > 0:
            print(f"   Profit Factor:   {metrics.profit_factor:.2f}")
    
    # Performance vs buy-and-hold
    buy_hold_return = (end_price - start_price) / start_price * 100
    grid_return = summary['return_pct']
    
    print("\n🏆 Strategy Comparison:")
    print(f"   Buy & Hold:      {buy_hold_return:+.2f}%")
    print(f"   Grid Strategy:   {grid_return:+.2f}%")
    if grid_return > buy_hold_return:
        print(f"   🎉 Grid strategy outperformed by {grid_return - buy_hold_return:+.2f}%!")
    elif abs(grid_return - buy_hold_return) < 0.1:
        print(f"   📊 Similar performance (±{abs(grid_return - buy_hold_return):.2f}%)")
    else:
        print(f"   📉 Buy & Hold outperformed by {buy_hold_return - grid_return:+.2f}%")
    
    print("\n" + "=" * 50)
    print("🎯 Demo completed successfully!")
    print("\nThis demonstrates how your grid bot backtest system works.")
    print("With real data from your ticker_data table, you would:")
    print("1. Run: python run_backtest.py BTCUSDT")
    print("2. Get comprehensive analysis of your grid strategy")
    print("3. Export detailed results for further analysis")
    
    return session


if __name__ == "__main__":
    main()
