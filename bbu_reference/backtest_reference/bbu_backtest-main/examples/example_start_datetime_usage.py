#!/usr/bin/env python3
"""
Example: Using start_datetime Parameter for Grid Bot Backtesting

This script demonstrates various ways to use the start_datetime parameter
to run backtests on specific date ranges of your historical data.

Prerequisites:
- Historical data in the ticker_data table
- Properly configured config.yaml with your symbol settings
- Database connection configured in config/settings.py

Usage Examples:
    # Run backtest from specific date
    python examples/example_start_datetime_usage.py --mode recent_week

    # Custom date range
    python examples/example_start_datetime_usage.py --mode custom --start_datetime "2025-09-15 00:00:00"

    # Compare different date ranges
    python examples/example_start_datetime_usage.py --mode comparison
"""

import argparse
import subprocess
import sys
from datetime import datetime, timedelta


def run_backtest_command(symbol, start_datetime, balance=10000, description=""):
    """Run a backtest command and return the result"""

    print(f"\n📅 {description}")
    print(f"   Symbol: {symbol}")
    print(f"   Start DateTime: {start_datetime}")
    print(f"   Balance: ${balance:,.2f}")
    print("   " + "=" * 50)

    cmd = [
        "python", "run_backtest.py",
        "--symbol", symbol,
        "--start_datetime", start_datetime,
        "--balance", str(balance),
        "--quiet"  # Minimal output for examples
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

        # Extract key metrics from output
        output_lines = result.stdout.split('\n')

        final_balance = None
        total_pnl = None
        total_trades = None

        for line in output_lines:
            if "Final Balance:" in line:
                try:
                    final_balance = float(line.split('$')[1].replace(',', ''))
                except Exception:
                    pass
            elif "Total PnL:" in line:
                try:
                    # Handle both positive and negative PnL
                    pnl_part = line.split('$')[1]
                    total_pnl = float(pnl_part.replace(',', '').replace('+', ''))
                except Exception:
                    pass
            elif "Total Trades:" in line:
                try:
                    total_trades = int(line.split(':')[1].strip())
                except Exception:
                    pass

        return {
            'success': True,
            'final_balance': final_balance or balance,
            'total_pnl': total_pnl or 0.0,
            'total_trades': total_trades or 0,
            'return_pct': ((final_balance or balance) - balance) / balance * 100
        }

    except subprocess.CalledProcessError as e:
        print(f"❌ Backtest failed: {e}")
        if e.stderr:
            print(f"Error output: {e.stderr}")
        return {'success': False, 'error': str(e)}


def example_recent_week(symbol="BTCUSDT"):
    """Example: Run backtest on the most recent week of data"""

    print("🎯 Example 1: Recent Week Backtest")
    print("=" * 60)
    print("This example runs a backtest on the most recent week of historical data.")
    print("Useful for testing recent market conditions and strategy performance.")

    # Calculate start datetime for last week
    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)
    start_datetime = start_date.strftime('%Y-%m-%d %H:%M:%S')

    result = run_backtest_command(
        symbol=symbol,
        start_datetime=start_datetime,
        balance=10000,
        description="Recent Week Performance Test"
    )

    if result['success']:
        print("\n📊 Results Summary:")
        print("   Period: Last 7 days")
        print(f"   Total PnL: ${result['total_pnl']:+,.2f}")
        print(f"   Return: {result['return_pct']:+.2f}%")
        print(f"   Total Trades: {result['total_trades']}")

        if result['return_pct'] > 0:
            print("   ✅ Profitable period!")
        else:
            print("   📉 Losing period - consider strategy adjustments")

    return result


def example_specific_date_range(symbol="BTCUSDT", start_datetime="2025-09-15 00:00:00"):
    """Example: Run backtest on a specific date range"""

    print("\n🎯 Example 2: Specific Date Range Backtest")
    print("=" * 60)
    print("This example runs a backtest starting from a specific datetime.")
    print("Useful for testing strategy performance during known market events.")

    result = run_backtest_command(
        symbol=symbol,
        start_datetime=start_datetime,
        balance=15000,  # Different balance for variety
        description=f"Custom Date Range Test from {start_datetime}"
    )

    if result['success']:
        print("\n📊 Results Summary:")
        print(f"   Start Date: {start_datetime}")
        print(f"   Total PnL: ${result['total_pnl']:+,.2f}")
        print(f"   Return: {result['return_pct']:+.2f}%")
        print(f"   Total Trades: {result['total_trades']}")

        # Estimate daily return if we have trades
        if result['total_trades'] > 0:
            # Rough estimate assuming backtest ran for several days
            est_daily_return = result['return_pct'] / 7  # Assume ~7 days
            print(f"   Est. Daily Return: {est_daily_return:+.3f}%")

    return result


def example_date_range_comparison(symbol="BTCUSDT"):
    """Example: Compare performance across different date ranges"""

    print("\n🎯 Example 3: Date Range Comparison")
    print("=" * 60)
    print("This example compares strategy performance across different time periods.")
    print("Useful for understanding how strategy performs in different market conditions.")

    # Define different test periods
    now = datetime.now()
    periods = [
        {
            'name': '3 Days Ago',
            'start': (now - timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S'),
            'balance': 10000
        },
        {
            'name': '1 Week Ago',
            'start': (now - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'),
            'balance': 10000
        },
        {
            'name': '2 Weeks Ago',
            'start': (now - timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S'),
            'balance': 10000
        }
    ]

    results = []

    for period in periods:
        print(f"\n📅 Testing period: {period['name']}")
        result = run_backtest_command(
            symbol=symbol,
            start_datetime=period['start'],
            balance=period['balance'],
            description=f"{period['name']} Performance"
        )

        if result['success']:
            result['period_name'] = period['name']
            result['start_date'] = period['start']
            results.append(result)

    # Compare results
    if results:
        print("\n📊 COMPARISON RESULTS")
        print("=" * 60)

        best_return = max(results, key=lambda x: x['return_pct'])
        most_active = max(results, key=lambda x: x['total_trades'])

        print(f"{'Period':<15} {'Return':<10} {'PnL':<12} {'Trades':<8}")
        print("-" * 50)

        for result in results:
            print(f"{result['period_name']:<15} {result['return_pct']:+6.2f}%   "
                  f"${result['total_pnl']:+8.2f}   {result['total_trades']:>5}")

        print(f"\n🏆 Best Return: {best_return['period_name']} ({best_return['return_pct']:+.2f}%)")
        print(f"📈 Most Active: {most_active['period_name']} ({most_active['total_trades']} trades)")

        # Calculate average performance
        avg_return = sum(r['return_pct'] for r in results) / len(results)
        avg_trades = sum(r['total_trades'] for r in results) / len(results)

        print(f"📊 Average Return: {avg_return:+.2f}%")
        print(f"📊 Average Trades: {avg_trades:.1f}")

    return results


def example_high_frequency_test(symbol="BTCUSDT"):
    """Example: Test strategy on a recent high-frequency period"""

    print("\n🎯 Example 4: High-Frequency Recent Data")
    print("=" * 60)
    print("This example tests on very recent data for high-frequency analysis.")
    print("Useful for validating strategy with the latest market microstructure.")

    # Use last 24 hours
    start_time = datetime.now() - timedelta(hours=24)
    start_datetime = start_time.strftime('%Y-%m-%d %H:%M:%S')

    result = run_backtest_command(
        symbol=symbol,
        start_datetime=start_datetime,
        balance=5000,  # Smaller balance for recent test
        description="High-Frequency 24H Test"
    )

    if result['success']:
        print("\n📊 High-Frequency Results:")
        print("   Period: Last 24 hours")
        print(f"   Total PnL: ${result['total_pnl']:+,.2f}")
        print(f"   Return: {result['return_pct']:+.2f}%")
        print(f"   Total Trades: {result['total_trades']}")

        if result['total_trades'] > 0:
            # Calculate trades per hour
            trades_per_hour = result['total_trades'] / 24
            print(f"   Trades/Hour: {trades_per_hour:.1f}")

            if trades_per_hour > 5:
                print("   ⚡ High-frequency trading detected")
            elif trades_per_hour > 1:
                print("   📊 Moderate trading frequency")
            else:
                print("   🐌 Low trading frequency")

    return result


def main():
    """Main example runner with CLI interface"""

    parser = argparse.ArgumentParser(description='Start DateTime Usage Examples')
    parser.add_argument('--mode',
                       choices=['recent_week', 'custom', 'comparison', 'high_frequency', 'all'],
                       default='all',
                       help='Example mode to run')
    parser.add_argument('--symbol', default='BTCUSDT',
                       help='Trading symbol (default: BTCUSDT)')
    parser.add_argument('--start_datetime',
                       help='Custom start datetime (YYYY-MM-DD HH:MM:SS)')

    args = parser.parse_args()

    print("🚀 Start DateTime Parameter Examples")
    print("=" * 80)
    print("Demonstrating various ways to use start_datetime for grid bot backtesting")
    print(f"Target Symbol: {args.symbol}")
    print()

    results = {}

    try:
        if args.mode == 'recent_week' or args.mode == 'all':
            results['recent_week'] = example_recent_week(args.symbol)

        if args.mode == 'custom' or args.mode == 'all':
            custom_date = args.start_datetime or "2025-09-15 00:00:00"
            results['custom'] = example_specific_date_range(args.symbol, custom_date)

        if args.mode == 'comparison' or args.mode == 'all':
            results['comparison'] = example_date_range_comparison(args.symbol)

        if args.mode == 'high_frequency' or args.mode == 'all':
            results['high_frequency'] = example_high_frequency_test(args.symbol)

        print(f"\n{'=' * 80}")
        print("🎉 Examples completed successfully!")
        print("\n📋 Key Takeaways:")
        print("   ✅ start_datetime parameter allows precise control over backtest periods")
        print("   ✅ Different date ranges can show varying strategy performance")
        print("   ✅ Recent data testing helps validate current market conditions")
        print("   ✅ Historical comparisons identify robust vs. period-specific results")

        print("\n🎯 Next Steps:")
        print("   1. Run your own custom date ranges using:")
        print(f"      python run_backtest.py --symbol {args.symbol} --start_datetime 'YYYY-MM-DD HH:MM:SS'")
        print("   2. Compare performance across different market conditions")
        print("   3. Use --export flag to save detailed results for analysis")

        return True

    except KeyboardInterrupt:
        print("\n⚠️  Examples interrupted by user")
        return False
    except Exception as e:
        print(f"\n❌ Examples failed: {e}")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)